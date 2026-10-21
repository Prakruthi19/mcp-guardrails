from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from opsguard.guard import Outcome


@dataclass
class Step:
    tool: str
    args: dict[str, Any]
    status: str
    message: str
    approval_id: int | None
    redactions: int

    @classmethod
    def from_outcome(cls, o: Outcome) -> Step:
        return cls(o.tool, o.args, o.status, o.message, o.approval_id, o.redactions)


@dataclass
class AgentReply:
    text: str
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"reply": self.text, "steps": [asdict(s) for s in self.steps]}
