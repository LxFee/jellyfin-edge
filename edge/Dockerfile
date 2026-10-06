FROM docker.io/library/python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/LxFee/jellyfin-edge"
WORKDIR /app
COPY edge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home edge
COPY edge/gateway.py edge/control.py edge/pair.py edge/runner.py edge/cache.py edge/media.py ./
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/jellyfin-edge/
USER 10001
EXPOSE 8080
CMD ["uvicorn", "gateway:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers", "--log-level", "critical"]
