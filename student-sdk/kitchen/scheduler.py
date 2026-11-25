"""The coursework interface.

Subclass :class:`Scheduler` and implement ``schedule``. It is called at every
scheduling point - service opening, an order arriving, a cook coming free, an
order coming back from the oven, a customer walking out, or your own alarm -
and returns a :class:`~kitchen.decision.Decision` saying which order each cook
should be working on.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from .decision import Decision
from .observation import Observation


class Scheduler:
    """Base class for a scheduling policy.

    Override ``schedule``. Optionally override ``reset`` to clear per-run
    state, and ``name``/``version`` so results identify your scheduler.
    """

    name: str = "unnamed_scheduler"
    version: str = "0"

    def schedule(self, obs: Observation) -> Decision:
        """Called at every scheduling point. Decide who cooks what.

        ``obs.reason`` says why you were called. ``obs.ready`` is the rail,
        ``obs.idle_cores`` the cooks with nothing to do. A cook you do not
        mention carries on; an order you do not mention waits.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement schedule(self, obs)")

    def reset(self, seed: int) -> None:
        """Called once before each run. Clear any per-run state here."""
        return None

    def on_run_end(self, result: Dict[str, Any]) -> None:
        """Called once after each run, with the result record."""
        return None

    @staticmethod
    def nothing() -> Decision:
        """A decision that changes nothing. A useful fallback."""
        return Decision()

    def describe(self) -> str:
        return f"{self.name} v{self.version} ({type(self).__name__})"


class FunctionScheduler(Scheduler):
    """Wraps a plain function ``f(obs) -> Decision``. Handy in tests and notebooks."""

    name = "function_scheduler"

    def __init__(self, schedule_fn: Callable[[Observation], Decision]) -> None:
        self._schedule = schedule_fn

    def schedule(self, obs: Observation) -> Decision:
        return self._schedule(obs)
