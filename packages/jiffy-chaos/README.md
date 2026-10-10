<h1 align="center">jiffy-chaos</h1>

<p align="center">
  Real-process chaos for tests: supervise workers, kill -9 them, freeze
  them, crash Postgres.
</p>

<p align="center">
  <a href="https://github.com/ZeeshanAdilButt/jiffy-chaos/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/ZeeshanAdilButt/jiffy-chaos/actions/workflows/ci.yml/badge.svg">
  </a>
  <img alt="Python" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white">
  <img alt="mypy" src="https://img.shields.io/badge/mypy-strict-2A6DB2">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
</p>

Real failures for tests, not mocks. It runs a fleet of real worker processes and restarts
the ones that die, kills them with SIGKILL at random, freezes and thaws them, and stops or
crashes PostgreSQL. The same chaos test can run against a local throwaway cluster, a
docker compose service, or a pod on Kubernetes.

A frozen process is how it simulates a long GC pause: every thread stops, nothing runs,
and the process carries on later as if no time had passed.

## Install

```bash
pip install "jiffy-chaos @ git+https://github.com/ZeeshanAdilButt/jiffy-chaos@v0.1.0"
pip install "jiffy-chaos[pytest] @ git+https://github.com/ZeeshanAdilButt/jiffy-chaos@v0.1.0"  # with the pg_url fixture
```

## Supervisor

```python
import sys
from jiffy_chaos.supervisor import ProcessSpec, Supervisor

worker = [sys.executable, "-m", "myapp.worker"]
specs = [ProcessSpec(f"worker-{i}", worker, env={"DATABASE_URL": url}) for i in range(4)]

with Supervisor(specs) as fleet:
    pid = fleet.kill("worker-0")  # SIGKILL, restarted after restart_delay
    fleet.freeze("worker-1")  # SIGSTOP: a GC pause that lasts until thaw
    ...
    fleet.thaw("worker-1")
    print(fleet.restarts, fleet.events)
```

- `running()`, `pid(name)`, `restarts` and `events` show what happened. Each
  `SupervisorEvent` has a `kind`: started, exited, killed, frozen, thawed or restarted.
- `stop(timeout=5.0)` sends SIGTERM to every process, hard kills what is left after the
  timeout, and stops restarting. Leaving the `with` block calls it.
- `log_path` appends one line per event. `ProcessSpec.stdout_path` appends the child's
  stdout and stderr.
- Signals go to the pid that was launched. Run the worker directly (`sys.executable -m
  ...`), not through `uv run` or a shell, or the signal stops at the wrapper.

## KillMonkey

```python
from jiffy_chaos.monkey import KillMonkey

monkey = KillMonkey(fleet, kills=200, interval=(0.05, 0.3), seed=7)
monkey.start()
run_workload()
monkey.join()
assert len(monkey.kill_log) == 200
```

Each tick it waits a random interval, picks a random running target and hard kills it. A
tick with nothing running does not count. `on_kill(number, name, pid)` is called after
each kill; if it raises, the monkey stops and `join()` re-raises.

## Signals

`jiffy_chaos.signals` has the primitives: `hard_kill(pid)`, `freeze(pid)` and `thaw(pid)`.
On POSIX they are SIGKILL, SIGSTOP and SIGCONT; `freeze` returns once the process has
actually stopped. On Windows they use psutil (TerminateProcess, suspend, resume).

## Postgres

Every controller implements `PgController`: `url`, `start()`, `stop(mode)`,
`restart(mode)`, `wait_ready(timeout)` and `is_ready()`. Mode `"immediate"` is a crash: no
clean shutdown, so the next start runs crash recovery.

| Controller | immediate restart | fast restart |
|---|---|---|
| `LocalCluster(datadir)` | SIGKILL to the postmaster and every backend, then `pg_ctl start` | `pg_ctl stop -m fast`, then start |
| `DockerCompose(compose_file, url=...)` | `docker compose kill -s SIGKILL`, then `up -d` | `docker compose restart` |
| `Kubernetes(url=...)` | `kubectl delete pod --grace-period=0 --force`, then wait for Ready | `kubectl rollout restart` |

```python
from jiffy_chaos.postgres import LocalCluster

with LocalCluster(tmp_path / "pgdata") as pg:  # initdb, start; destroy on exit
    run_writes(pg.url)
    pg.restart("immediate")  # crash; recovery runs on start
    check_nothing_committed_was_lost(pg.url)
```

`LocalCluster` needs the server binaries (initdb, pg_ctl). `find_pg_bin()` looks at
`$PG_BIN`, then `pg_config --bindir`, then the usual install locations. Its datadir must
be on a filesystem that keeps Unix permissions: under WSL, `/mnt/c` and `/mnt/d` do not.

`from_env()` picks a controller from `JIFFY_PG_MODE` so a test does not care where
Postgres runs. See [the bundled infra notes](src/jiffy_chaos/infra/README.md) for the variables and for
bringing each flavour up.

## Command line

```sh
jiffy-chaos pg up                    # local: initdb if needed, start, print the URL
jiffy-chaos pg status                # prints ready or not ready; exit code 0 or 1
jiffy-chaos pg restart --immediate   # crash and recover
jiffy-chaos pg down
jiffy-chaos pg up --mode docker      # or set JIFFY_PG_MODE
```

## Where it lives

jiffy-chaos is developed in
[distributed-systems-challenges](https://github.com/ZeeshanAdilButt/distributed-systems-challenges),
next to the challenges that use it, and split out here with its history. Issues are
welcome here; changes land there first and are mirrored.

## License

MIT
