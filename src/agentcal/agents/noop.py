from __future__ import annotations

from ..runner.protocol import Agent, Step


class NullAgent(Agent):
    """M0/M1 diagnostics: proves scheduling, journaling and projection without side effects."""

    name = "noop"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    def steps(self, task: dict) -> list[Step]:
        return [Step("prepare"), Step("execute"), Step("finalize")]

    def perform(self, step: Step, ctx: dict) -> str | None:
        return None
