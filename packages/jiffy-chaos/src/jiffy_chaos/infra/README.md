# infra

Postgres for chaos tests, in three flavours. `jiffy_chaos.postgres.from_env()` and the
`jiffy-chaos pg` command pick one with `JIFFY_PG_MODE`, so the same test runs against any
of them.

| `JIFFY_PG_MODE` | Postgres runs as | Variables |
|---|---|---|
| `local` (default) | a throwaway cluster from local server binaries | `JIFFY_PG_DATADIR` (default `.pgdata`), `JIFFY_PG_PORT` (default: a free port), `PG_BIN` |
| `docker` | the `postgres` service in `postgres/compose.yaml` | `JIFFY_PG_URL` (required), `JIFFY_PG_COMPOSE_FILE`, `JIFFY_PG_SERVICE`, `JIFFY_PG_PROJECT` |
| `kubernetes` | the `postgres` StatefulSet in `postgres/k8s/postgres.yaml` | `JIFFY_PG_URL` (required), `JIFFY_PG_NAMESPACE` (default `jiffy`), `JIFFY_PG_STATEFULSET`, `JIFFY_PG_POD`, `JIFFY_PG_CONTEXT` |

## Local

Needs the PostgreSQL server binaries (initdb, pg_ctl). Set `PG_BIN` if they are not in a
standard place.

```sh
export JIFFY_PG_DATADIR=/tmp/jiffy-pg
jiffy-chaos pg up          # prints postgresql://postgres@127.0.0.1:<port>/postgres
jiffy-chaos pg down
```

Under WSL keep the datadir on the Linux filesystem: Postgres refuses a datadir under
`/mnt/c` or `/mnt/d`, because those mounts do not keep Unix permissions.

## Docker compose

```sh
docker compose -f infra/postgres/compose.yaml up -d --wait
export JIFFY_PG_MODE=docker
export JIFFY_PG_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
jiffy-chaos pg restart --immediate
```

`PG_PORT` changes the published port and `POSTGRES_PASSWORD` the password. Data is on
the `pgdata` volume so it survives a killed container. Remove it with
`docker compose -f infra/postgres/compose.yaml down -v`.

## Kubernetes (k3s, kind)

```sh
kubectl apply -f infra/postgres/k8s/postgres.yaml
kubectl -n jiffy wait --for=condition=Ready pod/postgres-0 --timeout=120s
export JIFFY_PG_MODE=kubernetes
export JIFFY_PG_URL=postgresql://postgres:postgres@127.0.0.1:30432/postgres
jiffy-chaos pg restart --immediate
```

The `postgres` Service is a NodePort on 30432. On k3s the node is usually reachable at
127.0.0.1. On kind, map the port in the cluster config or use
`kubectl -n jiffy port-forward svc/postgres 55432:5432`; a port-forward breaks every time
the pod is replaced, so it has to be restarted after each crash.

Data is on the `data-postgres-0` PersistentVolumeClaim so a deleted pod comes back with
it. Remove everything with `kubectl delete namespace jiffy`.
