FROM docker:29-cli AS docker-cli

FROM python:3.11-slim AS wheels

WORKDIR /wheels

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

COPY requirements.txt .
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY docker_tg_bot.py .

# ─── 健康检查 (#7) ───────────────────────────────────────
# bot 每 30s 更新 /tmp/bot_healthy，如果 90s 内未更新则判定为不健康
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,time; f=os.path.getmtime('/tmp/bot_healthy') if os.path.exists('/tmp/bot_healthy') else 0; sys.exit(0 if time.time()-f<90 else 1)"

CMD ["python", "/app/docker_tg_bot.py"]
