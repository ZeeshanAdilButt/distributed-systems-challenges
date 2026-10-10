"""Shape checks for the bundled Postgres manifests. They need PyYAML and skip without it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jiffy_chaos.postgres.env import INFRA


def load_all(path: Path) -> list[Any]:
    yaml = pytest.importorskip("yaml")
    with path.open(encoding="utf-8") as stream:
        return list(yaml.safe_load_all(stream))


def test_compose_file() -> None:
    [compose] = load_all(INFRA / "compose.yaml")
    postgres = compose["services"]["postgres"]

    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-postgres}"
    assert postgres["ports"] == ["${PG_PORT:-55432}:5432"]
    assert "fsync=on" in postgres["command"]
    assert "pg_isready" in " ".join(postgres["healthcheck"]["test"])
    assert postgres["healthcheck"]["interval"] == "1s"


def test_kubernetes_manifest() -> None:
    documents = load_all(INFRA / "k8s" / "postgres.yaml")
    by_kind: dict[str, list[Any]] = {}
    for document in documents:
        by_kind.setdefault(document["kind"], []).append(document)

    assert sorted(by_kind) == ["Namespace", "Secret", "Service", "StatefulSet"]
    assert all(
        document["metadata"]["namespace"] == "jiffy"
        for document in documents
        if document["kind"] != "Namespace"
    )
    [statefulset] = by_kind["StatefulSet"]
    spec = statefulset["spec"]
    assert statefulset["metadata"]["name"] == "postgres"
    assert spec["replicas"] == 1
    [container] = spec["template"]["spec"]["containers"]
    assert container["image"] == "postgres:16-alpine"
    assert "pg_isready" in container["readinessProbe"]["exec"]["command"]
    assert spec["volumeClaimTemplates"][0]["metadata"]["name"] == "data"

    services = {service["metadata"]["name"]: service["spec"] for service in by_kind["Service"]}
    assert services[spec["serviceName"]]["clusterIP"] == "None"
    assert any(service.get("type") == "NodePort" for service in services.values())
