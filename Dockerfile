# verorun-mcp-shim — 最小镜像（零第三方依赖，仅 CPython 标准库）
#
# 构建：  docker build -t verorun-mcp-shim:1.0.0 .
# 运行：  docker run -i --rm \
#           -e VERORUN_BASE_URL=https://verorun.example.com \
#           -e VERORUN_API_KEY=vr_svc_xxx \
#           verorun-mcp-shim:1.0.0
#
# 未发布到公共镜像仓库；自行构建后在本地打 tag 即可。
# 若将来发布到 ghcr，须给镜像加 label
#   io.modelcontextprotocol.server.name="com.verorun/verorun-mcp-shim"
# 否则官方 registry 不接受 OCI 条目。
#
# 验收（设计 §13 #8）：镜像内不得出现 plugins/ 任何文件、不得内置任何 VeroRun 凭据。
#   验证命令：docker run --rm --entrypoint sh <image> -c 'ls /app; grep -ril "vr_svc_" /app || echo CLEAN'
#   预期：仅 mcp_shim.py；输出 CLEAN

FROM python:3.12-alpine

# 非 root 运行：stdio 场景由宿主拉起子进程，无需特权
RUN adduser -D -u 10001 shim
WORKDIR /app

# 只拷单文件 —— 不 COPY 整个构建上下文，从机制上杜绝误带入仓库内容
COPY --chown=shim:shim mcp_shim.py /app/mcp_shim.py

USER shim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# stdio 传输：不暴露端口，由宿主进程通过 stdin/stdout 通信。
# 如需 HTTP：docker run -p 8000:8000 <image> --http --port 8000
#   （ENTRYPOINT 已是 python /app/mcp_shim.py，追加参数即可，不要再写 python）
ENTRYPOINT ["python", "/app/mcp_shim.py"]
