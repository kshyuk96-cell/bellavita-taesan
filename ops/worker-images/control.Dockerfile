# Local preparation only. Build from prepare_build_context(), never the workspace.
FROM python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419
ARG APPLICATION_COMMIT
ARG CODEX_SHA256=not-included
LABEL org.opencontainers.image.revision="${APPLICATION_COMMIT}" \
      io.taesan.worker.role="control" \
      io.taesan.worker.uid="10001" \
      io.taesan.worker.auth="runtime-only" \
      io.taesan.worker.codex-sha256="${CODEX_SHA256}"
RUN apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 update \
 && apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 install -y --no-install-recommends git bubblewrap util-linux ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 worker \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin worker \
 && install -d -m 0700 -o 10001 -g 10001 /state /work /runtime /home/worker/profile
COPY app /app
RUN test "$(git -c safe.directory=/app -C /app rev-parse HEAD)" = "${APPLICATION_COMMIT}" \
 && test -z "$(git -c safe.directory=/app -C /app status --porcelain)" \
 && chown -R 10001:10001 /app
ENV HOME=/home/worker CODEX_HOME=/home/worker/profile PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001
CMD ["/usr/local/bin/python", "-B", "/app/scripts/run_governance_service.py", "--help"]
