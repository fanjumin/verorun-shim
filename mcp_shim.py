#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verorun-mcp-shim — MCP 客户端侧薄代理（WP-MCP0，零第三方依赖）
================================================================
协议基线：MCP 2026-07-28（无状态）
  - 实现 server/discover —— 规范对服务端是 MUST（SEP-2575）
  - 每请求从 params._meta 读取声明；不再有 protocol-level session，
    也不发 Mcp-Session-Id（SEP-2567）
  - 所有 result 带 resultType="complete"（SEP-2322）
  - tools/list 带 ttlMs / cacheScope（CacheableResult，SEP-2549）
  - transport 层校验 Mcp-Method / Mcp-Name（SEP-2243）
  - initialize / notifications/initialized 已被移除；此处仅保留为
    legacy 兼容分支，供尚未升级的老宿主使用（见 ASSUMPTION-16）

职责边界（设计不变式 1）：零插件逻辑、零资产、零凭据落盘。
本文件只做三件事：
  1) 把 MCP 协议帧翻译成对单一客户实例的 HTTP 调用（/api/mcp/tools、/api/mcp/call）；
  2) 把网关的 {ok, data, error} 信封翻译回 MCP 的 content/isError；
  3) 提供 stdio 与 Streamable HTTP 两种传输。
工具执行、鉴权、限流、审计、脱敏全部留在客户实例内，本进程不参与。

配置（环境变量）：
  VERORUN_BASE_URL   必填，如 https://your-instance.example（末尾斜杠自动去掉）
  VERORUN_API_KEY    必填，vr_svc_ 开头的服务密钥
  VERORUN_REGION     可选，cn | global，仅用于 stderr 排障，不进协议帧
  VERORUN_TIMEOUT    可选，HTTP 超时秒数，默认 60
  VERORUN_TOOLS_TTL  可选，tools/list 缓存秒数，默认 0（不缓存＝每次实时拉取）

用法：
  stdio（Claude Desktop / Cursor 等）:  python mcp_shim.py
  Streamable HTTP 服务端:              python mcp_shim.py --http --port 8000
  自检（不启 MCP，只打一条 tools/list）: python mcp_shim.py --selftest

实现假设：
  - [ASSUMPTION-13] tools/list 默认不缓存（ttlMs=0）。网关清单本来就是实时构造的，
    缓存会让"停用插件即刻不可见"这一验收项（#3）在 shim 侧失真。需要降延迟时可
    设 VERORUN_TOOLS_TTL=60，代价是该窗口内清单可能滞后。
  - [ASSUMPTION-14] HTTP 4xx/5xx 不抛给 MCP 客户端，而是转成 isError 结果帧。
    否则 401/429 会让宿主（如 Claude Desktop）整条链路异常退出，体验远差于一条错误结果。
  - [ASSUMPTION-15] 无 id 的未知 notification 一律不回复（JSON-RPC 2.0 要求）。
  - [ASSUMPTION-16] 保留 initialize 握手分支。2026-07-28 已移除该握手，但删掉会让
    未升级的老宿主完全连不上；保留它在规范上是"多实现一个已移除的方法"，无害。
    legacy 分支的响应用老 schema（不补 resultType），避免老客户端 schema 校验失败。
  - [ASSUMPTION-17] tools/list 的 cacheScope 固定 "private"：工具清单取决于该密钥
    所属主体可见的插件与 License 状态，不同密钥结果不同，绝不能被共享缓存复用。
    discover 的 cacheScope 才是 "public"（服务器身份与能力对所有人一致）。
  - [ASSUMPTION-18] HTTP 传输对 Mcp-Method / Mcp-Name 采取"有则严格校验、无则放行"。
    规范要求 POST 必带这两个头，但老客户端不发；缺头时放行并在 stderr 提示一次，
    比直接拒绝更符合 shim 的定位（本进程不是公网服务端，而是本机胶水）。
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = (os.environ.get('VERORUN_BASE_URL') or '').rstrip('/')
API_KEY = os.environ.get('VERORUN_API_KEY') or ''
REGION = os.environ.get('VERORUN_REGION') or ''
TIMEOUT = float(os.environ.get('VERORUN_TIMEOUT') or '60')
TOOLS_TTL = float(os.environ.get('VERORUN_TOOLS_TTL') or '0')

PROTOCOL_VERSION = '2026-07-28'
LEGACY_PROTOCOL_VERSION = '2024-11-05'
SUPPORTED_VERSIONS = [PROTOCOL_VERSION]
SERVER_INFO = {'name': 'verorun-mcp-shim', 'version': '1.0.0'}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024   # 8 MiB 上限，防宿主被超大响应拖死

# 2026-07-28 的 _meta 命名空间键（SEP-2575）
META_PROTOCOL_VERSION = 'io.modelcontextprotocol/protocolVersion'
META_CLIENT_INFO = 'io.modelcontextprotocol/clientInfo'
META_CLIENT_CAPS = 'io.modelcontextprotocol/clientCapabilities'
META_SERVER_INFO = 'io.modelcontextprotocol/serverInfo'

# 规范保留区间 -32020..-32099（见 2026-07-28 错误码分配策略）
ERR_HEADER_MISMATCH = -32020
ERR_UNSUPPORTED_VERSION = -32022

DISCOVER_TTL_MS = 3600000     # 服务器身份/能力 1 小时，cacheScope=public
INSTRUCTIONS = ('Read-only market-data and web-search tools from a self-hosted '
                'VeroRun instance. Every call executes inside that instance; '
                'the shim itself holds no data and no plugin logic.')


# ── HTTP 层 ──────────────────────────────────────────────────────

class GatewayError(Exception):
    """网关不可达 / 非 JSON 响应 / 网络层错误。"""


def _http(method, path, payload=None):
    """调网关，返回解析后的 JSON dict。

    关键点：urllib 对 4xx/5xx 抛 HTTPError，而网关的业务错误（401/403/404/429/400）
    恰恰都在这些状态码上，且响应体是标准 {ok:false,error:{...}} 信封。
    因此这里必须读取 HTTPError 的 body 并正常返回，交由上层翻译成 isError 帧。
    """
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') \
        if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={'Authorization': 'Bearer %s' % API_KEY,
                 'Content-Type': 'application/json',
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GatewayError('response too large (>8MiB)')
            return json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_RESPONSE_BYTES)
            return json.loads(raw.decode('utf-8'))
        except Exception:
            raise GatewayError('HTTP %s %s' % (e.code, e.reason))
    except urllib.error.URLError as e:
        raise GatewayError('cannot reach %s: %s' % (BASE_URL, e.reason))
    except json.JSONDecodeError:
        raise GatewayError('non-JSON response from gateway')


def _list_tools():
    r = _http('GET', '/api/mcp/tools')
    if not r.get('ok'):
        e = r.get('error') or {}
        raise GatewayError('%s: %s' % (e.get('code', 'error'),
                                       e.get('message', '')))
    return (r.get('data') or {}).get('tools') or []


def _call_tool(name, arguments):
    return _http('POST', '/api/mcp/call',
                 {'tool': name, 'arguments': arguments or {}})


# ── MCP 帧翻译 ───────────────────────────────────────────────────

def _result(req_id, payload):
    """包一个 2026-07-28 形态的结果帧。

    强制补 resultType="complete"（SEP-2322），并给 result._meta 盖上
    io.modelcontextprotocol/serverInfo（规范 SHOULD）。
    """
    body = {'resultType': 'complete'}
    body.update(payload)
    meta = dict(payload.get('_meta') or {})
    meta[META_SERVER_INFO] = dict(SERVER_INFO)
    body['_meta'] = meta
    return {'jsonrpc': '2.0', 'id': req_id, 'result': body}


def _tool_payload(gw):
    """网关信封 → tools/call 的 result 内容（不含 jsonrpc 外壳）。"""
    if isinstance(gw, dict) and gw.get('ok'):
        data = gw.get('data', '')
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return {'content': [{'type': 'text', 'text': text}]}
    e = (gw or {}).get('error') or {}
    text = '%s: %s' % (e.get('code', 'error'), e.get('message', ''))
    return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def _err(req_id, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': err}


class ToolCache(object):
    """tools/list 缓存（TTL，默认 0 = 不缓存，见 ASSUMPTION-13）。"""

    def __init__(self, ttl):
        self.ttl = ttl
        self._tools = None
        self._at = 0.0

    def get(self):
        import time
        now = time.time()
        if self._tools is not None and self.ttl > 0 and (now - self._at) < self.ttl:
            return self._tools
        tools = _list_tools()
        self._tools, self._at = tools, now
        return tools


_CACHE = ToolCache(TOOLS_TTL)


def dispatch(req):
    """单帧分发。返回响应 dict，或 None 表示不需要回复（notification）。"""
    method = req.get('method')
    rid = req.get('id')
    params = req.get('params') or {}
    meta = params.get('_meta') or {}
    is_notification = 'id' not in req

    # 版本协商：带 _meta 的 modern 请求必须落在支持列表内，否则按规范回
    # UnsupportedProtocolVersionError 并附上 supportedVersions，客户端据此重试。
    declared = meta.get(META_PROTOCOL_VERSION)
    if declared and declared not in SUPPORTED_VERSIONS:
        return _err(rid, ERR_UNSUPPORTED_VERSION,
                    'unsupported protocol version: %s' % declared,
                    {'supportedVersions': list(SUPPORTED_VERSIONS)})

    if method == 'server/discover':
        # 规范要求的 MUST 方法。客户端可能在任何其它请求之前先探测，
        # 用于版本选择，或作为 stdio 上的 backward-compat probe。
        return _result(rid, {
            'supportedVersions': list(SUPPORTED_VERSIONS),
            'capabilities': {'tools': {'listChanged': False}},
            'instructions': INSTRUCTIONS,
            'ttlMs': DISCOVER_TTL_MS,
            'cacheScope': 'public',
        })

    if method == 'initialize':
        # [ASSUMPTION-16] 已被 2026-07-28 移除，仅为老宿主保留。
        # 刻意使用老 schema 形状（无 resultType / 无 result._meta）。
        return {'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': LEGACY_PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': dict(SERVER_INFO)}}
    if method in ('notifications/initialized', 'notifications/cancelled'):
        return None
    if method == 'ping':
        # 新版已移除 ping；保留只为老客户端的心跳探测。
        return {'jsonrpc': '2.0', 'id': rid, 'result': {}}

    if method == 'tools/list':
        try:
            tools = _CACHE.get()
        except GatewayError as e:
            return _err(rid, -32603, str(e))
        return _result(rid, {
            'tools': tools,
            # [ASSUMPTION-13] ttlMs=0 → 明确告知客户端不要缓存
            'ttlMs': int(TOOLS_TTL * 1000),
            # [ASSUMPTION-17] 清单随密钥主体而变，禁止共享缓存
            'cacheScope': 'private',
        })

    if method == 'tools/call':
        name = params.get('name')
        if not name:
            return _err(rid, -32602, 'params.name is required')
        try:
            gw = _call_tool(name, params.get('arguments') or {})
        except GatewayError as e:
            return _err(rid, -32603, str(e))
        return _result(rid, _tool_payload(gw))

    if is_notification:
        return None   # [ASSUMPTION-15] 未知 notification 不回复
    return _err(rid, -32601, 'method not found: %s' % method)


# ── 传输 1：stdio（行分隔 JSON-RPC）─────────────────────────────

def run_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(_err(None, -32700, 'parse error')) + '\n')
            sys.stdout.flush()
            continue
        out = dispatch(req)
        if out is None:
            continue
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
        sys.stdout.flush()


# ── 传输 2：Streamable HTTP（stdlib http.server，POST 单帧）──────

_HEADER_WARNED = []

def run_http(port, host='0.0.0.0'):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *a):
            # 不记录 Authorization；仅记录方法/路径/状态（默认格式已含路径）
            sys.stderr.write('[shim] %s %s\n' % (self.command, self.path))

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_headers(self, req):
            """[ASSUMPTION-18] Mcp-Method / Mcp-Name 校验（SEP-2243）。

            有头则必须与 body 一致（否则 HeaderMismatchError）；
            无头时放行一次并在 stderr 提示，兼容不发这两个头的老客户端。
            """
            m = self.headers.get('Mcp-Method')
            if m is None:
                if not _HEADER_WARNED:
                    _HEADER_WARNED.append(1)
                    sys.stderr.write(
                        '[shim] warning: request without Mcp-Method header '
                        '(2026-07-28 requires it); accepted for legacy clients\n')
                return None
            if m != req.get('method'):
                return _err(req.get('id'), ERR_HEADER_MISMATCH,
                            'Mcp-Method header "%s" does not match body method "%s"'
                            % (m, req.get('method')))
            name = self.headers.get('Mcp-Name')
            if name is not None and req.get('method') == 'tools/call':
                body_name = (req.get('params') or {}).get('name')
                if body_name is not None and name != body_name:
                    return _err(req.get('id'), ERR_HEADER_MISMATCH,
                                'Mcp-Name header "%s" does not match params.name "%s"'
                                % (name, body_name))
            return None

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path not in ('/', '/mcp'):
                self._send(404, {'error': 'not found'})
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n > MAX_RESPONSE_BYTES:
                    self._send(413, {'error': 'payload too large'})
                    return
                req = json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                self._send(400, _err(None, -32700, 'parse error'))
                return
            bad = self._check_headers(req)
            if bad is not None:
                self._send(400, bad)
                return
            out = dispatch(req)
            if out is None:
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self._send(200, out)

        def do_GET(self):
            # 2026-07-28 移除了 HTTP GET 流，改由 subscriptions/listen 承载；
            # 本 shim 不提供 server→client 通知，故不支持 GET。
            self._send(405, {'error': 'GET not supported; use POST'})
        def do_DELETE(self):
            # session 已移除，DELETE 无意义
            self._send(405, {'error': 'DELETE not supported'})

    ThreadingHTTPServer((host, port), Handler).serve_forever()


# ── 自检 ─────────────────────────────────────────────────────────

def selftest():
    """不启 MCP 传输，直接打一条 /tools，用于部署后连通性验证。"""
    print('protocol : %s (legacy handshake also accepted)' % PROTOCOL_VERSION)
    print('gateway  : %s' % (BASE_URL or '(unset)'))
    print('region   : %s' % (REGION or '(unset)'))
    try:
        tools = _list_tools()
    except GatewayError as e:
        print('FAIL tools/list: %s' % e)
        return 2
    print('OK tools/list: %d tool(s)' % len(tools))
    for t in tools:
        print('  - %s' % (t.get('function') or {}).get('name'))
    return 0


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    if '--selftest' in sys.argv:
        return selftest()
    if not BASE_URL or not API_KEY:
        sys.stderr.write('VERORUN_BASE_URL and VERORUN_API_KEY are required\n')
        return 2
    if '--http' in sys.argv:
        port = int(_arg('--port', '8000'))
        host = _arg('--host', '0.0.0.0')
        sys.stderr.write('[shim] Streamable HTTP on %s:%s → %s (MCP %s)\n'
                         % (host, port, BASE_URL, PROTOCOL_VERSION))
        run_http(port, host)
        return 0
    run_stdio()
    return 0


if __name__ == '__main__':
    sys.exit(main())
