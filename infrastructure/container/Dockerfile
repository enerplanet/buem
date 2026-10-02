# ──────────────────────────────────────────────────────────────────────────────
# BuEM API — Docker Image (deps-only)
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1 (builder) — create the conda environment
#   Stage 2 (runtime) — lean image with only the finished env + source
#
# Source code is COPIED at build time (not bind-mounted) -- this image runs
# the API server as a deployable artifact. Rebuild when source OR
# buem_env.yml changes.
#
# ── Build ──────────────────────────────────────────────────────────────────
#   docker build -f infrastructure/container/Dockerfile -t buem-api:latest .
#
# ── Run (recommended: docker compose) ─────────────────────────────────────
#   docker compose -f infrastructure/container/docker-compose.yml up -d
# ──────────────────────────────────────────────────────────────────────────────

# 25.3.1-1 is the newest miniconda3 tag built on Debian bookworm; the 26.x
# tags are trixie. bullseye's security pool was pruned after its LTS ended
# on 2026-08-31, which broke apt on the previous 24.1.2-0 base.
FROM continuumio/miniconda3:25.3.1-1 AS builder

# Create the conda environment from the pinned spec file
COPY infrastructure/env/buem_env.yml /tmp/buem_env.yml
RUN conda env create -f /tmp/buem_env.yml \
    && conda clean -afy \
    && rm /tmp/buem_env.yml

# gunicorn (WSGI server for CMD below) is deliberately not in buem_env.yml
# itself -- it's Unix-only (fork()-based) and has no win-64 conda-forge
# build, which breaks `conda env update` on Windows dev machines. Install
# it here instead, only in the Linux container where it actually works.
RUN /opt/conda/envs/buem_env/bin/pip install gunicorn

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM continuumio/miniconda3:25.3.1-1

LABEL org.opencontainers.image.source="https://github.com/UU-BUEM/buem"
LABEL org.opencontainers.image.description="Building energy model API — UU-BUEM"
LABEL org.opencontainers.image.licenses="MIT"

# curl serves the health checks here and in buem-gateway's compose files,
# which run it inside this container. cbc comes from the conda env
# (coincbc in buem_env.yml, BUEM_CBC_EXE in docker-compose.yml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built conda environment from builder
COPY --from=builder /opt/conda/envs/buem_env /opt/conda/envs/buem_env

# Activate environment
ENV PATH=/opt/conda/envs/buem_env/bin:$PATH
ENV CONDA_DEFAULT_ENV=buem_env
ENV PYTHONPATH=/app/src

# Project source (deps-only image otherwise; code changes require a rebuild)
COPY . /app
WORKDIR /app/src

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:5000/api/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "buem.apis.api_server:create_app()", "--workers", "2", "--threads", "2"]
