#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shim 协议一致性测试（stdlib unittest，零依赖）。

只测协议帧的形状与语义，不触网：_list_tools / _call_tool 全部替换为桩。
运行：python -m unittest test_protocol -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_shim as S  # noqa: E402

V = S.PROTOCOL_VERSION


def frame(method, params=None, rid=1, meta=None):
    """构造一个 modern 请求帧（带 _meta 版本声明）。"""
    p = dict(params or {})
    m = {S.META_PROTOCOL_VERSION: V}
    if meta:
        m.update(meta)
    p['_meta'] = m
    return {'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': p}


class ShimProtocolTest(unittest.TestCase):

    def setUp(self):
        S._CACHE = S.ToolCache(0)
        S._list_tools = lambda: [
            {'type': 'function',
             'function': {'name': 'mcp__stock_analysis__stock__get_quote',
                          'parameters': {}}}]
        S._call_tool = lambda name, args: {'ok': True, 'data': {'price': 1.0}}

    # ── server/discover（规范对服务端是 MUST）────────────────────

    def test_discover_is_implemented(self):
        r = S.dispatch(frame('server/discover'))
        self.assertIn('result', r, 'server/discover 必须实现')
        self.assertEqual(r['error']['code'] if 'error' in r else None, None)

    def test_discover_reports_supported_versions(self):
        res = S.dispatch(frame('server/discover'))['result']
        self.assertIn(V, res['supportedVersions'])
        self.assertEqual(res['resultType'], 'complete')

    def test_discover_is_publicly_cacheable(self):
        res = S.dispatch(frame('server/discover'))['result']
        self.assertEqual(res['cacheScope'], 'public')
        self.assertGreater(res['ttlMs'], 0)
        self.assertIn('instructions', res)

    def test_discover_carries_server_info(self):
        res = S.dispatch(frame('server/discover'))['result']
        info = res['_meta'][S.META_SERVER_INFO]
        self.assertEqual(info['name'], 'verorun-mcp-shim')
        self.assertIn('version', info)

    # ── 无状态：会话已移除 ────────────────────────────────────────

    def test_no_session_id_in_any_response(self):
        for m, p in (('server/discover', None), ('tools/list', None),
                     ('tools/call', {'name': 'x', 'arguments': {}})):
            out = S.dispatch(frame(m, p))
            self.assertNotIn('Mcp-Session-Id', out)
            self.assertNotIn('sessionId', out.get('result', {}))

    # ── resultType（SEP-2322）────────────────────────────────────

    def test_every_modern_result_has_result_type(self):
        for m, p in (('server/discover', None),
                     ('tools/list', None),
                     ('tools/call', {'name': 'x'})):
            res = S.dispatch(frame(m, p))['result']
            self.assertEqual(res.get('resultType'), 'complete', m)

    # ── 版本协商 ─────────────────────────────────────────────────

    def test_unsupported_version_returns_32022(self):
        f = frame('tools/list')
        f['params']['_meta'][S.META_PROTOCOL_VERSION] = '2025-06-18'
        err = S.dispatch(f)['error']
        self.assertEqual(err['code'], S.ERR_UNSUPPORTED_VERSION)
        self.assertEqual(err['code'], -32022)
        self.assertIn(V, err['data']['supportedVersions'])

    def test_current_version_passes(self):
        f = frame('tools/list')
        f['params']['_meta'][S.META_PROTOCOL_VERSION] = V
        self.assertIn('result', S.dispatch(f))

    def test_request_without_meta_is_tolerated(self):
        # 老客户端不发 _meta，不能因此报错
        r = S.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                        'params': {}})
        self.assertIn('result', r)

    # ── tools/list 缓存字段（SEP-2549）───────────────────────────

    def test_tools_list_has_cache_fields(self):
        res = S.dispatch(frame('tools/list'))['result']
        self.assertIn('ttlMs', res)
        self.assertIn('cacheScope', res)

    def test_tools_list_is_private(self):
        res = S.dispatch(frame('tools/list'))['result']
        self.assertEqual(res['cacheScope'], 'private',
                         '清单随密钥主体而变，禁止共享缓存')

    def test_tools_list_ttl_zero_by_default(self):
        res = S.dispatch(frame('tools/list'))['result']
        self.assertEqual(res['ttlMs'], 0, '默认不缓存，保证停用插件即刻不可见')

    def test_tools_list_ordering_is_stable(self):
        a = S.dispatch(frame('tools/list'))['result']['tools']
        b = S.dispatch(frame('tools/list'))['result']['tools']
        self.assertEqual([t['function']['name'] for t in a],
                         [t['function']['name'] for t in b])

    # ── tools/call ───────────────────────────────────────────────

    def test_call_success(self):
        res = S.dispatch(frame('tools/call',
                               {'name': 'x', 'arguments': {'a': 1}}))['result']
        self.assertEqual(res['content'][0]['type'], 'text')
        self.assertNotIn('isError', res)

    def test_call_gateway_error_becomes_iserror(self):
        S._call_tool = lambda n, a: {'ok': False,
                                     'error': {'code': 'rate_limited',
                                               'message': 'slow down'}}
        res = S.dispatch(frame('tools/call', {'name': 'x'}))['result']
        self.assertTrue(res['isError'])
        self.assertIn('rate_limited', res['content'][0]['text'])

    def test_call_missing_name(self):
        err = S.dispatch(frame('tools/call', {}))['error']
        self.assertEqual(err['code'], -32602)

    def test_gateway_unreachable_is_32603(self):
        def boom(*a, **k):
            raise S.GatewayError('cannot reach')
        S._call_tool = boom
        err = S.dispatch(frame('tools/call', {'name': 'x'}))['error']
        self.assertEqual(err['code'], -32603)

    # ── legacy 兼容分支 ──────────────────────────────────────────

    def test_legacy_initialize_kept(self):
        r = S.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {}})
        self.assertEqual(r['result']['protocolVersion'], '2024-11-05')

    def test_legacy_initialize_has_no_result_type(self):
        # 老客户端可能做严格 schema 校验，legacy 分支不该混入新字段
        r = S.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {}})
        self.assertNotIn('resultType', r['result'])

    def test_legacy_notifications_silent(self):
        self.assertIsNone(S.dispatch({'jsonrpc': '2.0',
                                      'method': 'notifications/initialized'}))
        self.assertIsNone(S.dispatch({'jsonrpc': '2.0',
                                      'method': 'notifications/cancelled'}))

    def test_unknown_notification_silent(self):
        self.assertIsNone(S.dispatch({'jsonrpc': '2.0',
                                      'method': 'notifications/whatever'}))

    def test_unknown_request_32601(self):
        err = S.dispatch(frame('nonexistent/method'))['error']
        self.assertEqual(err['code'], -32601)


if __name__ == '__main__':
    unittest.main(verbosity=2)
