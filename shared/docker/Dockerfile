# Runs ./verify with nothing installed on the host but Docker.
#
#   docker build -f shared/docker/Dockerfile -t dsc-verify .
#   docker run --rm --shm-size=256m dsc-verify            # add --quick for the short run
#
# Postgres refuses to run as root, and the chaos run starts and crashes its own
# cluster, so everything runs as an ordinary user.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-15 procps \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV PG_BIN=/usr/lib/postgresql/15/bin \
    UV_PROJECT_ENVIRONMENT=/home/runner/venv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home runner
USER runner
WORKDIR /home/runner/repo
COPY --chown=runner . .
RUN uv sync --all-packages --frozen

ENTRYPOINT ["./verify"]
