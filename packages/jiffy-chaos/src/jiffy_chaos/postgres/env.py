"""Pick a Postgres controller from environment variables, so one test runs anywhere."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from jiffy_chaos.postgres.controller import PgController
from jiffy_chaos.postgres.docker import DockerCompose
from jiffy_chaos.postgres.kubernetes import Kubernetes
from jiffy_chaos.postgres.local import LocalCluster

# The manifests ship inside the package, so `pip install jiffy-chaos` is enough.
INFRA = Path(__file__).resolve().parent.parent / "infra" / "postgres"
DEFAULT_COMPOSE_FILE = INFRA / "compose.yaml"
DEFAULT_NAMESPACE = "jiffy"


def from_env(environ: Mapping[str, str] = os.environ) -> PgController:
    """Build the controller that JIFFY_PG_MODE names: local (default), docker or kubernetes.

    local:      JIFFY_PG_DATADIR (default .pgdata), JIFFY_PG_PORT, PG_BIN
    docker:     JIFFY_PG_URL (required), JIFFY_PG_COMPOSE_FILE, JIFFY_PG_SERVICE,
                JIFFY_PG_PROJECT
    kubernetes: JIFFY_PG_URL (required), JIFFY_PG_NAMESPACE (default jiffy, the namespace
                of the bundled k8s manifest), JIFFY_PG_STATEFULSET, JIFFY_PG_POD,
                JIFFY_PG_CONTEXT
    """
    mode = environ.get("JIFFY_PG_MODE", "local")
    if mode == "local":
        port = environ.get("JIFFY_PG_PORT")
        pg_bin = environ.get("PG_BIN")
        return LocalCluster(
            Path(environ.get("JIFFY_PG_DATADIR", ".pgdata")),
            port=int(port) if port else None,
            bindir=Path(pg_bin) if pg_bin else None,
        )
    if mode == "docker":
        return DockerCompose(
            Path(environ.get("JIFFY_PG_COMPOSE_FILE", str(DEFAULT_COMPOSE_FILE))),
            url=_required(environ, "JIFFY_PG_URL", mode),
            service=environ.get("JIFFY_PG_SERVICE", "postgres"),
            project=environ.get("JIFFY_PG_PROJECT"),
        )
    if mode == "kubernetes":
        return Kubernetes(
            url=_required(environ, "JIFFY_PG_URL", mode),
            namespace=environ.get("JIFFY_PG_NAMESPACE", DEFAULT_NAMESPACE),
            statefulset=environ.get("JIFFY_PG_STATEFULSET", "postgres"),
            pod=environ.get("JIFFY_PG_POD", "postgres-0"),
            context=environ.get("JIFFY_PG_CONTEXT"),
        )
    raise ValueError(f"JIFFY_PG_MODE must be local, docker or kubernetes, not {mode!r}")


def _required(environ: Mapping[str, str], key: str, mode: str) -> str:
    value = environ.get(key)
    if not value:
        raise ValueError(f"{key} is required when JIFFY_PG_MODE={mode}")
    return value
