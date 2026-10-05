# My roadmap

My build phases, in order: what each builds, when it counts as done, and where it stands. Kentrell
and I work from this plan (the design doc has the full reasoning). It's written the way I'd say it
to him: "I" is me, "you" is Kentrell. Phases are built in order (except Iris, which you moved to the end), and a phase is done only after a
real-world check on your hardware: its field checks (config/checks/, run by Swallowtail once the release is
live) passing in your home. Keep it current: update a phase's status and its "built so far" list with the
release that changes it.

Names: my functions have space names (Probe, Orbit, Nebula, Kepler, Constellation…); my own core
parts have butterfly names (Chrysalis for updates, Mnemosyne for the record, Iris for sight, Admiral
for planning, Morpho for synthesis).

## 0. Foundation — Done
Builds: repo skeleton, Docker Compose, Postgres and Redis, the uv lockfile, a zero-downtime deploy script.
Done when: the whole stack comes up with one command.

## 1. The brain — Done
Builds: the orchestrator, the tool-calling loop through governance, the function registry, the task store, the fast path, Probe, and Chrysalis automatic updates.
Done when: a request calls a dummy echo tool end to end.

## 2. Orbit: the home — Done
Builds: SmartRent (locks, lights, thermostat, sensors) and the Roku TV, device names and nicknames; the surgical view that shows panels.
Done when: lock, thermostat and sensor state show as panels.

## 3. Memory — Done
Builds: pgvector, panel snapshots, hot / warm / cold / pinned retention, freshness-based reuse, nightly rollups.
Done when: a repeated task restores its last panel, labeled with its age.

## 4. Mailbox and Parallax — Done
Builds: the review mailbox (approve, modify, reject, defer) and the morning digest; Parallax infers ETAs, drift, recurrence and criticality from task history and sends them as proposals.
Done when: an inferred proposal lands in the mailbox and takes effect only after approval.

## 5. Nebula — Done
Builds: web search, fetch and download on the server, under its policy (domain lists, download limits).
Done when: a search returns result panels; a download reports progress in bytes.

## 6. Kepler and context — Done
Builds: tool results kept in the conversation, numbered panels, focus on a panel, requests in parallel, day-long sessions, Mnemosyne (the record); then Kepler: cited research through Nebula, reading PDFs and Word files (from your downloads and file server), a library grouped into topics by meaning, explanations at your level, quizzes, notes in Trilium.
Done when: "download the third one" and "summarize panel 2" work; a research question returns a cited summary, joins its topic, and can be recalled later by meaning.

## 7. Constellation — Done
Builds: per-device keys over Tailscale; my node agent on your computers, enrolled through the mailbox; the node as the machine's shell (Linux; Windows optional); content in a real browser; seeing and using the page on a screen; node health and diagnostics; power and presence; sound and media; moving things between devices; each display keeping its own panels; exam and gaming modes; app install and removal (always confirmed); the phone web app.
Done when: "open YouTube on the living room TV" opens it on that computer, on the TV's input.
Built so far:
- Pairing and per-device keys; my node agent (self-updating) with my shell on the TV PC (Wayland/Cage, scaled for the 4K TV).
- Opening sites and apps on a computer; turning the TV on and switching its input when the TV PC shows something.
- Seeing and using the page on my screen: numbered elements, click, type, keys, scroll, video controls; anything that buys, sends, posts or deletes waits for your confirmation.
- Strict sign-ins with logins saved only on the computer.
- Node health ("why is it slow"), closing a process with confirmation, and diagnostics ("what's wrong with the TV PC": read-only checks, worst first).
- Each display keeps its own panels; device roles (presentation, workstation, server, companion); sending a panel, link, note or any look-up to another display or a group, confirmed from that display's own report; "what's on my screens"; an ambient screen on presentation displays.
- Power and presence: lock, sleep, screen off/on, idle time, restart and shutdown (always confirmed; I watch for it to come back), wake-on-LAN; shutdowns that can't hang (installer --system); diagnostics say how the last shutdown went.
- Sound and media: the computer's volume, mute and which output it plays through; next, previous and what's playing for any player (Spotify, VLC, a browser tab).
- Watching a screen live in a panel ("show me the TV PC screen"), even while I'm signing in; the frames go only to your display, never to me.
- A live "Now playing" panel on your displays for anything playing (my player or Spotify), from what the player itself reports, for as long as it plays.
- Quasar step 4: Spotify on your Spotify speakers (your computers among them, through librespot, built from source with cargo where Ubuntu doesn't package it), chosen through Spotify's API and confirmed by what Spotify reports.
- Quasar step 3: YouTube in my own player: your home feed, subscriptions, Watch Later, history and search as thumbnails, read on the TV PC with its own sign-in; yt-dlp comes from my release, checksummed.
- Quasar step 2: a title by name on a Roku TV: from your library in its Jellyfin app (confirmed by Jellyfin), anything else through Roku's search in whichever of your apps has it, and the TV's own state says whether it's playing, open, or only found. (To improve later: Roku's search often won't open a title by itself, so it ends at "found".)
- Video and music with my own player (Quasar, brought forward from Phase 15): I play from services with APIs, never by working a web page. Step 1: your Jellyfin library (find, play a show where you left off and keep going, movies, albums), full screen on any computer, controlled and confirmed through the player itself, with your resume points kept in Jellyfin. The live view now uses the browser's screencast, so the TV no longer jumps.
- The Windows installer (PowerShell, no admin; Python installed for you), and modes: gaming (I keep out of the way, never the GPU; on by itself while a game runs) and exam (nothing of mine runs until a timer brings me back).
- A shared clipboard across your devices (text; a computer's clipboard, a phone's Copy card; never passwords or keys).
- Installing and removing apps (Ubuntu's packages and snaps, always confirmed, never the system's core); a computer notices a new agent within about 25 seconds and never updates mid-playback.
- My own window on your workstations and laptops (an "Aurelia" app, Ctrl+Alt+A on Windows), so you don't need a browser tab.
- No ghost confirmations: sends are tried three times and confirmed only by the display's own report (and you hear when a kept one finally shows); opening a page or an app, going home and the clipboard are checked on the computer itself, and anything I couldn't see happen is called "asked", not "done".
- Long answers open as a panel; a Raphael speaking style on trial.
- I read my own releases and this roadmap, and harder questions (why, explain, improve, compare) use a stronger model.
- The phone app: Aurelia installs to the home screen, and notifications (Web Push, encrypted end to end) for urgent mailbox items, long requests that finished, and things kept for the phone, only when it isn't already on screen.
Field check: done on your hardware (October 2026).
Noted for later: my own model, trained from your data (for now I keep using Claude, with Ollama as the fallback).
Noted for later: media on Windows workstations (the Windows installer adds mpv and Node through winget, install cards use winget there, and a tap in a computer's own window plays on that computer).

## 8. Iris — Moved to the end (after 20), at your call
Decided for when it's built: at most 10 looks per task before I stop and check with you; proven first on the Windows desk.
Builds: sight. Models that take images (screenshots, photos, later cameras); pixel-level control of a whole desktop, not just my browser, on Linux and Windows. What's on screen is data; I never type passwords; anything that buys, sends or deletes waits for your OK. Later, with the GPU box: home cameras you choose, processed locally.
Done when: on your Windows desktop I finish a task in an app I have no integration for, by seeing it, and a send button still asks first.

## 9. Admiral — Done
Builds: planning and long tasks. A big request becomes a checked plan of governed steps that survives restarts, retries or replans, asks questions through the mailbox, runs independent steps in parallel, and checks the result against what you asked. Every step that acts on a device is validated by the state the device itself reports afterwards (the page among its pages, the program among its processes, the panel in the display's report), never by "the command was sent"; a step that can't be checked is marked unconfirmed, and the plan treats it as not done until it is. Also the scheduling engine, and Morpho (synthesis): plans that worked become reusable skills, skills learn their variations, and trust is earned for low-impact actions.
Done when: "find the best computer within a $1000 budget" becomes a plan across Nebula, Kepler and Constellation that ends in a checked compare-and-contrast report; afterwards it's kept as a skill.
Built so far:
- Slice 1, plans that run: a request becomes a checked plan of up to 12 steps across my tools, each governed like a request of yours (a change-tier step waits on the plan's panel, showing exactly what it would run); independent steps run in parallel; a step is done only when its own result confirms it, and an unconfirmed one blocks what depends on it; a failed step is tried once more; plans survive a restart; "stop the plan" stops it; the end is a report checked against what you asked, with its gaps. The plan's panel shows it live, and your phone hears when it finishes or needs you.
- Slice 2, questions and changing course: a plan asks you what only you know (on its panel and as a mailbox card; "your call" lets me decide), and when a step still fails or isn't confirmed it plans another route for what hasn't run (at most twice), never around your "no". Also the budget panel: this month's model budget, its pace and when it resets, counted in your time zone.
- The scheduling engine: timers that survive restarts, for you and for me. Once, later: reminders, commands and plans ("at 11 PM lock the front door"), with the time read by code in your time zone (daylight saving included) and the command settled when you ask; a heads-up with Cancel before physical actions; a change-tier action asks your OK when it's due; each run checked by its own result; never run twice (leases), resumed after a restart, missed runs said as missed. My own timers moved onto it: memory maintenance, Mnemosyne's rounds, watching a computer come back, the librespot build, waiting plans (reminded, then expired), and step retries.
- Morpho: a plan that worked is kept as a skill (your tap, your approval, your name and phrases); the planner reaches for skills first; a skill's phrases are fast paths, and a skill that needs no model runs with it down; versions learned from a change of course or a plan that differs in one step (a choice), with rollback; tools you use together proposed daily as skills; and earned trust for exactly what you keep approving, only from your allow-list in morpho.yaml, never the coded never-list, taken back by one no or a failure. (Replaying past runs before a new version replaces the old one comes with Nova's eval set, Phase 17.)
Field check passed: "find the best computer within a $1000 budget" as a plan, kept as a skill, then "find the best monitor under $400" reusing it; a finished plan's notification opens its panel.

## 10. Pulsar — In progress (everything built; awaiting your field check)
Builds: recurring automations on Admiral's scheduling engine, routines you set up in words, and initiative: deciding when something is worth interrupting you, by severity levels you set in governance.
Done when: an approved recurring automation runs on schedule with its heads-up.
Built so far:
- Automations in your words: by the clock ("every weekday at 7 am, lock the front door", "turn off the lights every night at 11", "every 2 hours…", "on the 1st of every month at 9…", "remind me every Sunday at 6 pm to…"), the time read by code (a bare 7 is asked about, never guessed) and the command settled by its own fast path. I read it back; it's saved only when you confirm (saving is change-tier, never trusted, never offered to the model). Anything that repeats never runs now or as a one-off, whatever the phrasing ("every weeknight", "Monday, Wednesday and Friday", "11 o'clock" are read too; what I can't read goes to the model, which asks). Each run is an engine job: governed when it runs (a change-tier action asks each time, and your OK is for that run only), a heads-up with Cancel before physical actions, checked by its own result, never twice; a run that worked goes in the digest, not your phone; three failures in a row and it's paused and you're told. Pause, resume, skip the next one (Cancel on a heads-up skips that one too), delete: in words or on the automations panel.
- Routines: several commands under one name ("make a routine called goodnight: lock the front door, turn off the living room lights and turn off the TV"), each settled by its own fast path ("then" means after the one before), confirmed by you, kept as a skill that needs no model; saying its name runs it (its whole name: never a guess from part of it, and never words that already mean something else), and it can be an automation's command ("run goodnight every night at 11"). A step waiting for your OK waits only as long as that run may still happen; after that it's stopped and the run is missed, never run late.
- Events: an automation can wait for a computer coming online or going offline, or a display opening (at most once per cooldown). Doors, lights and arrivals wait for the Home Assistant bridge (Lagrange).
- Initiative: I watch for a door unlocked during quiet hours, a water leak, low batteries, the server's disk, a server (or a computer you list) down, a failed update, a paused automation, the model budget running ahead, and mailbox items waiting. Each kind's severity is yours in pulsar.yaml: critical (phone and every open display, even in quiet hours), high (phone now, or when quiet hours end), normal (a card, no phone), low (digest only). Told once while it lasts, repeated only on your interval, never after "got it", and its card taken back when it clears. The home checks run as Pulsar's own user, which may run Orbit's status and nothing else (an allow-list in charter.yaml, so tools added later can't slip past it). Everything is in the morning digest.
- Suggestions: approving one of Parallax's habit cards makes the automation now; the ones you approved before Pulsar existed are running too (I'll tell you which, once).
Everything in this phase is built. Field check: say "every day at <a few minutes from now>, turn on the living room light" (or another light), confirm the read-back, and watch it run on time with its heads-up and Cancel; then "show my automations".

## 10.5. Swallowtail — In progress (built; awaiting its first run in your home)
Builds: field checks of each release, run on the live system in your home, beside Chrysalis's tests (which run before a release deploys). Each release brings a checks file (config/checks/): the input (what you'd say, or a tool), what's expected, and how I find out what actually happened (the result, or a device's own report afterwards).
Done when: a release's checks run once it's live, the ones that act wait for your Start, and the report panel shows each check's input, expected and actual, with failures as a notice.
Built so far:
- Checks files, validated (a bad one says what's wrong); your home's names in config/checks/_home.yaml ({light}, {lock}, {tv}, {tv_computer}), a check that needs an unset one is skipped and says which.
- After a release goes live, its added or changed checks run (once, even across restarts). On their own, checks only look: nothing is confirmed (an action that asks is recorded as asked, then declined), earned trust never applies, the model isn't called, and anything that would act (a light, a computer, a send, a plan) waits for your Start, never during quiet hours. Their tasks are marked as checks: never your habits, your conversations or the record.
- After your Start: checks that act, checks that give you something to do first and watch for what it causes, yes/no questions only you can judge, and clean-up afterwards (the test automation deleted, the light turned back off).
- The report panel: one row per check (passed, failed, waiting for you, skipped), each with the input given, what was expected and what actually happened, line by line. Kept on the release ("how did the last release's checks go"). Failures are a notice (high by default, pulsar.yaml) with a rollback offered, never done on my own.
- Checks for Pulsar, for Swallowtail itself, and for each finished phase's "Done when" (0-7 and 9): "run all the checks" re-tests everything.

## 11. Lagrange — Not started
Builds: integrations of any kind without new code per service: an MCP client, OAuth connections, a Home Assistant bridge, webhooks in and out. Every new tool starts in the change tier; credentials sit in a vault the model never sees.
Done when: you connect a new service from the display with no code, and the first use asks you.

## 12. Beacon — Not started
Builds: communication: email (sending always confirmed), calendar, my own number, iMessage through a Mac bridge, notifications to your phone.
Done when: a text to my number gets a reply; the day's calendar and important email appear in the digest.

## 13. Voice — Not started
Builds: push-to-talk, then the wake word "Aurelia" through room units; local speech-to-text; a speech engine that sounds like a person talking; real conversation (I stop when you talk over me); spoken confirmations.
Done when: from across the room, "Aurelia, pause the TV" works, a spoken "yes" confirms a held action, and my voice wins your blind listening test.

## 14. Kuiper — Not started
Builds: files and storage across your computers: search, scans with live results and early exit, space used.
Done when: a file scan streams results and ends early with partial results kept.

## 15. Quasar — Started (in Phase 7)
Builds: media: find videos and music and play them where you are, with my own player and services' APIs (never by working a web page). Jellyfin, Roku apps, YouTube and Spotify are built in Phase 7; this phase adds the rest: following you from room to room, music in several rooms at once, and casting from your phone.
Done when: "play the second one on the TV" plays that search result.

## 16. Libra — Not started
Builds: bookkeeping: services, subscriptions and renewals; suggestions in the digest; never pays.
Done when: a renewal due soon appears in the digest as a suggested payment.

## 17. Nova — Not started
Builds: a code sandbox; reading my own logs to write tested patches and deploy them (as decided below); node fixes (runbooks, and fix scripts you approve and sign); an eval set from your real requests, replayed before every release.
Decided (Oct 3, your standing policy for when I change my own code; you're the watcher, the protector, the mediator):
- Rules are yours alone: governance, the charter, the policy files. I can read them and suggest changes; I never write them.
- Chrysalis and the code that enforces the rules (the tier check every task passes, the mailbox's decide, the engine's "OK when it's due", device keys and what a TV's screen may do): every change itemized line by line in plain words, with your strict confirmation. Nothing bundled; an approval covers exactly the change you saw, and any difference is a new confirmation.
- Everything else (Orbit, Pulsar, Quasar, Kepler, Constellation, the rest of me): I deploy it myself once it passes the tests and the eval set from your real requests. You're told of each change as it ships, and it rolls back by itself if anything fails after deploy.
Done when: an error found in the logs becomes a tested patch that I deploy myself (or, for Chrysalis and the enforcement code, one you confirm change by change), and rolls back on its own if it breaks something.

## 18. Orbit hardware — Not started
Builds: ESP32 nodes over MQTT; presence (motion and occupancy sensors, your phone on Wi-Fi); things that move (smart plugs, blinds, a robot vacuum, later a small robot arm).
Done when: a physical sensor reports through me.

## 19. Companion — Not started
Builds: me with you away from home: an installable phone app (talk, panels, notifications, voice), presence (home or away), later a wearable.
Done when: away from home you talk to me on your phone, see panels, get notifications, and pick up the same conversation.

## 20. Aurelia OS — Not started
Builds: a minimal Linux image that boots straight into my node shell, with Tailscale and enrollment built in.
Done when: a freshly flashed machine boots into me and enrolls with nothing typed but the approval.
