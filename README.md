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
| `mnemosyne` | The record: past conversations and facts you asked her to keep (part of Aurelia, served by the orchestrator) | `mnemosyne.recall`, `mnemosyne.facts`, `mnemosyne.remember`, `mnemosyne.forget` | Built |
| `probe` | Diagnostics, testing Aurelia herself | `probe.echo`, `probe.functions`, `probe.function` | Built (reference) |
| `nebula` | The web: search (web and YouTube), read pages, download files, open links | `nebula.search`, `nebula.youtube`, `nebula.read`, `nebula.download`, `nebula.open` | Built |
| `constellation` | His devices and the machines she runs on (part of Aurelia, served by the orchestrator). Phase 7 in progress: pairing and the devices panel built; the node agent next | `constellation.devices` | Building |
| `orbit` | Smart home: SmartRent (locks, lights, thermostat, sensors) and Roku TVs | `orbit.status`, `orbit.lock`, `orbit.unlock`, `orbit.light`, `orbit.thermostat`, `orbit.login`, `orbit.alias`, `orbit.tv_power`, `orbit.tv_input`, `orbit.tv_inputs`, `orbit.tv_app`, `orbit.tv_remote`, `orbit.tv_mute` | Built |
| `kepler` | Research and learning: cited answers from several sources, a library of topics that grows, explanations at your level, quizzes; written to Trilium | `kepler.research`, `kepler.recall`, `kepler.topic`, `kepler.explain`, `kepler.quiz`, `kepler.read_pdf`, `kepler.forget` | Built |
| `nova` | Developer: code in a sandbox | | Planned |
| `beacon` | Communication: messages, calls, calendar | | Planned |
| `libra` | Bookkeeping: services and subscriptions | | Planned |
| `kuiper` | Files and storage | | Planned |
| `pulsar` | Scheduling and automations | | Planned |
| `parallax` | Analytics on task history: habits, what matters most, timing; proposes to the mailbox | `parallax.analyze`, `parallax.mailbox`, `parallax.digest` | Built |
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

## Pairing and access

Every device that talks to Aurelia has its own key: each browser, the phone,
each computer running her node, each terminal. Without one, nothing gets past
the proxy (Traefik asks `/auth/check` first). Services inside the stack talk to
each other directly and don't need keys.

**Pairing a browser.** Open the display on it. An unpaired browser lands on a
pairing page and shows a code like `K7P-4QX`. Approve it one of two ways:

- from the mailbox on any device that's already paired (the card shows the same
  code: only approve if they match), or
- on the server: `./scripts/pair.sh K7P-4QX`. That's how the very first device
  gets in, and it works any time.

The browser then gets its key as a cookie the page itself can't read, and the
display opens. Codes expire after 15 minutes; at most 5 can wait at once.

**Terminals.** `./scripts/pair.sh --terminal` gives the server's terminal a key
(saved to `~/.config/aurelia/key`), which `scripts/ask.sh` uses. For
`ask.ps1` on Windows, put that key in `%APPDATA%\aurelia\key` or `$env:AURELIA_KEY`.

**What each key reaches.** A browser key reaches everything the display does.
A node key reaches only the node connection (`/nodes/…`): it can't approve,
decide or revoke anything. A terminal key reaches only `/ask`.

**Devices.** "Show my devices" lists everything paired, with when each was last
seen; **Revoke** on a row cuts that device off at once. On the server:
`./scripts/pair.sh --list` and `./scripts/pair.sh --revoke ID`. Aurelia can list
devices but can never pair, approve or revoke one.

**Reaching her from other devices: Tailscale Serve.** The proxy stays bound to
the server's localhost. Tailscale publishes it to your tailnet only, with
HTTPS (which the phone's installable app needs), never to your LAN. On port
8443, because Nginx Proxy Manager owns 443 on this server. Tailscale runs here
as a container with host networking, so the command goes through it:

```bash
docker exec tailscale tailscale serve --bg --https=8443 http://127.0.0.1:8080
docker exec tailscale tailscale serve status   # e.g. https://atlas-ai.<your-tailnet>.ts.net:8443
```

(Once, in the Tailscale admin console: DNS → MagicDNS on, and HTTPS Certificates
→ Enable.) The setting lives in the container's state, so keep its state
directory on a volume.

Open that address on any device in your tailnet and pair it. The SSH tunnel
(`ssh -L 8080:localhost:8080 server`, then `http://localhost:8080`) keeps
working too; it's just another browser to pair.

## Constellation: her computers

A computer running her **node agent** is one she can act on: open a page or an
app there, see what's installed. The agent is one Python file (standard
library only), the same on Linux and Windows. It never opens a port: it calls
out over the tailnet with its own node key, says what it is, and waits for
work. What it will do is fixed in the agent, not decided by the server: open an
http(s) page, launch an installed app by name, list apps, describe the machine.
Nothing runs through a shell.

**Install on Linux** (Ubuntu, the living room PC), as your user, no sudo:

```bash
curl -fsSL https://atlas-ai.<tailnet>.ts.net:8443/node/install-linux.sh | bash -s -- https://atlas-ai.<tailnet>.ts.net:8443 "Living room TV"
```

It shows a code: approve it in the mailbox. A systemd user service keeps it
running through its guard (`journalctl --user -u aurelia-node -f` for its log).
Revoking the computer on the devices panel stops it.

**What each device is for (roles).** Every computer and browser has a role,
and her behavior follows from it:

| Role | Examples | What it means |
|---|---|---|
| Presentation | the TV PC, a wall tablet | Shows only what's sent to it, over an ambient screen. Panels nobody sent it never appear there. |
| Workstation | your desktop, a laptop | A machine you use: panels you ask for open there; she never takes over its screen without asking. |
| Server | the home server, the GPU box | No screen: health, services, fixes. |
| Companion | your phone | Personal: what you ask there stays there. |
| Room | a microphone and speaker unit (later) | No screen. |

A role is guessed until you choose one on the devices panel: a computer
running her shell is Presentation, one with a screen a Workstation, one
without a Server; a phone's browser is a Companion. Add `--role=server` (or
`presentation`, `workstation`) to the install line to suggest one. Only you
set roles, on the devices panel; she can mention them, never change them.

**Sending to another display.** "Send this to the TV", "send panel 2 to my
phone", "send this to all presentation devices", "send it everywhere". "This"
is the panel you clicked, otherwise the newest one. She can also send a link
or a short note ("send 'dinner at 7' to the hall tablet"). A Presentation
display shows it big; a link sent to a Presentation computer opens there. A
display that isn't open gets it when it next opens (within a day). Something that isn't on screen yet works too: "send the device list to my
iPhone" or "show the house on the TV" runs the look-up once and opens it only
there, never here first (look-ups only; an action happens where you ask).

**Every send is checked.** Each display tells her which panels it's showing
whenever that changes. After sending, she checks the panel appears in that
display's report; if it doesn't within a few seconds she sends it once more,
and if it still doesn't (a phone that's locked, a tab in the background) she
keeps it, and it shows the moment that display is back on screen. Her answer
says exactly where it was confirmed and where it's waiting. "What's on my
screens?" (or "what's on the TV?") shows each display with its panels, whether
it's on screen, anything kept for it, and what a TV is playing. If the TV
is showing something she opened, the panel waits behind it and she says so.

**The ambient screen.** A Presentation display with nothing on it shows a dim
clock, the date, the weather and anything worth a glance (a door left
unlocked), and goes dark during quiet hours. The clock steps out of the way
when a panel arrives. Settings are under `ambient:` in `config/aurelia.yaml`:
how dim, the quiet hours, and your latitude and longitude for the weather
(from Open-Meteo, no key; only the coordinates are sent).

**Her shell: the computer's screen is her.** Add `--shell` to the install line
(it asks for sudo once):

```bash
curl -fsSL https://…:8443/node/install-linux.sh | bash -s -- https://…:8443 "TV PC" --shell
```

That adds an "Aurelia" login session and makes it the automatic login: no
desktop, just her view full screen in a kiosk browser (Chromium is installed if
there's no Chrome, Chromium or Edge). The session is Wayland, run by Cage (a
kiosk compositor), so it works on Ubuntu 25.10 and later, which dropped X11 from
the desktop, and on 24.04 too. The login session is set through the system's
account service (and checked), and every step says what failed if one does. The screen signs itself in with a key its
agent gets from Aurelia; that key shows her but can't decide mailbox items or
touch devices, and it's revoked with its computer. Pages she opens there open
inside it; "go home on the TV PC" closes them. GNOME stays on the login screen
as a way back in; `--no-shell` returns to it at the next login.

**Updates (your standing decision, Constellation's policy `updates`).** Nodes keep
their agent current without asking: each check-in compares versions, and a newer
one Aurelia serves (only ever from a release) is downloaded, checked against its
checksum, compiled, and swapped in with the old one kept. The guard, which never
updates itself, puts the old one back if the new one doesn't reconnect. After
three failed tries it stops and a mailbox card tells you; **Retry** starts over.
The devices panel shows each computer's agent version. Agents from before this
(0.2.x) can't update themselves: run the install line once more on them.

| Say | Does |
| --- | --- |
| `open youtube.com on the living room tv` | Opens the page in that computer's browser |
| `open the vlc app on the living room tv` | Starts an installed app by name |
| `what's installed on the living room tv` | The apps a person would launch there |
| `show my devices` | Every paired device; computers show online or offline |
| `show connected nodes` | Just your computers: online, agent version, shell |
| `go home on the tv pc` | Closes what she opened there; her view again |

With one computer online, she uses it when you don't say which. "Open X on Y"
tries a computer named Y first (YouTube, Netflix and the like open as sites in
its browser unless the app is installed), then a Roku TV named Y through Orbit.

**How your computers are doing.** Every computer running her agent (0.5+) reports
its hardware (CPU, GPU, memory, disks, model) and, with each check-in, a small
health reading. Ask for more and it samples live: CPU and memory, swap, disk space,
temperatures and whether the CPU is slowing itself down, GPU load, battery,
uptime, network, and what's using the most. She explains what's wrong in plain
words. Processes are reported by name, user and what they use, never their command
lines. Closing a program never happens by itself: she finds exactly what would
close and puts a confirmation card in front of you. The computer closes only those
exact processes (checked by number and start time), only yours, never the
system's or her own. It asks first and ends a program only if you say force.
Problems past the lines in Constellation's policy (`health`) show on the devices
panel and in the digest; urgent ones (a disk 97% full, 95°C) also get a mailbox
card, once a day per problem. Windows reporting is written but gets its real test
with the Windows installer.

| Say | Does |
| --- | --- |
| `how's the laptop doing` / `check on the tv pc` | Health panel: hardware, live numbers, what's using the most, and what's wrong |
| `why is the laptop so slow` | The same, with the reason in a sentence |
| `what are the specs of the desktop` | Its hardware |
| `what's running on the tv pc` / `what's using the memory on the laptop` | Processes, by program and one by one |
| `close chrome on the laptop` / `force close chrome on the laptop` | A confirmation card, then it closes |

**Signing in (your decision, Sep 30: strict).** She can sign in to a site on one
of your screens with a login you saved on that computer yourself. She never sees
it. On the TV PC:

```bash
python3 ~/.local/share/aurelia-node/aurelia_node.py login add youtube       # or crunchyroll, netflix
python3 ~/.local/share/aurelia-node/aurelia_node.py login list              # names only
```

The password is asked without being shown and kept in a file only that account
can read (refused if anyone else could). "Sign in to youtube on the TV PC" puts
a confirmation card in front of you. The computer then opens the site's own
sign-in page and fills the login there, and only there (by exact address, never
a lookalike). Nothing of it goes to Aurelia's server, the model or any log; she
hears only "signed in" or "waiting for your phone" (2-step stays yours). Other
sites work with `--origin https://login.example.com`. Google sometimes refuses
browsers it thinks are automated; if so, sign in by hand once.

**A computer on a TV's input.** The TV PC's picture goes through the living room
Roku TV, so the TV has to be on and on the right input. Set which input in
Constellation's policy (`screens`): the computer, the TV (Orbit's name for it)
and the input ("HDMI 2", or what you named it on the TV). Then opening
something on the TV PC or "go home on the tv pc" also has Orbit turn the TV on
if it's off and switch it to that input, while the page loads. A web address
"on the living room tv" goes to the computer plugged into it (a Roku can't open
web pages); an app name there still opens the Roku's own app. To find the
input: put the TV PC on screen with the remote and ask "what inputs does the
living room tv have" (the one showing is marked).

**Seeing and using the page on her screen (step 4).** On a computer running her
shell, she can read the page that's open and act on it the way you would with a
remote: everything clickable gets a number, drawn on the TV itself and listed on
your panel with a screenshot, so "click 4" works straight away. For "click the
second video" she looks, picks the number, then clicks. The agent talks to the
kiosk browser only through its control port on 127.0.0.1, and the script it runs
in the page is fixed in the agent and runs apart from the page's own JavaScript.
What a page says is data, never instructions. She never types into password or
payment boxes, and anything that buys, pays, sends, posts or deletes (a "Place
order" button, Enter in a message box) comes back as a card for your OK instead
(`submit`, always confirmed; the words are in Constellation's policy,
`screen.confirm_before`). Screenshots are kept for an hour. With no screen named,
she uses the one running her shell.

| Say | Does |
| --- | --- |
| `what's on the screen` / `show numbers` | Numbers what can be clicked, on the TV and your panel |
| `click 4` (or `press 4`, `open 4`) | Clicks number 4 |
| `click the second video` | Looks, finds it, clicks it (through the model) |
| `type cats and search` / `type hello into 8` | Types into the selected (or numbered) box |
| `scroll down` / `scroll to the top` / `go back` | New numbers after a scroll; back is the browser's back |
| `press enter` / `press escape` | A key |
| `pause` / `play` | The video on the page |
| `skip ahead 30 seconds` / `fast forward` / `rewind` / `go back 10 seconds` | Seek by an amount (10 s by default) |
| `go to 1:30` / `start it over` | Jump to a time |
| `full screen` / `exit full screen` | The player, with its controls |
| `volume 30` / `mute the video` / `play at 1.5x` | Volume, mute, speed |

"Pause the living room tv", "mute" and "volume up" still go to a Roku. Skipping
YouTube's ads and other site-specific smarts come in Phase 9 (Quasar); for now
she can see and click a "Skip" button like any other.

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

Or over HTTP: `POST /ask {"text": "...", "session_id": "...", "screen": {...}}`
(`screen` is optional: the panels you see, `{"panels": [{"n", "task_id", "tool", "title"}], "focus": n}`,
sent by the display). Each reply
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

**Speaking styles:** say "use Raphael style" to try her in the style of
Raphael from *That Time I Got Reincarnated as a Slime*: formal and precise,
with replies opening "Answer.", "Report." or "Notice." and held actions asking
"Do you wish to proceed?". Say "normal style" to go back. The choice is per
person and stays until you change it. A style changes only how she speaks,
never what she may do. Tune it in `config/styles/raphael.md` (read on every
request, so edits apply right away).

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
Enter (Shift+Enter for a new line). Esc clears finished panels.

**Long answers open as a panel.** When her reply runs past a couple of
sentences (`display.reply_panel_sentences`, or `reply_panel_chars`), the whole
answer opens as a panel on the display you asked from, and the top line keeps
just its first sentence with an arrow. The panel stays like any look-up: pin
it, send it to the TV, or ask about it ("summarize panel 2"). A turn that
already opened a panel (search results, research) keeps its reply on the top
line.

**Each display keeps its own panels.** A panel opens only on the display that
asked for it: what you ask on the phone stays on the phone, and the desk
stays as it was. Panels nobody in particular asked for (a schedule, a request
from `ask.sh`) show on every display except Presentation ones, so a TV shows
only what's sent to it. The mailbox count shows everywhere. The
morning digest opens where you first are after the digest hour, never on a
node's screen by itself. Revoking a device also closes its open display within
seconds.

The top of the screen is the conversation, and it has a fixed height, so
nothing she says moves or resizes the panels. Her name has the time and date
under it, your last question sits across the top, and her reply sits just
below. A long reply fades out at the bottom with MORE, which opens it over the
panels; LESS, Esc or a click elsewhere closes it. The layout uses the full
width: one panel spans up to 1100 px, and so does the command bar.

**An interface that answers, not software to operate.**

- *She picks the size of the answer.* Every tool call carries a `_display`
  choice: "brief" for one fact ("is the back door locked?"), which she answers
  in her reply with no panel, or "panel" when there's more to see. With a
  panel, her reply adds only what matters.
- *Answer-first panels.* Each panel leads with its headline in large type
  ("Front Door unlocked · 71° · 1 light on"), in amber when something's off;
  the title becomes a small label and the details sit below.
- *Sized to their content.* One fact or device is a small card, a set of cards
  is medium, and results, pages, lists and the mailbox are large. Panels share
  each row by size (a 12-column grid), and three panels put the largest on its
  own row.
- *Next steps.* Under a result, up to three likely follow-ups ("Lock Front
  Door", "Read #1"), one of them what you usually ask next (from your own
  requests: a follow-up made at least twice within 3 minutes, `GET /suggest`).
  Tapping one focuses that panel and sends it as an ordinary request.
- *Linked replies.* Names in her reply (devices, results, functions, "panel
  2") are dotted; pointing at one lights up that panel and the card it means.
- *Worth a glance.* The bottom corners show only what's off or in motion (a
  door unlocked, a leak, low battery, a device offline, an important action
  that failed, a download at 64%), from `GET /glance`; empty when all's well.
  Tap an item to act on it or bring its panel forward.
- *Tuck away.* A panel you've read and left alone for 3 minutes moves to the
  rail as its headline; click it to bring it back. Pinned, focused and
  running panels stay.

From another computer, tunnel to the server and open it:

```bash
ssh -L 8080:localhost:8080 atlas@atlas-ai      # leave this running
# then browse to http://localhost:8080/view
```

Up to four panels share the screen (one large, two side by side, three or
four in a grid; two on a phone), and each scrolls inside itself, never the
page. Past that, the panel you've looked at least recently waits as a chip in
the rail on the right: click it to bring it back, or × to dismiss it. Running
panels and confirmations are never queued, queued panels keep updating, and a
freed slot takes back the most recently queued one.

Panels stay live. Asking the same thing again refreshes the panel already on
screen instead of opening another. When an action finishes, open panels it
affects update in place: Orbit's actions return the device's confirmed state,
which is merged into any panel showing that device (the card glows, the panel
says "updated just now"); look-ups an action declares it changes (a tool's
`refreshes` list, e.g. `orbit.alias` → `orbit.aliases`) are re-run in place
when the merge didn't already cover them.

Ask about her functions ("what's the status of your functions", "tell me about
the orbit function") and Probe answers with panels: every function as a card
(running, offline with when it was last seen, or planned; tool count; recent
use; its own health note), and one function in detail (purpose, health, rate
limit, rules, and each tool with its tier and recent use). "Details" on a card
opens the detail panel.

**Follow-ups and focus.** Every panel shows its number in its top-left
corner. Each request tells Aurelia what's on screen by those numbers, plus
what earlier requests in the conversation found (even after you close their
panels), so follow-ups work: "download the third one", "summarize panel 2",
"was the lamp off?". Click a panel to focus it: it lights up, a chip in the
command bar shows which one, and what you type is about that panel unless you
name another ("lock the door" is still just a request). Click the chip's ×,
the empty space around the panels, or press Esc to let go; the next Esc clears
the screen as before. A focused action panel doesn't auto-close.

You can send a request before the last one is answered. They share one
conversation, and each answer keeps its own line under what you asked, so none
overwrites another. A conversation lasts a day after your last request
(`session.ttl_minutes`); what was on screen for each request is kept in
`ask_requests.screen`. How it works: `services/orchestrator/app/context.py`.

When an update changes the page itself, the display notices on its next
reconnect, reloads, and re-opens the look-ups that were on screen (memory shows
them at once while they refresh). No manual refresh needed.

The orchestrator serves it at `/view`; it only renders the panel events from
`/ws` and sends what you type to `/ask`. Result panels stay until you dismiss
them (how they should leave is still an open decision). Chrysalis's own smoke
tests are hidden. This is the Phase 2 minimum; the installable overlay app
comes with Constellation (Phase 7).

## Nebula: the web

Nebula runs on the server with no browser. It searches, reads pages as text
and downloads files; results show as panels, and links open in a new tab on
the device you're using (opening on a chosen screen comes with Constellation).

| Say | Does |
| --- | --- |
| `look up who Ada Lovelace was` | Aurelia searches, reads the best results, answers and names her sources |
| `search for best ramen in dallas` | A results panel (title, site, snippet, link) |
| `search youtube for frieren ost` | Videos with length, views and date; click to watch |
| `read https://…` | The page's main text, without menus or ads |
| `download https://…` | Saves to `data/downloads/` on the server, with progress in MB |
| `open https://…` | A link to open here |

**Setup (once, on the server's `.env`):**

- `BRAVE_API_KEY`: sign up at [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com),
  choose the Search plan (about $5 per 1,000 searches, with $5 of free credit a
  month; it needs a card), and create a key.
- `YOUTUBE_API_KEY`: in the [Google Cloud console](https://console.cloud.google.com),
  create a project, enable **YouTube Data API v3**, then Credentials → Create
  credentials → API key (restrict it to that API). The free daily allowance
  covers about 100 searches a day.

Add both before pushing Nebula, or run `docker compose up -d nebula` after
adding them later (keys are read when the container starts). Without a key,
that search says it isn't set up yet; reading and downloading still work.

**Safety.** Every request and every redirect is checked: Nebula only reaches
the public web, never your router, this server or Aurelia's own services
(exceptions: `network.allow_private` in its policy). Page text is data, never
instructions. Files that can run programs (`.exe`, `.sh`, …) go to
`data/downloads/quarantine/`, and nothing downloaded is opened or run
automatically. Blocked and confirm-first domains, size limits and search
settings (Brave or a self-hosted SearXNG) are in
`config/governance/functions/nebula.yaml`.

## Kepler: research and learning

Kepler reads several sources, answers with citations, and keeps what you
learn in a library that grows by topic.

| Say | Does |
| --- | --- |
| `research how do azure landing zones work` | Quick: reads 3 sources. A panel shows progress, then the answer with numbered citations, key points, where sources disagree, and the sources it read |
| `research … thoroughly` (or `in depth`, `properly`) | Plans 2 or 3 searches from different angles and reads 8 sources |
| `what did I learn about landing zones?` | Your library: matching topics and the research in them |
| `what topics have I learned?` / `show my library` | Your whole library: every topic, and your latest research |
| `show the topic Azure landing zones` | The topic: combined understanding, what's new, open questions, every piece of research and its sources |
| `explain landing zones` | An explanation at your level, built on your research when there is some, with one analogy and two check questions |
| `quiz me on landing zones` | 5 multiple-choice questions; answers stay hidden until you pick |

- **Topics grow.** New research on something you've looked into before joins
  that topic (found by name, then by meaning), and the topic's summary is
  rewritten to fold it in: "Added to Azure landing zones (2 pieces)".
- **At your level.** Answers, explanations and quizzes use what your profile
  says you're learning. Scoring 80% or more on a quiz of at least 4
  questions proposes "Solid on …" to the mailbox as something you know; you
  approve it like any other About you card.
- **Web access is Nebula's.** Kepler searches and reads through Nebula as
  hidden tasks, so Nebula's safety rules apply (public web only, pages are
  data). PDFs are read as text. Video and social pages are skipped, and at
  most 2 sources come from one site.
- **Models.** Claude writes answers, merges topics and writes quizzes, through
  the orchestrator (`llm.routes.research`, counted toward the monthly cap);
  Ollama embeds research for finding it by meaning (words are used without it).
- **Forgetting.** "Forget the azure research" (or "forget what I learned about DNS") removes that topic, its research and its Trilium note. It always asks you first, and the confirmation names the topic. A name has to match one topic by its words (never by meaning); otherwise nothing is deleted and she lists your topics.
- **Your documents.** "Read my study plan" or "read the azure networking
  notes" reads a PDF, Word (.docx) or text file from your downloads or your
  file server, found by its name or words from it (she asks which one if
  several match). Set `AURELIA_FILES` in `.env` to your file server's folder
  (e.g. `/mnt/myssd/files`), then `docker compose up -d kepler`. It's mounted
  read-only: she reads only what you ask for, by name, and never changes,
  moves or indexes anything there. Links that lead outside the folder are
  never followed. Old .doc files and scanned PDFs (images only) can't be read.
- **Always a panel.** Research, the library, topics, explanations and quizzes always open a panel; other tools can answer in a sentence instead (tools declare `display: panel`).

**Trilium (one-way).** Each topic is written to Trilium as a note under
"Aurelia · Library" (a note labelled `#aureliaLibrary`, created under
`trilium.parent_note`). She writes, you read: edits you make there are
overwritten the next time the topic grows, and nothing comes back yet (two-way
comes later, once the rules for it are settled). To connect it, in Trilium open
Options → ETAPI → Create new token, then in the server's `.env` set
`TRILIUM_TOKEN` and `TRILIUM_URL=http://lazytainer-a:8080`, and run
`docker compose up -d kepler`. Without them the library still works; Kepler's
health shows Trilium as not set up.

How Kepler reaches it: Trilium runs in the home-lab stack (`docker-home`) and
sleeps behind Lazytainer (`lazytainer-a`), which starts it when traffic arrives.
Kepler, and only Kepler, joins that stack's `docker-home_proxy` network
(`HOMELAB_NETWORK` in `.env` to change it), so the token never leaves the
machine and no port is published. It talks to `lazytainer-a` rather than
Trilium itself, so its traffic wakes Trilium; while Trilium starts, Kepler
retries for up to a minute (`trilium.wake_seconds`). The home-lab network
must exist before Kepler starts.

**If Trilium is unreachable** nothing is lost: the library in Aurelia's
database is the record, and Trilium is a copy. Research is saved as usual
(the panel says Trilium will catch up), and a forgotten topic's note is queued
for removal. Every 5 minutes (`trilium.retry_minutes`) Kepler writes whatever
Trilium missed and removes queued notes, stopping at the first failure. It only
contacts Trilium when something is waiting, so it never keeps it awake. Topics
from before Trilium was connected are copied the same way. Her health shows
"N waiting to sync" until it's caught up; `POST /trilium/sync` on Kepler runs a
round now.

Source counts, per-site limit, how close a topic must be to join it, quiz
length and Trilium settings are in `config/governance/functions/kepler.yaml`.
Code: `services/functions/kepler/`.

## Mailbox and morning digest

Aurelia infers from her own history, but nothing she infers changes anything
until you approve it. **Parallax** (a function) reads her task history about
hourly and proposes ideas to the **review mailbox**:

| Idea | When | If you approve |
| --- | --- | --- |
| Automation | You do the same thing at about the same time on at least 5 of the last 14 days (weekdays, or every day), at least half the days it could have happened | Saved; runs once Pulsar (Phase 10) exists. Look-ups run a few minutes early; physical actions keep their cancellable heads-up. Anything that needs confirmation (unlock) is never proposed |
| What matters most | A tool you use often, wait on, chase when it fails, or that acts on the home | "High": its failures stand out. "Critical": also keeps full panels 30 days |
| Keep longer | A look-up you use about 3+ times a day | Full panels kept 30 days instead of 7 |

A "3 for review" chip appears in the top bar while anything's waiting; click
it (or say "open the mailbox"). Each idea shows its evidence and exactly what
approving does, with **Approve**, **Change** (edit the time/days, level or days
first), **Not now** (back in 7 days) and **Decline** (with an optional reason; a
declined idea only returns if its evidence grows by half or more). Similar
ideas can be approved together. Deciding exists only on the display: Aurelia
has no tool for it, so she can show you her ideas but never approve them.

The **morning digest** appears once a day on your first interaction after 6 AM
(opening the display or asking anything): what's waiting, what ran without you
(releases, later automations), failures, anything unusual (e.g. "home status
checks took 3× longer this week"), and one thing noticed. Quiet days produce
none. Hour and timezone: `digest` in `config/aurelia.yaml`; Parallax's
thresholds: `config/governance/functions/parallax.yaml`. Say "any new ideas" to
have Parallax look now, "show my digest" to see today's again.

Habits need history: with only a few days of it, the mailbox stays empty.

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

## Mnemosyne: the record

Mnemosyne ("Mnemo") is part of Aurelia, not a separate service. It keeps the
record of your conversations and the facts you ask her to keep.

- **Conversations.** Every request is logged as it's answered. When a
  conversation has been quiet for 30 minutes, a background round writes its
  record: what you asked, what was decided, what was left open, and a few
  topics. Records are embedded locally (Ollama) and found by meaning: "what did
  we decide about the thermostat?", "what were we talking about Tuesday night?"
  open a Recall panel. Claude writes the summaries (`llm.routes.summarize`,
  counted toward the monthly cap); if no model can, or the budget is spent, a
  plain list of what you asked is kept instead, so nothing goes unrecorded.
  A lone command ("lock the door") isn't worth a record, and requests that ran
  `orbit.login` (SmartRent codes) are left out entirely.
- **What she knows about you.** Three layers: a core profile (up to 50
  facts) that goes with every request; everything else (up to 5,000), recalled
  by meaning when it fits what you're asking; and experience, the conversation
  records and Kepler's research. Facts come from you ("remember
  that…") or from what she noticed: every recorded conversation is also read
  for what it shows about you (how you like to be talked to, interests, goals,
  what's going on, people, what you're learning, rhythms, mood patterns), in
  the same model call as its summary. Each observation goes to the mailbox as
  an "About you" card with the moment it came from; nothing is kept until you
  approve it (or change it first). Declined ones aren't asked again. "What do
  you know about me" shows it all, grouped by kind: ☆/★ moves a fact in or
  out of the core, × deletes it. "Remember to…" is a reminder, which comes
  with Pulsar.
- **Mood.** She reads your mood from how you write and adjusts how she talks,
  in the moment. Only patterns you approve are kept ("prefers short answers
  when tired"), never a one-off mood. Health is never inferred from
  conversations (it comes later with biometrics, under its own rules).
- **Forgetting.** "Forget that conversation" or "forget the wifi fact" always
  asks you first. Forgetting a conversation empties its record and blanks what
  was said in its requests (timing and which tools ran stay, for Parallax).

What gets recorded, how long records are kept (365 days), what's left out, what
she may and may never infer, and the limits are in `config/governance/functions/mnemosyne.yaml`, which is
protected: Aurelia can't change it. Run a round now with
`POST /mnemosyne/record` (`?quiet_minutes=0` records everything, even the
conversation you're in). Code: `services/orchestrator/app/mnemosyne.py`.

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

Each release shows on the display as a **"New release detected!"** panel: the
commit message and a progress bar through testing, building, migrating, the
swap, verification and the crash-loop watch (with a countdown). It ends as
"Release live" (fades after 20 s), "Release rolled back" or "Release failed"
with the reason (stays), or "Release waiting for approval". Chrysalis reports
each step to the orchestrator (`POST /releases/progress`), which keeps the
latest in Redis, so a display that reconnects mid-release (the orchestrator may
be what's being swapped) picks the panel back up where it is.

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
- The proxy only listens on `localhost`; other devices reach it through
  Tailscale Serve (see Pairing and access). Don't publish port 8080 to the LAN.
- Every device is the owner for now: a second person would get their own user
  in the charter and their own devices.
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
scripts/pair.sh              pair a device from the server, list or revoke devices, key for this terminal
scripts/deploy.sh            zero-downtime update of one service
services/orchestrator/       the brain (FastAPI)
  app/llm/                   provider interface: Claude, Ollama (+ scripted, tests only)
  app/  migrations/  tests/
services/functions/probe/    reference function (tool: echo)
services/functions/orbit/    smart home (SmartRent, Roku TV)
services/functions/parallax/ analytics on task history → mailbox proposals, morning digest
services/functions/nebula/   the web: search, YouTube, read, download, open
services/functions/kepler/   research and learning: library, topics, explain, quiz, Trilium
data/downloads/              Nebula's downloads (not in git)
data/files/                  where Kepler looks if AURELIA_FILES isn't set (not in git)
services/core/chrysalis/     update system (protected core)
config/chrysalis.yaml        Chrysalis settings: dry run, polling, held authors, checks
```
