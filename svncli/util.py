from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote


def encode_svn_path(path: str) -> str:
    """Encode a repository path for use in the url= query parameter.

    The SVN web client expects forward slashes encoded as %2F.
    """
    # Normalize: strip leading/trailing slashes, collapse doubles
    parts = [p for p in path.split("/") if p]
    return "%2F".join(quote(p, safe="") for p in parts)


def normalize_remote_path(path: str) -> str:
    """Normalize a remote path: strip leading/trailing slashes."""
    return "/".join(p for p in path.split("/") if p)


def fmt_size(size: int | None) -> str:
    if size is None:
        return "<DIR>"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def split_remote_path(remote_path: str) -> tuple[str, str]:
    """Split a remote path into (parent, name). Returns ("", name) for root-level items."""
    parts = remote_path.rsplit("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


@dataclass
class ParsedPath:
    """A parsed path — either local or remote."""

    server: str | None  # e.g. "https://host.example.com", None for local
    path: str  # remote path or local path

    @property
    def is_local(self) -> bool:
        return self.server is None

    @property
    def is_remote(self) -> bool:
        return self.server is not None

    def __str__(self) -> str:
        if self.server:
            return f"{self.server}:{self.path}"
        return self.path


# Matches https://host or https://host:port followed by :remote/path
_REMOTE_RE = re.compile(r"^(https?://[^/:]+(?::\d+)?):(.*)$")


def parse_path(raw: str) -> ParsedPath:
    """Parse a path string into a ParsedPath.

    Formats:
        https://server.com:Repo/path  → remote (server + path)
        /local/path, ./path, ~/path   → local
    """
    m = _REMOTE_RE.match(raw)
    if m:
        return ParsedPath(server=m.group(1), path=normalize_remote_path(m.group(2)))

    if raw.startswith(("/", ".", "~")):
        expanded = os.path.expanduser(raw)
        return ParsedPath(server=None, path=expanded)

    # Doesn't look like a URL or local path
    raise ValueError(
        f"Cannot parse path: {raw}\n"
        "Remote paths must include the server: https://server.com:Repo/path\n"
        "Local paths must start with / ./ or ~/"
    )


def log_verbose(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg, file=sys.stderr)
