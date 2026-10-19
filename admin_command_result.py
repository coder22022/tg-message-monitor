from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdminCommandResult:
    text: str
    document: Path | None = None
    documents: tuple[Path, ...] = ()
    reply_messages: tuple[str, ...] = ()

    def attachment_paths(self) -> tuple[Path, ...]:
        if self.documents:
            return self.documents
        if self.document is not None:
            return (self.document,)
        return ()
