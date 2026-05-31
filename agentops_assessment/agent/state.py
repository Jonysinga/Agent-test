from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepState:
    step_id: str
    tool_name: str
    status: str = "pending"
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class RunState:
    run_id: str
    status: str = "pending"
    steps: list[StepState] = field(default_factory=list)
    result: dict[str, Any] | None = None
    token_cost: int = 0  # FakeLLM 每步 completion_tokens=24，由 Executor 累加


class InMemoryRunStateStore:
    def __init__(self) -> None:
        self._states: dict[str, RunState] = {}

    def save(self, state: RunState) -> None:
        self._states[state.run_id] = state

    def get(self, run_id: str) -> RunState | None:
        return self._states.get(run_id)
