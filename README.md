# Aurelia

**Aurelia** ("Aura" for short) is a personal, always-on assistant: an LLM
orchestrator that routes requests to containerized **functions** and shows her
work on the surgical-view display. She's the software built by **Project Monarch**.
Design doc: the *Monarch — Design Doc* in the Monarch project.

This is **Phase 0: the foundation.** No LLM yet — just the always-on skeleton
every later phase plugs into.

## Functions and tools

A **function** is one containerized service that groups related **tools** under a
space-themed name. Tools are addressed as `<function>.<tool>`.

| Function | Domain | Example tools | Status |
| --- | --- | --- | --- |
| `probe` | Diagnostics, testing Aurelia herself | `probe.echo` | Built (reference) |
| `nebula` | The web (the cloud) | `nebula.search`, `nebula.download`, `nebula.open` | Planned, policy drafted |
| `constellation` | Devices and shared computer access | `constellation.search_files`, `constellation.open` | Planned, policy drafted |
| `orbit` | Smart home (SmartRent) | | Planned |
| `kuiper` | Files and storage | | Planned |
| `pulsar` | Scheduling and automations | | Planned |
| `parallax` | Analytics on task history | | Planned |
| `quasar` | Media and entertainment | | Planned |

Every function follows Probe's contract: register itself and its tools at
startup and heartbeat; return from `/tools/<tool>/invoke` at once; report
progress in real units; honour cancel; report a result (partial if cancelled).

## What's here

| Piece | What it does |
| --- | --- |
| `proxy` (Traefik) | Only entry point, on port 8080. Routes only to healthy containers. |
| `postgres` (pgvector) | Tasks, and later memory and panel snapshots. |
| `redis` | Function registry, cancel flags, panel event bus. |
| `migrate` | One-shot: applies pending SQL migrations, then exits. |
| `orchestrator` | FastAPI: function registry, task lifecycle, `/ws` panel event stream, hot-reloaded config. |
| `probe` | Reference function with one tool, `echo`. |

## Run it

```bash
cp .env.example .env        # then change the password
docker compose up -d --build
curl localhost:8080/ready
curl localhost:8080/registry/functions   # probe appears within ~10 s
```

Watch the event stream in one terminal (any WebSocket client, e.g. `websocat`):

```bash
websocat ws://localhost:8080/ws
```

Start a task in another:

```bash
curl -X POST localhost:8080/tasks -H 'content-type: application/json' \
  -d '{"tool":"probe.echo","args":{"message":"hello Aurelia","steps":10}}'
```

You'll see `panel.open`, ten `panel.progress` events, then `panel.result` and
`panel.close`. Cancel one mid-run:

```bash
curl -X POST localhost:8080/tasks/<task_id>/cancel
```

It stops cleanly and returns a partial result. Every run lands in `task_runs`
with its duration, even trivial ones.

## Update without downtime

```bash
./scripts/deploy.sh orchestrator    # or probe, or any future function
```

Starts the new container beside the old one, waits for its health check,
then retires the old one. A failed health check rolls back automatically.
Try it with the WebSocket open: you'll see at most a reconnect.

Config changes need no deploy at all: edit `config/aurelia.yaml` and save.
`curl localhost:8080/config` shows what's live.

## Governance

Rules live in `config/governance/` and are hot-reloaded:

- `charter.yaml`: rules every function follows, the action tiers, default rate
  limit, and users.
- `functions/<name>.yaml`: one policy per function. It sets each tool's tier
  (a function can't pick its own) and holds that function's specific rules,
  e.g. Nebula's download limits or Constellation's sharing rules.

| Tier | Rule |
| --- | --- |
| `reference`, `read`, `act` | Run on request, logged |
| `change` | Refused with `409 needs_confirmation` until re-sent with `"confirmed": true` |
| `never` | Always refused |

The orchestrator enforces tiers, enabled/disabled functions, per-function rate
limits and users before any tool runs; every refusal is logged in
`governance_denials`. Each function also fetches its own policy from
`/governance/functions/<name>` and enforces its specific rules itself. A policy
file that fails to parse is ignored and the last good version stays in force.
Aurelia can propose rule changes through the mailbox but never edits these files.

**Users:** every task records who asked (`owner` by default). Adding a person
is one entry under `users:` in the charter, where per-user `deny` lists can
narrow, never widen, a function's policy.

## Rules that keep her always-on

- **No state in containers.** Tasks, sessions, panels: Postgres or Redis.
- **Every service has a healthcheck**, no `container_name`, no host ports
  (only the proxy publishes one).
- **Migrations are expand-then-contract.** Add first; drop in a later release.
  Old and new code run side by side during a deploy.
- **Functions register themselves** and heartbeat; a stopped function drops out
  after `registry.function_ttl_seconds`.

## Known gaps (by design, for later phases)

- A task running inside a function container that gets replaced is cut off
  after the 20 s grace period. Checkpoint-and-resume arrives with the first
  long-running tool.
- No auth yet. Keep port 8080 on your LAN only (remote access: Tailscale).
- No LLM, display UI, memory tiers or mailbox — Phases 1–5.

## Layout

```
config/aurelia.yaml          hot-reloaded runtime config
config/governance/           charter + one policy per function (hot-reloaded)
scripts/deploy.sh            zero-downtime update of one service
services/orchestrator/       the brain (FastAPI)
  app/  migrations/
services/functions/probe/    reference function (tool: echo)
```
