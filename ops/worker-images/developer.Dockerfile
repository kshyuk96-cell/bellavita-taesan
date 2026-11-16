# Local preparation only. Build from prepare_build_context(), never the workspace.
FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419
ARG APPLICATION_COMMIT
ARG CODEX_SHA256=not-included
LABEL org.opencontainers.image.revision="${APPLICATION_COMMIT}" \
      io.taesan.worker.role="developer" \
      io.taesan.worker.uid="10002" \
      io.taesan.worker.auth="runtime-only" \
      io.taesan.worker.codex-sha256="${CODEX_SHA256}"
RUN apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 update \
 && apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 install -y --no-install-recommends git bubblewrap util-linux ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10002 worker \
 && useradd --uid 10002 --gid 10002 --create-home --shell /usr/sbin/nologin worker \
 && install -d -m 0700 -o 10002 -g 10002 /state /work /runtime /home/worker/profile
COPY app /app
# Optional host-trusted public CA bundle for managed build proxies; never persisted.
RUN --mount=type=secret,id=taesan_build_ca,target=/run/secrets/taesan-build-ca,required=false \
    if [ -f /run/secrets/taesan-build-ca ]; then export PIP_CERT=/run/secrets/taesan-build-ca; fi; \
    /usr/local/bin/python -m pip install --no-cache-dir --only-binary=:all: -r /app/manager_bridge/runtime-requirements.txt
RUN test "$(git -c safe.directory=/app -C /app rev-parse HEAD)" = "${APPLICATION_COMMIT}" \
 && test -z "$(git -c safe.directory=/app -C /app status --porcelain)" \
 && chown -R 10002:10002 /app
COPY --chmod=0555 codex /usr/local/bin/codex
RUN echo "${CODEX_SHA256}  /usr/local/bin/codex" | sha256sum -c -
ENV HOME=/home/worker CODEX_HOME=/home/worker/profile PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10002:10002
CMD ["/usr/local/bin/codex", "--version"]
