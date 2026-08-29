FROM python:3.11-slim

# Otherwise every print() in this codebase sits in Python's stdout buffer
# forever, since a container's stdout isn't a TTY (line-buffered) - `docker
# compose logs` would show nothing until the buffer happened to fill.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1Password CLI - only needed if any config value is an `op://...` secret
# reference (see config.py's module docstring). On by default; build with
# `--build-arg INSTALL_OP=0` to drop it and shave ~25MB off the image.
ARG INSTALL_OP=1
ARG OP_VERSION=2.31.1
RUN if [ "$INSTALL_OP" = "1" ]; then set -eux; \
      apt-get update; \
      apt-get install -y --no-install-recommends curl unzip ca-certificates; \
      case "$(dpkg --print-architecture)" in \
        amd64) oparch=amd64 ;; \
        arm64) oparch=arm64 ;; \
        *) echo "unsupported arch for op CLI"; exit 1 ;; \
      esac; \
      curl -sSfL "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_VERSION}/op_linux_${oparch}_v${OP_VERSION}.zip" -o /tmp/op.zip; \
      unzip -o /tmp/op.zip op -d /usr/local/bin; \
      rm /tmp/op.zip; \
      apt-get purge -y --auto-remove curl unzip; \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Layer-cached separately from the source so an app-only change doesn't
# force a dependency reinstall.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# config.yaml, .env's contents (via env_file/environment, not the file
# itself), and certs/ are supplied at run time - see docker-compose.yml -
# not baked into the image (.dockerignore excludes them from the build
# context entirely).
ENTRYPOINT ["python", "-m", "stoat_discord_bridge"]

# health_server.py's liveness endpoint - see its module docstring for why
# this is liveness-only (event loop responsive) rather than reflecting
# per-connector state. No curl/wget in the slim base image, so the check
# shells out to Python itself instead: a non-2xx/connection failure raises,
# which exits non-zero - a plain 200 exits 0, no extra plumbing needed.
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"
