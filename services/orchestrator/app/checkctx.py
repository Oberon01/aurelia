"""While one of Swallowtail's field checks runs (Phase 10.5): what's different about the requests it makes.

A check goes through the same path as anything Kentrell asks (fast path or model, registry, governance, rate
limits), with these differences, set here and read by tasks.start_task and assistant.ask:
  - its tasks are marked trigger "check" (never his habits, never in his conversations or the record), and its
    panels go to no display (device 0, like a look-up sent elsewhere);
  - nothing is ever confirmed by a check, and earned trust never applies: a change-tier action is only asked
    for, and the check declines it;
  - a check that runs on its own ("auto": right after a release) only looks: anything that would act (a light,
    a computer, a plan, a send) is refused and the check waits for his Start;
  - the model isn't called unless the check says so (model: true, which also makes it wait for his Start).
It's a context variable, so it follows the request into every task it starts, and nothing outside a check sees it.
"""
import contextvars
from typing import Any

CHECK: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("aurelia_check", default=None)
DEVICE = 0                      # no display (the same as a look-up sent to another display)
TRIGGER = "check"
LOOK_TIERS = ("reference", "read")
# Act-tier tools that only draft (nothing happens until he confirms a read-back): fine in a check on its own.
AUTO_OK = ("pulsar.automate", "pulsar.routine")
# Read-tier tools that still change something (his library, the mailbox): never in a check on its own.
AUTO_DENY = ("kepler.research", "kepler.read_pdf", "parallax.analyze")
# Never in a check at all: a plan's or skill's steps run in their own tasks, outside the check's rules (they could
# use earned trust, ask for OKs on no display, and teach Morpho). Checks ask him to try those himself instead.
NEVER = ("admiral.plan", "morpho.run")


def state(mode: str, model: bool = False) -> dict[str, Any]:
    """mode: "auto" (only looks) or "started" (he pressed Start: it may act, still never confirms)."""
    return {"mode": mode, "model": model, "tasks": [], "holds": [], "blocked": []}


def current() -> dict[str, Any] | None:
    return CHECK.get()
