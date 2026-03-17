from __future__ import annotations

import sys
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


def log_verbose(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg, file=sys.stderr)
