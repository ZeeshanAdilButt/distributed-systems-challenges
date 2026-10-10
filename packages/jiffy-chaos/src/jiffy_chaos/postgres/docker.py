"""Postgres as a docker compose service."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from pathlib import Path

from jiffy_chaos.postgres.controller import (
    RestartMode,
    Runner,
    StopMode,
    accepts_connections,
    run_command,
    wait_until_ready,
)


@dataclass(frozen=True, slots=True)
class DockerCompose:
    """Controls one service of a compose file through the docker CLI.

    "immediate" sends SIGKILL to the container, so Postgres gets no chance to shut down.
    The data has to live on a volume for crash recovery to have anything to recover.
    """

    compose_file: Path
    _: KW_ONLY
    url: str
    service: str = "postgres"
    project: str | None = None
    runner: Runner = run_command

    def start(self) -> None:
        self.runner(self._compose("up", "-d", self.service))

    def stop(self, mode: StopMode = "fast") -> None:
        if mode == "immediate":
            self.runner(self._compose("kill", "-s", "SIGKILL", self.service))
        else:
            self.runner(self._compose("stop", self.service))

    def restart(self, mode: RestartMode = "immediate") -> None:
        if mode == "immediate":
            self.stop("immediate")
            self.start()
        else:
            self.runner(self._compose("restart", self.service))

    def wait_ready(self, timeout: float = 30.0) -> None:
        wait_until_ready(self.url, timeout)

    def is_ready(self) -> bool:
        return accepts_connections(self.url)

    def _compose(self, *args: str) -> list[str]:
        command = ["docker", "compose", "-f", str(self.compose_file)]
        if self.project is not None:
            command += ["-p", self.project]
        return [*command, *args]
