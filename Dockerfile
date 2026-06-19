# ═══ Stage 1: 构建 Python wheels ═══════════════════════════
FROM python:3.11-slim AS wheels

WORKDIR /wheels

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

# ═══ Stage 2: 压缩 Docker 二进制 ═══════════════════════════
FROM docker:cli AS docker-src

RUN apk add --no-cache upx \
    && cp /usr/local/bin/docker /tmp/docker \
    && cp /usr/local/libexec/docker/cli-plugins/docker-compose /tmp/docker-compose \
    && upx --best --lzma /tmp/docker \
    && upx --best --lzma /tmp/docker-compose

# ═══ Stage 3: 运行时（Debian slim 极致优化） ═══════════════
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Docker CLI + compose plugin（UPX 压缩后）
COPY --from=docker-src /tmp/docker /usr/local/bin/docker
COPY --from=docker-src /tmp/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

# Python 依赖 + 清理
COPY requirements.txt .
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels \
    # 删除 pip（运行时不需要），保留 setuptools（APScheduler 依赖 pkg_resources）
    && pip uninstall -y pip wheel \
    && rm -rf /root/.cache \
    # 清理 Python 无用文件（测试/缓存/IDE 工具）
    && find /usr/local/lib/python3.* -depth \
       \( -name '__pycache__' -o -name '*.pyc' -o -name 'tests' \
          -o -name 'test' -o -name 'idlelib' -o -name 'tkinter' \
          -o -name 'turtledemo' -o -name '*.exe' \) -exec rm -rf {} + 2>/dev/null; true \
    # 清理临时文件
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY docker_tg_bot.py .

# 健康检查
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,time; f=os.path.getmtime('/tmp/bot_healthy') if os.path.exists('/tmp/bot_healthy') else 0; sys.exit(0 if time.time()-f<90 else 1)"

CMD ["python", "/app/docker_tg_bot.py"]
