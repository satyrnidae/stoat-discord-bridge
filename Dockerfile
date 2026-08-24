FROM python:3.11-slim

# Otherwise every print() in this codebase sits in Python's stdout buffer
# forever, since a container's stdout isn't a TTY (line-buffered) - `docker
# compose logs` would show nothing until the buffer happened to fill.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

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
