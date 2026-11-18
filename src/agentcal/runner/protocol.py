"""Step/agent contract shared by parent, worker and tests."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    kind: str
    target: str | None = None


class AgentError(RuntimeError):
    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class


class Agent:
    name = "abstract"

    def steps(self, task: dict) -> list[Step]:
        raise NotImplementedError

    def perform(self, step: Step, ctx: dict) -> str | None:
        raise NotImplementedError
