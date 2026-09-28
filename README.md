# Aurelia

**Aurelia** ("Aura" for short) is a personal, always-on assistant: an LLM
orchestrator that routes requests to containerized **functions** and shows her
work on the surgical-view display. She's the software built by **Project Monarch**.
Design doc: the *Monarch — Design Doc* in the Monarch project.

**Phase 1: Aurelia understands requests.** You talk to her in plain language;
a fast path handles predictable commands instantly, and a language model picks
functions and tools for everything else, always through governance.

## Functions and tools

A **function** is one containerized service that groups related **tools** under a
space-themed name. Tools are addressed as `<function>.<tool>`.

| Function | Domain | Example tools | Status |
| --- | --- | --- | --- |
| `probe` | Diagnostics, testing Aurelia herself | `probe.echo` | Built (reference) |
| `nebula` | The web (the cloud) | `nebula.search`, `nebula.download`, `nebula.open` | Planned, policy drafted |
| `constellation` | Devices and shared computer access | `constellation.search_files`, `constellation.open` | Planned, policy drafted |
| `orbit` | Smart home (SmartRent) | | Next to build |
| `kepler` | Scholar: research, summaries, knowledge base | | Planned |
| `nova` | Developer: code in a sandbox | | Planned |
| `beacon` | Communication: messages, calls, calendar | | Planned |
| `libra` | Bookkeeping: services and subscriptions | | Planned |
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
| `proxy` (Traefik) | Only entry point, on `localhost:8080`. Routes only to healthy containers. |
| `postgres` (pgvector) | Tasks, and later memory and panel snapshots. |
| `redis` | Function registry, cancel flags, panel event bus. |
| `migrate` | One-shot: applies pending SQL migrations, then exits. |
| `orchestrator` | FastAPI: `/ask`, fast path, LLM providers, function registry, governance, task lifecycle, `/ws` panel events. |
| `probe` | Reference function with one tool, `echo`. |
| `chrysalis` | Update system (home server only): watches GitHub, tests, deploys, rolls back. Also a function: `status`, `releases`, `rollback`, `approve`. |

## Talk to her

```bash
./scripts/ask.sh                     # on the server; interactive, "exit" to quit, "new" to start over
./scripts/ask.sh "echo hello 3 times"
./scripts/ask.sh --new "..."         # fresh conversation
```

```powershell
.\scripts\ask.ps1                    # from Windows (through an SSH tunnel: ssh -L 8080:localhost:8080 server)
```

`ask.sh` keeps the conversation between runs, so one-off requests keep their context.

Or over HTTP: `POST /ask {"text": "...", "session_id": "..."}`. Each reply
says which route answered (`fastpath` or `llm`), its latency, and any tasks it
started. Actions that need confirmation come back in `needs_confirmation`;
approve or decline with `POST /ask/confirm {"action_id": "...", "approve": true}`.
`ask.ps1` prompts you for these.

**How a request is handled**

1. **Fast path.** Patterns in `config/aurelia.yaml` → `fastpath` match
   predictable commands and run the tool directly with a templated reply: no
   model call, no cost. Add patterns as functions arrive (examples are there
   for Constellation and Orbit).
2. **Language model.** Anything else goes to the model chosen by
   `llm.routes.chat`, an ordered list: the first provider that answers wins
   (e.g. `[claude, ollama]` falls back to a local model). The model sees only
   the tools governance allows for you, and every call it makes goes through
   the same checks as `POST /tasks`. Tool output is wrapped as data, never
   instructions.
3. **Confirmation.** `change`-tier actions are held, not run, until you approve.

**Personality and rules for the model:** `config/identity.md`, plus the
charter's principles, appended automatically. Both hot-reload.

**Cost control:** `llm.monthly_token_cap` stops cloud model calls for the rest
of the month once reached (the fast path keeps working). Every request is
logged in `ask_requests` and every model call in `llm_calls` with tokens and
latency, for Libra and Parallax later.

**Model setup:** put an Anthropic API key in `.env` as `ANTHROPIC_API_KEY`
(use one with a low spend limit while testing). For a local model, run Ollama
on the host and add `ollama` to `llm.routes.chat`.

## Run it

```bash
cp .env.example .env        # then change the password
docker compose up -d --build
curl localhost:8080/ready
curl localhost:8080/registry/functions   # probe appears within ~10 s
.\scripts\ask.ps1 "echo hello"            # Windows; the fast path needs no API key
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

**Automatic (Chrysalis).** On the home server, Chrysalis watches `main` on
GitHub and releases every new commit by itself:

1. Classifies what changed: `config/` hot-reloads (after YAML validation),
   `services/orchestrator` and `services/functions/<name>` rebuild only that
   service, `docker-compose.yml` applies the stack, docs need nothing.
2. Runs each changed service's `test` stage (the unit tests). A failure stops
   the release before anything changes.
3. Builds images tagged with the commit, migrates, swaps with zero downtime
   (orchestrator first).
4. Verifies: orchestrator ready, every function re-registered, a real
   `probe.echo` task, then watches for crash loops.
5. Goes live, or restores the previous images **and** the previous commit.
   A failed commit is never retried; the next commit is.

The last 5 good releases keep their images, so rollback takes seconds. Every
decision is recorded in the `releases` table. Your commits deploy
automatically; commits by anyone in `hold_authors` (Aurelia's identity, later)
wait for approval. Ask Aurelia: "what's your update status?", "show recent
releases", "roll back" (needs your confirmation).

Chrysalis is **protected core**: changes to `services/core/` or
`scripts/deploy.sh` are recorded but never applied automatically. Apply them on
the server with `docker compose up -d --build chrysalis`.

**Manual.**

```bash
./scripts/deploy.sh orchestrator    # or probe, or any future function
```

Starts the new container beside the old one, waits for its health check,
then retires the old one. A failed health check rolls back automatically.

## Setting up Chrysalis (home server, once)

1. **Deploy key** (read-only access to this repo only):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/aurelia_deploy -N "" -C "aurelia server"
   cat ~/.ssh/aurelia_deploy.pub
   ```
   GitHub → the repo → Settings → Deploy keys → Add, paste it, leave write
   access **off**.
2. **Point the repo at SSH** and use that key for manual pulls too:
   ```bash
   git remote set-url origin git@github.com:Oberon01/aurelia.git
   git config core.sshCommand "ssh -i ~/.ssh/aurelia_deploy -o IdentitiesOnly=yes"
   git fetch   # accept GitHub's host key once
   ```
3. **Add to `.env`** (the commented block at the end of `.env.example`):
   ```bash
   COMPOSE_PROFILES=chrysalis
   AURELIA_ROOT=/home/<you>/aurelia          # absolute path of this repo
   AURELIA_UID=...                            # id -u
   AURELIA_GID=...                            # id -g
   DOCKER_GID=...                             # getent group docker | cut -d: -f3
   DEPLOY_KEY_PATH=/home/<you>/.ssh/aurelia_deploy
   ```
4. **Start it:** `docker compose up -d --build`. It records what's running as
   the baseline release and starts in **dry run** (`config/chrysalis.yaml`).
5. **Check a dry run:** push a small change from your dev machine, wait two
   minutes, then `docker compose logs chrysalis` or ask Aurelia for recent
   releases. Each shows the plan it *would* have run.
6. **Go live:** set `dry_run: false` in `config/chrysalis.yaml`, commit and
   push. Chrysalis applies that as its own first config-only release.

Chrysalis only runs where `COMPOSE_PROFILES=chrysalis` is set, so it never
starts on a dev machine. Never edit tracked files on the server: Chrysalis
holds releases while the working copy has local edits.

Config changes need no deploy at all: they hot-reload.
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

The orchestrator enforces tiers, enabled/disabled functions, per-function rate limits and users before any tool runs; every refusal is logged in
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
- The proxy only listens on `localhost`. Open it to the network (`"8080:80"`)
  on the home box once authentication exists.
- No display UI, memory tiers or mailbox yet — later phases.

## Layout

```
config/aurelia.yaml          hot-reloaded runtime config
config/governance/           charter + one policy per function (hot-reloaded)
config/identity.md           Aurelia's personality and rules for the model (hot-reloaded)
scripts/ask.sh               talk to Aurelia on the server (bash)
scripts/ask.ps1              talk to Aurelia from PowerShell
scripts/deploy.sh            zero-downtime update of one service
services/orchestrator/       the brain (FastAPI)
  app/llm/                   provider interface: Claude, Ollama (+ scripted, tests only)
  app/  migrations/  tests/
services/functions/probe/    reference function (tool: echo)
services/core/chrysalis/     update system (protected core)
config/chrysalis.yaml        Chrysalis settings: dry run, polling, held authors, checks
```
