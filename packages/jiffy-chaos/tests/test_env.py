from __future__ import annotations

from pathlib import Path

import pytest

from jiffy_chaos.postgres import DockerCompose, Kubernetes, LocalCluster, from_env
from jiffy_chaos.postgres.env import DEFAULT_COMPOSE_FILE

URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"


def test_local_is_the_default(tmp_path: Path) -> None:
    controller = from_env(
        {"JIFFY_PG_DATADIR": str(tmp_path / "data"), "JIFFY_PG_PORT": "6543", "PG_BIN": "/pg/bin"}
    )
    assert isinstance(controller, LocalCluster)
    assert controller.datadir == (tmp_path / "data").absolute()
    assert controller.bindir == Path("/pg/bin")
    assert controller.url == "postgresql://postgres@127.0.0.1:6543/postgres"


def test_docker_mode() -> None:
    controller = from_env({"JIFFY_PG_MODE": "docker", "JIFFY_PG_URL": URL})
    assert controller == DockerCompose(DEFAULT_COMPOSE_FILE, url=URL)

    custom = from_env(
        {
            "JIFFY_PG_MODE": "docker",
            "JIFFY_PG_URL": URL,
            "JIFFY_PG_COMPOSE_FILE": "ci/compose.yaml",
            "JIFFY_PG_SERVICE": "db",
            "JIFFY_PG_PROJECT": "ci",
        }
    )
    assert custom == DockerCompose(Path("ci/compose.yaml"), url=URL, service="db", project="ci")


def test_kubernetes_mode() -> None:
    controller = from_env({"JIFFY_PG_MODE": "kubernetes", "JIFFY_PG_URL": URL})
    assert controller == Kubernetes(url=URL, namespace="jiffy")

    custom = from_env(
        {
            "JIFFY_PG_MODE": "kubernetes",
            "JIFFY_PG_URL": URL,
            "JIFFY_PG_NAMESPACE": "chaos",
            "JIFFY_PG_STATEFULSET": "db",
            "JIFFY_PG_POD": "db-0",
            "JIFFY_PG_CONTEXT": "kind-kind",
        }
    )
    assert custom == Kubernetes(
        url=URL, namespace="chaos", statefulset="db", pod="db-0", context="kind-kind"
    )


@pytest.mark.parametrize("mode", ["docker", "kubernetes"])
def test_remote_modes_need_a_url(mode: str) -> None:
    with pytest.raises(ValueError, match="JIFFY_PG_URL is required"):
        from_env({"JIFFY_PG_MODE": mode})


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="JIFFY_PG_MODE must be"):
        from_env({"JIFFY_PG_MODE": "podman"})
