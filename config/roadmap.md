# My roadmap

My build phases, in order: what each builds, when it counts as done, and where it stands. Kentrell
and I work from this plan (the design doc has the full reasoning). It's written the way I'd say it
to him: "I" is me, "you" is Kentrell. Phases are built in order, and a phase is done only after a
real-world check on your hardware. Keep it current: update a phase's status and its "built so far"
list with the release that changes it.

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

## 7. Constellation — In progress
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
- Video and music with my own player (Quasar, brought forward from Phase 15): I play from services with APIs, never by working a web page. Step 1: your Jellyfin library (find, play a show where you left off and keep going, movies, albums), full screen on any computer, controlled and confirmed through the player itself, with your resume points kept in Jellyfin. The live view now uses the browser's screencast, so the TV no longer jumps.
- Installing and removing apps (Ubuntu's packages and snaps, always confirmed, never the system's core); a computer notices a new agent within about 25 seconds and never updates mid-playback.
- Long answers open as a panel; a Raphael speaking style on trial.
- I read my own releases and this roadmap, and harder questions (why, explain, improve, compare) use a stronger model.
Still to do: the rest of Quasar's sources (Roku apps opened straight on a show by name; YouTube, including your home feed, read with the TV PC's own sign-in; Spotify through its API with my computers as Spotify speakers), a shared clipboard, the Windows installer with exam and gaming modes, the phone web app with push notifications, and the field check.

## 8. Iris — Not started
Builds: sight. Models that take images (screenshots, photos, later cameras); pixel-level control of a whole desktop, not just my browser, on Linux and Windows. What's on screen is data; I never type passwords; anything that buys, sends or deletes waits for your OK. Later, with the GPU box: home cameras you choose, processed locally.
Done when: on your Windows desktop I finish a task in an app I have no integration for, by seeing it, and a send button still asks first.

## 9. Admiral — Not started
Builds: planning and long tasks. A big request becomes a checked plan of governed steps that survives restarts, retries or replans, asks questions through the mailbox, runs independent steps in parallel, and checks the result against what you asked. Also the scheduling engine, and Morpho (synthesis): plans that worked become reusable skills, skills learn their variations, and trust is earned for low-impact actions.
Done when: "find the best computer within a $1000 budget" becomes a plan across Nebula, Kepler and Constellation that ends in a checked compare-and-contrast report; afterwards it's kept as a skill.

## 10. Pulsar — Not started
Builds: recurring automations on Admiral's scheduling engine, routines you set up in words, and initiative: deciding when something is worth interrupting you, by severity levels you set in governance.
Done when: an approved recurring automation runs on schedule with its heads-up.

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
Builds: a code sandbox; reading my own logs to write tested patches that Chrysalis deploys after your approval; node fixes (runbooks, and fix scripts you approve and sign); an eval set from your real requests, replayed before every release.
Done when: an error found in the logs becomes a tested patch in the mailbox, deployed only after approval.

## 18. Orbit hardware — Not started
Builds: ESP32 nodes over MQTT; presence (motion and occupancy sensors, your phone on Wi-Fi); things that move (smart plugs, blinds, a robot vacuum, later a small robot arm).
Done when: a physical sensor reports through me.

## 19. Companion — Not started
Builds: me with you away from home: an installable phone app (talk, panels, notifications, voice), presence (home or away), later a wearable.
Done when: away from home you talk to me on your phone, see panels, get notifications, and pick up the same conversation.

## 20. Aurelia OS — Not started
Builds: a minimal Linux image that boots straight into my node shell, with Tailscale and enrollment built in.
Done when: a freshly flashed machine boots into me and enrolls with nothing typed but the approval.
