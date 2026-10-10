"""Run every gate and print one report.

    ./verify            everything (about 15 minutes)
    ./verify --quick    skip mutation testing, shorter chaos and benchmark

Needs Postgres server binaries (initdb, pg_ctl) on PATH or in PG_BIN, because the chaos
run crashes its own Postgres. Set JIFFY_PG_MODE=docker to use the compose service that
ships with jiffy-chaos instead. Reports land in reports/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PYTHON = sys.executable

TYPED = ["packages", "challenges", "tools"]
MUTATION_TARGETS = {
    "jiffy-lease (claim, compare-and-set, release, heartbeat, fair claim)": ROOT
    / "packages"
    / "jiffy-lease",
    "courier (outbox intent, attempts, merge, settle, claims)": ROOT
    / "challenges"
    / "02-flaky-provider",
}
MUTATION_FLOOR = 0.85


@dataclass
class Gate:
    name: str
    passed: bool
    seconds: float
    summary: str
    details: str = ""


@dataclass
class Report:
    gates: list[Gate] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def markdown(self) -> str:
        lines = ["| gate | result | time | summary |", "|---|---|---|---|"]
        for g in self.gates:
            mark = "pass" if g.passed else "FAIL"
            lines.append(f"| {g.name} | {mark} | {g.seconds:.0f}s | {g.summary} |")
        for g in self.gates:
            if g.details:
                lines += ["", f"### {g.name}", "", g.details]
        return "\n".join(lines)


def run(
    argv: Sequence[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def timed(name: str, body: Callable[[], tuple[bool, str, str]]) -> Gate:
    print(f"-> {name}", flush=True)
    started = time.monotonic()
    try:
        passed, summary, details = body()
    except Exception as exc:  # a crashed gate is a failed gate, with the reason
        passed, summary, details = False, f"crashed: {exc}", ""
    gate = Gate(name, passed, time.monotonic() - started, summary, details)
    print(f"   {'pass' if passed else 'FAIL'}: {summary}", flush=True)
    return gate


def last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def ruff() -> tuple[bool, str, str]:
    code_check, out_check = run([PYTHON, "-m", "ruff", "check", "."])
    code_fmt, out_fmt = run([PYTHON, "-m", "ruff", "format", "--check", "."])
    passed = code_check == 0 and code_fmt == 0
    summary = f"{last_line(out_check)}; {last_line(out_fmt)}"
    return passed, summary, "" if passed else f"```\n{out_check}\n{out_fmt}\n```"


def mypy() -> tuple[bool, str, str]:
    """One run per package: each is its own project, with its own tests/conftest.py."""
    groups = [
        [str(d / "src"), str(d / "tests")]
        for d in sorted(ROOT.glob("packages/*"))
        if (d / "src").is_dir()
    ]
    groups += [[str(d)] for d in sorted(ROOT.glob("challenges/*")) if d.is_dir()]
    groups.append([str(ROOT / "tools")])
    files = 0
    failures = []
    for group in groups:
        code, out = run([PYTHON, "-m", "mypy", "--strict", "--explicit-package-bases", *group])
        found = re.search(r"in (\d+) source files?", out)
        files += int(found.group(1)) if found else 0
        if code != 0:
            failures.append(out[-3000:])
    summary = f"{len(groups)} projects, {files} files" + (", errors" if failures else ", clean")
    return not failures, summary, "\n".join(f"```\n{f}\n```" for f in failures)


def pytest(env: dict[str, str]) -> tuple[bool, str, str]:
    junit = REPORTS / "pytest.xml"
    code, out = run(
        [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"],
        env=env,
    )
    suites: dict[str, list[int]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        path = case.get("classname", "")
        area = (
            "property tests" if "propert" in path or "test_states" in path else path.split(".")[0]
        )
        tally = suites.setdefault(area, [0, 0])
        failed = case.find("failure") is not None or case.find("error") is not None
        tally[1 if failed else 0] += 1
    details = "\n".join(
        f"- {area}: {ok} passed" + (f", {bad} failed" if bad else "")
        for area, (ok, bad) in sorted(suites.items())
    )
    return code == 0, last_line(out), details


def mutation(env: dict[str, str]) -> tuple[bool, str, str]:
    lines = []
    passed = True
    scores = []
    for label, directory in MUTATION_TARGETS.items():
        shutil.rmtree(directory / "mutants", ignore_errors=True)
        code, _ = run(
            [PYTHON, "-m", "mutmut", "run", "--max-children", str(os.cpu_count() or 4)],
            cwd=directory,
            env=env,
        )
        _, results = run(
            [PYTHON, "-m", "mutmut", "results", "--all", "true"], cwd=directory, env=env
        )
        statuses = re.findall(r": (\w[\w ]*)$", results, flags=re.MULTILINE)
        killed = statuses.count("killed")
        survived = statuses.count("survived")
        other = len(statuses) - killed - survived
        total = killed + survived + other
        score = killed / total if total else 0.0
        scores.append(score)
        ok = code == 0 and score >= MUTATION_FLOOR
        passed &= ok
        lines.append(
            f"- {label}: {killed}/{total} killed, {survived} survived, {other} other"
            f" -> {score:.1%} {'pass' if ok else 'FAIL'}"
        )
        if not ok:
            survivors = [line.strip() for line in results.splitlines() if "survived" in line]
            lines.append("  survivors:\n" + "\n".join(f"    {s}" for s in survivors[:40]))
    summary = ", ".join(f"{s:.1%}" for s in scores) + f" (floor {MUTATION_FLOOR:.0%})"
    return passed, summary, "\n".join(lines)


def scenario(
    module: str, args: list[str], env: dict[str, str]
) -> Callable[[], tuple[bool, str, str]]:
    def body() -> tuple[bool, str, str]:
        out_json = REPORTS / f"{module.split('.')[-1]}.json"
        code, out = run([PYTHON, "-m", module, *args, "--json", str(out_json)], env=env)
        table = out.split("\nPASS")[0].split("\nFAIL")[0].strip()
        return code == 0, last_line(out), table

    return body


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify")
    parser.add_argument("--quick", action="store_true", help="skip mutation, shorter runs")
    parser.add_argument("--skip", action="append", default=[], help="gate name prefix to skip")
    args = parser.parse_args(argv)
    REPORTS.mkdir(exist_ok=True)
    report = Report()

    from jiffy_chaos.postgres import LocalCluster, find_pg_bin

    env = dict(os.environ)
    if find_pg_bin() is None and env.get("JIFFY_PG_MODE", "local") == "local":
        print(
            "verify needs the Postgres server binaries (initdb, pg_ctl), because the chaos"
            " run starts and crashes its own cluster.\nInstall them (apt install postgresql,"
            " brew install postgresql@16) or point PG_BIN at their directory, or run"
            " everything in Docker: see shared/docker/Dockerfile.",
            file=sys.stderr,
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="verify-pg-") as scratch:
        cluster: LocalCluster | None = None
        if "DATABASE_URL" not in env:
            cluster = LocalCluster(Path(scratch) / "data")
            cluster.init()
            cluster.start()
            cluster.wait_ready()
            env["DATABASE_URL"] = cluster.url
        try:
            plan: list[tuple[str, Callable[[], tuple[bool, str, str]]]] = [
                ("ruff (lint and format)", ruff),
                ("mypy --strict", mypy),
                ("tests (unit, integration, property, real processes)", lambda: pytest(env)),
            ]
            if not args.quick:
                plan.append(("mutation score on claim, finish and outbox", lambda: mutation(env)))
            bench_args = ["--duration", "20"] if args.quick else []
            chaos_args = ["--actions", "600", "--kills", "60"] if args.quick else []
            chaos_env = {k: v for k, v in env.items() if k != "DATABASE_URL"}
            plan += [
                (
                    "fair claiming: 100 stores, one at 10x",
                    scenario("courier.bench", bench_args, chaos_env),
                ),
                (
                    "chaos: SIGKILLs plus a Postgres crash",
                    scenario("courier.chaos", chaos_args, chaos_env),
                ),
            ]
            for name, body in plan:
                if any(name.startswith(s) for s in args.skip):
                    continue
                report.gates.append(timed(name, body))
        finally:
            if cluster is not None:
                cluster.destroy()

    text = report.markdown()
    (REPORTS / "verify.md").write_text(text + "\n")
    (REPORTS / "verify.json").write_text(json.dumps([asdict(g) for g in report.gates], indent=2))
    print("\n" + text)
    print(f"\n{'ALL GATES PASSED' if report.passed else 'SOME GATES FAILED'} (reports/verify.md)")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
