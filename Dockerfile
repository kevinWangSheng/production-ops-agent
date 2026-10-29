# The one OpsPilot product image (#392). Every role is this image with a
# different command (deploy/compose/compose.yaml):
#   web          python -m opspilot.web serve
#   worker       python -m opspilot.worker_main
#   observer     python -m opspilot.observer
#   migrate      python -m opspilot.schema migrate
#   init-roles   python -m opspilot.deploy.roles
#   retention    python -m opspilot.retention
# Base images are pinned by digest; Python dependencies come only from
# uv.lock, runtime groups only (no dev group).
#
# Build args (#394), set by install.sh and the release workflow:
#   OPSPILOT_VERSION   the pyproject version (= opspilot.__version__)
#   OPSPILOT_REVISION  the git commit the image is built from

# Digests are the multi-platform indexes; uv is the version CI uses.
FROM ghcr.io/astral-sh/uv:0.10.8@sha256:88234bc9e09c2b2f6d176a3daf411419eb0370d450a08129257410de9cfafd2a AS uv

FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-default-groups --group m0 --group web

FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime
RUN groupadd --system --gid 10001 opspilot \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin opspilot
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY opspilot /app/opspilot
COPY deploy/docker-entrypoint.sh /usr/local/bin/opspilot-entrypoint
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Last, so a new revision does not rebuild the layers above.
ARG OPSPILOT_VERSION
ARG OPSPILOT_REVISION
LABEL org.opencontainers.image.version="${OPSPILOT_VERSION}" \
      org.opencontainers.image.revision="${OPSPILOT_REVISION}" \
      org.opencontainers.image.source="https://github.com/kevinWangSheng/production-ops-agent"
USER 10001:10001
ENTRYPOINT ["opspilot-entrypoint"]
CMD ["python", "-m", "opspilot.web", "serve"]
