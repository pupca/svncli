from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass
class RemoteItem:
    """A file or directory entry from the SVN web client directory listing."""

    name: str
    path: str  # relative to repo root, e.g. "PolarionDPP/PolarionSVN/build.xml"
    is_dir: bool
    size: int | None = None  # bytes; None for directories
    revision: int | None = None
    last_modified: datetime | None = None
    author: str | None = None
    comment: str | None = None


class SyncOp(str, Enum):
    UPLOAD = "upload"
    UPDATE = "update"
    MKDIR = "mkdir"
    DELETE = "delete"
    DOWNLOAD = "download"
    SKIP = "skip"


@dataclass
class SyncAction:
    """A single planned sync operation."""

    op: SyncOp
    remote_path: str
    local_path: str | None = None
    reason: str = ""

    def __str__(self) -> str:
        arrow = {
            SyncOp.UPLOAD: "→",
            SyncOp.UPDATE: "↑",
            SyncOp.DOWNLOAD: "←",
            SyncOp.MKDIR: "+",
            SyncOp.DELETE: "✗",
            SyncOp.SKIP: "=",
        }[self.op]
        return f"  {arrow} {self.op.value:8s} {self.remote_path}  ({self.reason})"
