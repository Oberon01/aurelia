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
| `orbit` | Smart home: SmartRent (locks, lights, thermostat, sensors) and Roku TVs | `orbit.status`, `orbit.lock`, `orbit.unlock`, `orbit.light`, `orbit.thermostat`, `orbit.login`, `orbit.alias`, `orbit.tv_power`, `orbit.tv_input`, `orbit.tv_inputs`, `orbit.tv_app`, `orbit.tv_remote`, `orbit.tv_mute` | Built |
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
| `orbit` | Smart home through SmartRent. |
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

### The surgical view

A dark page where panels appear only while something is relevant: progress
bars while a task runs, results when it finishes, a "Complete" flash that
fades when there's nothing to read, and a confirmation panel for anything that
needs your go-ahead (unlock). Type into it like `ask.sh`: press `/`, type,
Enter. Esc clears finished panels.

From another computer, tunnel to the server and open it:

```bash
ssh -L 8080:localhost:8080 atlas@atlas-ai      # leave this running
# then browse to http://localhost:8080/view
```

The orchestrator serves it at `/view`; it only renders the panel events from
`/ws` and sends what you type to `/ask`. Result panels stay until you dismiss
them (how they should leave is still an open decision). Chrysalis's own smoke
tests are hidden. This is the Phase 2 minimum; the installable overlay app
comes with Constellation (Phase 7).

## Memory

Every task that returns something to read is saved as a panel snapshot, next
to the task's permanent metadata (tool, arguments, timing, outcome).

- **Repeat a look-up, see the last answer at once.** Ask for the home status
  again and the last panel appears immediately, labelled with its age (and
  "stale" past the tool's freshness window) while a fresh run replaces it.
  Only look-ups do this; actions like lock or light never show an old result
  as if it were new.
- **Tiers** (days in `retention` in `config/aurelia.yaml`): full panel for 7
  days, then a summary (first few items plus counts) until 90, then metadata
  only. **Pin** a panel on the display to keep it in full regardless of age.
- **Daily stats per tool** (runs, failures, median / p90 / worst time) roll up
  in `tool_stats_daily`, with a weekly view. Phase 4's inference reads these.
- **Recall by meaning** through local embeddings, when available; otherwise by
  words. `GET /memory/recall?q=is the front door locked`

Maintenance (tiering, rollups, embeddings) runs hourly on one orchestrator at
a time; `POST /memory/maintain` runs it now. Other endpoints:
`GET /memory/snapshots`, `GET /memory/snapshots/<task id>`,
`POST /memory/snapshots/<task id>/pin`, `GET /memory/stats?tool=orbit.status`.

**Embeddings are optional.** They use Ollama on the server with
`nomic-embed-text` (small, fine on a CPU). Without it everything else works and
recall matches words; snapshots are embedded later once it's there:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text
# Ollama must accept connections from Docker (it listens on 127.0.0.1 by default):
sudo systemctl edit ollama     # add:  [Service]  Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```

Keep port 11434 closed to the rest of the network (it has no login).

The end-to-end memory test needs a real Postgres with pgvector and Redis, so
the deploy test stage skips it. Run it locally with `TEST_DATABASE_URL` and
`TEST_REDIS_URL` set: `uv run python -m pytest -q tests/integration`.

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

## Orbit: the smart home

Put your SmartRent login in `.env` on the server (`SMARTRENT_EMAIL`,
`SMARTRENT_PASSWORD`). If SmartRent sends you a two-factor code, give it to
Aurelia: `smartrent code 123456`. The session is saved, so restarts and deploys
don't ask again.

Things to say (fast path, no model call):

| Say | Does |
| --- | --- |
| `lock up`, `lock the front door` | Locks; confirms the lock's real state afterwards |
| `unlock the front door` | Asks for your confirmation first, always |
| `turn on the living room light`, `turn off the lights` | Lights; asks "which light?" if it's ambiguous |
| `set the bedroom lamp to 40%` | Dimmable lights (a plain switch treats >0 as on) |
| `set the thermostat to 71` | Target for the current mode; refuses outside 60–85°F |
| `remember that the big screen is Living Room TV` | Saves your name for a device (any kind) |
| `turn on the tv`, `turn the living room tv off` | TV power, confirmed afterwards |
| `switch the tv to hdmi 2`, `switch the tv to the pc` | Input; turns the TV on first if needed |
| `open youtube on the tv` | Launches an app; turns the TV on first if needed |
| `pause the tv`, `resume the tv` | Checks what's playing first, so pause never un-pauses |
| `volume up`, `turn the volume down 5` | Volume (3 steps unless you say) |
| `mute`, `unmute the tv` | Always ends muted or unmuted as asked, even though Roku's button is a toggle |

Anything else ("is the door locked?", "what's the temperature inside?") goes
through the model, which uses `orbit.status`. Limits and the physical-world
heads-up delay live in `config/governance/functions/orbit.yaml`.

### TVs (Roku)

Orbit finds Roku TVs on the home network by itself at startup and every 30
minutes, and knows each TV by its serial number, not its IP. If the router
gives the TV a new address, the next command notices and follows it. On the TV:

1. Settings > System > Advanced system settings > Control by mobile apps >
   Network access: **Enabled** (the default, Limited, refuses commands).
2. Settings > System > Power > **Fast TV Start: on**. Without it the TV drops
   off the network when it's off, so it can't be turned back on remotely.

Inputs are read from the TV each time, so if you rename one on the TV (HDMI 2
→ "PC"), `switch the tv to the pc` works and follows later renames;
`orbit.tv_inputs` lists them. Optional, in `orbit.yaml` under `rules.tv`:
`subnets` (your network, e.g. `192.168.1.0/24`, makes a search take ~1s
instead of ~10s) and `inputs` (extra names, e.g. `{ xbox: hdmi1 }`). Aurelia can't see the TV's
screen, so she can press buttons and type into search boxes when you direct
her, but can't pick "that one" from what's showing. Playing a specific
YouTube video works by its id (`orbit.tv_app` with `content_id`), which is how
choosing from search results will work once Nebula can search YouTube.

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
- The proxy only listens on `localhost`. Open it to the network (`"8080:80"`)
  on the home box once authentication exists.
- The display is the minimal web view; the mailbox and digest come in
  Phase 4 (see the phase table in the design doc).
- Daily stats use the database's clock (UTC in the container), so a "day"
  runs midnight to midnight UTC, not Central.

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
services/functions/orbit/    smart home (SmartRent)
services/core/chrysalis/     update system (protected core)
config/chrysalis.yaml        Chrysalis settings: dry run, polling, held authors, checks
```
