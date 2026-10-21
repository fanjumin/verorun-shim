# verorun-mcp-shim

mcp-name: com.verorun/verorun-mcp-shim

MCP 客户端侧薄代理，把一个自托管 VeroRun 实例的 7 个只读工具暴露给任意 MCP 宿主
（Claude Desktop / Cursor / 自研 Agent）。

**边界**：本代理零插件逻辑、零资产、零持久化凭据。工具执行、鉴权、限流、审计、
脱敏全部发生在客户自己的 VeroRun 实例内。镜像内不含 `plugins/` 任何文件。

## 协议基线：MCP `2026-07-28`（无状态）

| 规范项 | 本实现 |
|---|---|
| `server/discover`（规范要求服务端 MUST） | 已实现，返回 `supportedVersions` / `capabilities` / `instructions` / `ttlMs` / `cacheScope` |
| `initialize` 握手（本版已移除，SEP-2575） | 保留为 legacy 兼容分支，仅供未升级的老宿主 |
| 协议级 session、`Mcp-Session-Id`（已移除，SEP-2567） | 不使用，完全无状态 |
| 每请求 `_meta` 声明 | 读 `io.modelcontextprotocol/protocolVersion`；不支持则回 `-32022` 并附 `supportedVersions` |
| `resultType`（所有 result 必填，SEP-2322） | modern 路径统一 `"complete"` |
| `tools/list` 缓存字段（SEP-2549） | `ttlMs`（默认 `0` = 不缓存）、`cacheScope: "private"` |
| `Mcp-Method` / `Mcp-Name` 请求头（SEP-2243） | HTTP 传输严格校验；缺头放行一次以兼容老客户端 |
| `ping`（本版已移除） | 保留，仅为老客户端心跳探测 |

> 版本不匹配的判定只在请求**声明了** `_meta.protocolVersion` 时发生。老客户端不带
> `_meta`，按 legacy 握手服务，不会被误拒。

---

## 1. 安装

```bash
pip install verorun-mcp-shim          # 或
uvx verorun-mcp-shim                  # 免安装运行
```

镜像需自行构建（仓库内 `Dockerfile`，`docker build -t verorun-mcp-shim .`）。
暂未发布到任何公共镜像仓库 —— 已发布的制品才会被写入 `server.json`，
避免登记一个 404 的拉取地址。

零第三方依赖，仅 CPython 标准库（Python ≥ 3.9）。

## 2. 配置

| 环境变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `VERORUN_BASE_URL` | 是 | — | 实例地址，如 `https://verorun.example.com` |
| `VERORUN_API_KEY` | 是 | — | `vr_svc_` 服务密钥（管理端签发，仅显示一次） |
| `VERORUN_REGION` | 否 | 空 | `cn` / `global`，仅记录用于排障 |
| `VERORUN_TIMEOUT` | 否 | `60` | HTTP 超时（秒） |
| `VERORUN_TOOLS_TTL` | 否 | `0` | `tools/list` 缓存秒数；`0` = 不缓存（推荐） |

## 3. 用法

```bash
# stdio（宿主拉起子进程，最常见）
VERORUN_BASE_URL=https://verorun.example.com VERORUN_API_KEY=vr_svc_xxx verorun-mcp-shim

# Streamable HTTP
verorun-mcp-shim --http --port 8000

# 部署后连通性自检（不启 MCP 传输，只打一条 tools/list）
verorun-mcp-shim --selftest
```

Claude Desktop 配置示例（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "verorun": {
      "command": "verorun-mcp-shim",
      "env": {
        "VERORUN_BASE_URL": "https://verorun.example.com",
        "VERORUN_API_KEY": "vr_svc_replace_me"
      }
    }
  }
}
```

## 4. 暴露的工具（P0 首期，7 个，全部只读）

| 工具 | 来源 |
|---|---|
| `mcp__stock_analysis__stock__get_quote` | stock_analysis |
| `mcp__stock_analysis__stock__get_kline` | stock_analysis |
| `mcp__stock_analysis__stock__get_technical_signal` | stock_analysis |
| `mcp__stock_analysis__stock__get_fundamental_digest` | stock_analysis |
| `mcp__stock_analysis__stock__market_overview` | stock_analysis |
| `mcp__search_hub__search__search_hub_search` | search_hub |
| `mcp__search_hub__search__search_hub_fetch` | search_hub |

清单是动态的：插件被停用、License 失效、`expose_external` 声明与实际工具名不符，
都会立刻反映到 `tools/list`（对应服务端 `meta.degraded` 会给出原因）。

## 5. 错误语义

网关的业务错误统一为 `{ok:false, error:{code, message}}`，映射到 MCP 结果帧的
`isError: true` + 文本 `<code>: <message>`，**不** 抛成 JSON-RPC error（见 ASSUMPTION-14）。

| 网关 HTTP | code | 含义 |
|---|---|---|
| 401 | `unauthorized` | 密钥无效 / 已吊销 / 过期 / 非 service 类型 |
| 403 | `tool_not_exposed` | 该工具从未对外暴露 |
| 404 | `plugin_disabled` | 曾暴露过，当前不可用（插件停用 / License 失效） |
| 429 | `rate_limited` | 单 principal 限流，或全局月度上限达顶 |
| 400 | `invalid_request` | 入参结构错误，或入参命中注入模式 |
| 200 | `tool_error` | 工具执行失败（上游 isError 或异常），已脱敏 |

## 6. 自检与 CI 门禁

```bash
# 1) 语法
python -m py_compile mcp_shim.py test_protocol.py

# 2) 协议一致性（22 条，零依赖，不触网）
python -m unittest test_protocol -v

# 3) 零依赖校验：不得出现第三方 import
python - <<'PY'
import ast,sys
t=ast.parse(open('mcp_shim.py',encoding='utf-8').read())
mods={n.module.split('.')[0] for n in ast.walk(t) if isinstance(n,ast.ImportFrom) and n.module}
mods|={a.name.split('.')[0] for n in ast.walk(t) if isinstance(n,ast.Import) for a in n.names}
std=set(sys.stdlib_module_names)
bad=mods-std
print('third-party imports:',bad or 'NONE'); sys.exit(1 if bad else 0)
PY

# 4) 真实实例连通性（集成阶段）
python mcp_shim.py --selftest

# 5) 镜像扫描：无 plugins/、无凭据
docker run --rm --entrypoint sh $IMG -c 'ls /app; grep -ril "vr_svc_" /app || echo CLEAN'
```

## 7. 已登记的实现假设

- `ASSUMPTION-13`：`tools/list` 默认不缓存（`ttlMs=0`），保证"停用插件即刻不可见"。
- `ASSUMPTION-14`：HTTP 4xx/5xx 转 `isError` 结果帧，不抛 JSON-RPC error。
- `ASSUMPTION-15`：无 `id` 的未知 notification 不回复（JSON-RPC 2.0 要求）。
- `ASSUMPTION-16`：保留 `initialize` 握手。新版已移除该方法，但删掉会让未升级的宿主
  完全连不上；多实现一个已移除的方法在协议上是无害的。legacy 分支刻意使用老 schema
  形状（不带 `resultType`），避免老客户端严格校验失败。
- `ASSUMPTION-17`：`tools/list` 的 `cacheScope` 固定 `"private"`——工具清单取决于该密钥
  主体可见的插件与 License 状态，不同密钥结果不同，不可被共享缓存复用。
  `server/discover` 才是 `"public"`（服务器身份与能力对所有人一致）。
- `ASSUMPTION-18`：HTTP 传输对 `Mcp-Method` / `Mcp-Name` 采取"有则严格校验、无则放行"。
  规范要求 POST 必带这两个头，但老客户端不发；缺头放行并在 stderr 提示一次，
  比直接拒绝更符合 shim 的本机胶水定位。

## 8. 发布到官方 MCP Registry（暂缓）

`server.json` 已按 `2025-12-11` schema 写好，但**现在不应 `mcp-publisher publish`**：
本产品是私有部署，每客户实例地址不同，填不出 `remotes`；而只挂 `packages`
（stdio 适配器）指向一个没有默认端点的代理，会让发现者装了却连不上。
等有公开演示实例后再发。

真要发时，命名空间由 verorun.com 决定，注意是**反向 DNS**：

| 项 | 值 | 说明 |
|---|---|---|
| 域名 | `verorun.com` | 已有 |
| 命名空间 | `com.verorun/*` | **不是** `io.verorun/*` |
| 清单内 name | `com.verorun/verorun-mcp-shim` | 须与 PyPI README 的 `mcp-name:` 完全一致 |

域名**没有建站不影响发布**——验证有两种，只需其一：

- **DNS（适用于当前状态）**：在 `verorun.com` 加一条 TXT，无需任何 web 服务
  ```bash
  openssl genpkey -algorithm Ed25519 -out key.pem
  openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64
  # 记录形如：verorun.com. IN TXT "v=MCPv1; k=ed25519; p=<上面的 base64>"
  mcp-publisher login dns --domain verorun.com --private-key <私钥 hex>
  ```
  私钥不要进仓库，建议 `~/.config/verorun/mcp-registry.pem`，权限 0600。
- **HTTP（需要建站）**：在 `https://verorun.com/.well-known/mcp-registry-auth`
  放同一行的 `v=MCPv1; ...`。当前没有站点，暂不适用。

发布顺序不能颠倒：**先 `twine upload` 把包发到 PyPI，再回填 `server.json` 的
`packages`，最后 `mcp-publisher publish`**。反过来会把一个 404 的包名写进清单，
被 Glama / mcp.so 等目录站抓走后长期挂着死链。

发布前逐条自查（每条都是实际拒因）：

- `description` ≤ 100 字符（当前 87）；
- `repository.url` 必须是**公开可达**的地址；
- PyPI 包的 README 必须含 `mcp-name: com.verorun/verorun-mcp-shim`；
- 若改挂 npm，`package.json` 必须有 `mcpName`；
- 若挂 OCI 镜像，镜像必须有 `io.modelcontextprotocol.server.name` label。

