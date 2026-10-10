"""Postgres as a single-replica StatefulSet, driven through kubectl."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from jiffy_chaos.postgres.controller import (
    RestartMode,
    Runner,
    StopMode,
    accepts_connections,
    run_command,
    wait_until_ready,
)

_KUBECTL_TIMEOUT = "120s"
_POD_READY_DEADLINE = 120.0
_POD_RETRY_DELAY = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Kubernetes:
    """Controls the pod of a one-replica StatefulSet.

    restart("immediate") force deletes the pod and lets the StatefulSet recreate it, so
    the data must be on a PersistentVolumeClaim to survive. kubelet still sends SIGTERM
    and waits its minimum grace period (about 2s) before SIGKILL, so an idle server can
    finish a clean shutdown in that window; one with open connections cannot.
    """

    url: str
    namespace: str = "default"
    statefulset: str = "postgres"
    pod: str = "postgres-0"
    context: str | None = None
    runner: Runner = run_command

    def start(self) -> None:
        self.runner(self._kubectl("scale", f"statefulset/{self.statefulset}", "--replicas=1"))
        self._wait_pod_ready()

    def stop(self, mode: StopMode = "fast") -> None:
        self.runner(self._kubectl("scale", f"statefulset/{self.statefulset}", "--replicas=0"))
        if mode == "immediate":
            self.runner(self._delete_pod("--ignore-not-found"))

    def restart(self, mode: RestartMode = "immediate") -> None:
        if mode == "immediate":
            self.runner(self._delete_pod())
            self._wait_pod_ready()
        else:
            target = f"statefulset/{self.statefulset}"
            self.runner(self._kubectl("rollout", "restart", target))
            self.runner(self._kubectl("rollout", "status", target, f"--timeout={_KUBECTL_TIMEOUT}"))

    def wait_ready(self, timeout: float = 30.0) -> None:
        wait_until_ready(self.url, timeout)

    def is_ready(self) -> bool:
        return accepts_connections(self.url)

    def _delete_pod(self, *extra: str) -> list[str]:
        return self._kubectl("delete", "pod", self.pod, "--grace-period=0", "--force", *extra)

    def _wait_pod_ready(self) -> None:
        wait = self._kubectl(
            "wait", "--for=condition=Ready", f"pod/{self.pod}", f"--timeout={_KUBECTL_TIMEOUT}"
        )
        deadline = time.monotonic() + _POD_READY_DEADLINE
        while True:
            try:
                self.runner(wait)
                return
            except subprocess.CalledProcessError:
                # Right after a delete or a scale up the StatefulSet may not have created
                # the pod yet, and kubectl wait fails at once on a pod that does not exist.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_POD_RETRY_DELAY)

    def _kubectl(self, *args: str) -> list[str]:
        command = ["kubectl"]
        if self.context is not None:
            command += ["--context", self.context]
        return [*command, "-n", self.namespace, *args]
