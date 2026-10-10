"""docker and kubectl are not available everywhere, so these tests record the commands
the controllers would run instead of running them."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jiffy_chaos.postgres import DockerCompose, Kubernetes
from jiffy_chaos.postgres import kubernetes as kubernetes_module

COMPOSE_FILE = Path("infra/postgres/compose.yaml")
URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"


class Recorder:
    """A Runner that records commands and fails the first `failures` that contain `fail_on`."""

    def __init__(self, fail_on: str = "", failures: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on
        self.failures = failures

    def __call__(self, argv: Sequence[str]) -> None:
        self.commands.append(list(argv))
        if self.failures > 0 and self.fail_on in argv:
            self.failures -= 1
            raise subprocess.CalledProcessError(1, list(argv))


def compose(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", "chaos", *args]


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", "k3s", "-n", "jiffy", *args]


WAIT_READY = kubectl("wait", "--for=condition=Ready", "pod/postgres-0", "--timeout=120s")


@pytest.fixture
def docker() -> tuple[DockerCompose, Recorder]:
    recorder = Recorder()
    return DockerCompose(COMPOSE_FILE, url=URL, project="chaos", runner=recorder), recorder


@pytest.fixture
def k8s() -> tuple[Kubernetes, Recorder]:
    recorder = Recorder()
    return Kubernetes(url=URL, namespace="jiffy", context="k3s", runner=recorder), recorder


def test_docker_start_and_stop(docker: tuple[DockerCompose, Recorder]) -> None:
    controller, recorder = docker
    controller.start()
    controller.stop()
    controller.stop("smart")
    controller.stop("immediate")
    assert recorder.commands == [
        compose("up", "-d", "postgres"),
        compose("stop", "postgres"),
        compose("stop", "postgres"),
        compose("kill", "-s", "SIGKILL", "postgres"),
    ]


def test_docker_restarts(docker: tuple[DockerCompose, Recorder]) -> None:
    controller, recorder = docker
    controller.restart()
    controller.restart("fast")
    assert recorder.commands == [
        compose("kill", "-s", "SIGKILL", "postgres"),
        compose("up", "-d", "postgres"),
        compose("restart", "postgres"),
    ]


def test_docker_without_project() -> None:
    recorder = Recorder()
    DockerCompose(COMPOSE_FILE, service="db", url=URL, runner=recorder).start()
    assert recorder.commands == [["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "db"]]


def test_kubernetes_start_and_stop(k8s: tuple[Kubernetes, Recorder]) -> None:
    controller, recorder = k8s
    controller.start()
    controller.stop()
    controller.stop("immediate")
    scale_down = kubectl("scale", "statefulset/postgres", "--replicas=0")
    assert recorder.commands == [
        kubectl("scale", "statefulset/postgres", "--replicas=1"),
        WAIT_READY,
        scale_down,
        scale_down,
        kubectl("delete", "pod", "postgres-0", "--grace-period=0", "--force", "--ignore-not-found"),
    ]


def test_kubernetes_restarts(k8s: tuple[Kubernetes, Recorder]) -> None:
    controller, recorder = k8s
    controller.restart()
    controller.restart("fast")
    assert recorder.commands == [
        kubectl("delete", "pod", "postgres-0", "--grace-period=0", "--force"),
        WAIT_READY,
        kubectl("rollout", "restart", "statefulset/postgres"),
        kubectl("rollout", "status", "statefulset/postgres", "--timeout=120s"),
    ]


def test_kubernetes_retries_wait_until_the_pod_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kubernetes_module, "_POD_RETRY_DELAY", 0.0)
    recorder = Recorder(fail_on="wait", failures=2)
    Kubernetes(url=URL, namespace="jiffy", context="k3s", runner=recorder).restart()
    assert recorder.commands[1:] == [WAIT_READY, WAIT_READY, WAIT_READY]


def test_kubernetes_defaults() -> None:
    recorder = Recorder()
    Kubernetes(url=URL, runner=recorder).stop()
    assert recorder.commands == [
        ["kubectl", "-n", "default", "scale", "statefulset/postgres", "--replicas=0"]
    ]


def test_readiness_against_a_port_nobody_listens_on() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = DockerCompose(COMPOSE_FILE, url=f"postgresql://postgres@127.0.0.1:{port}/x")

    assert not controller.is_ready()
    with pytest.raises(TimeoutError):
        controller.wait_ready(timeout=0.2)
