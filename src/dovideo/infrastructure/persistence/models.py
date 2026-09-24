"""Small persistence records shared by checkpoint adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One durable ``agent_checkpoints`` row.

    ``stage`` is kept as the wire string used by Java's MySQL table.  Empty
    stage is accepted by the generic record for migration/fault-injection
    tests, while ``read_stage`` rejects it as malformed when a caller asks for
    a :class:`~dovideo.domain.TaskStage`.
    """

    media_id: int
    checkpoint_name: str
    stage: str = ""
    payload: str | None = None
    updated_at: str | None = None

    @property
    def checkpoint_key(self) -> str:
        """Java/MySQL spelling for the checkpoint name."""

        return self.checkpoint_name


__all__ = ["CheckpointRecord"]
