"""Sync engine: compares local and remote manifests using revision tracking."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path

from .models import RemoteItem, SyncAction, SyncOp

MANIFEST_DIR = Path.home() / ".svncli" / "manifests"


# ── Sync manifest (persisted state from last sync) ──────────────────


def _manifest_key(local_dir: Path) -> str:
    """Generate a unique manifest filename from the local directory path."""
    key = str(local_dir.resolve())
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _manifest_path(local_dir: Path) -> Path:
    return MANIFEST_DIR / f"{_manifest_key(local_dir)}.json"


def load_manifest(local_dir: Path) -> dict:
    """Load the sync manifest from a previous sync.

    Manifests are stored in ~/.svncli/manifests/ keyed by a hash of the
    local directory path. This keeps the user's working directory clean.

    Returns dict like:
    {
        "local_dir": "/abs/path/to/dir",
        "remote_path": "Repo/folder",
        "files": {
            "relative/path.txt": {
                "revision": 42,
                "size": 1234,
                "local_hash": "sha256:abc...",
                "local_mtime": 1710000000.0
            }
        }
    }
    """
    path = _manifest_path(local_dir)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"remote_path": "", "files": {}}


def save_manifest(local_dir: Path, remote_path: str, file_states: dict[str, dict]) -> None:
    """Save sync manifest to ~/.svncli/manifests/."""
    manifest = {
        "local_dir": str(local_dir.resolve()),
        "remote_path": remote_path,
        "files": file_states,
    }
    path = _manifest_path(local_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    path.chmod(0o600)


def _hash_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _file_state(local_path: Path, revision: int | None) -> dict:
    """Build a manifest entry for a file after sync."""
    return {
        "revision": revision,
        "size": local_path.stat().st_size,
        "local_hash": _hash_file(local_path),
        "local_mtime": local_path.stat().st_mtime,
    }


# ── Local manifest ──────────────────────────────────────────────────


def build_local_manifest(local_dir: Path, exclude: list[str] | None = None) -> dict[str, Path]:
    """Walk local directory, return {relative_posix_path: absolute_local_path}."""
    manifest: dict[str, Path] = {}
    for item in sorted(local_dir.rglob("*")):
        if item.is_file():
            rel = item.relative_to(local_dir).as_posix()
            if exclude and _matches_any(rel, exclude):
                continue
            manifest[rel] = item
    return manifest


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any of the given glob patterns."""
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.split("/")[-1], pattern) for pattern in patterns)


def _rel_path(item_path: str, prefix: str, fallback: str) -> str:
    """Extract relative path from a full remote path given a prefix."""
    return item_path[len(prefix) :] if item_path.startswith(prefix) else fallback


# ── Upload sync planning ────────────────────────────────────────────


def plan_sync_upload(
    local_dir: Path,
    remote_path: str,
    remote_items: list[RemoteItem],
    delete: bool = False,
    exclude: list[str] | None = None,
) -> list[SyncAction]:
    """Plan a local→remote sync using revision tracking."""
    local_manifest = build_local_manifest(local_dir, exclude=exclude)
    prev = load_manifest(local_dir)
    prev_files = prev.get("files", {})

    # Build remote manifest: {relative_path: RemoteItem}
    remote_prefix = remote_path.rstrip("/") + "/"
    remote_manifest: dict[str, RemoteItem] = {}
    remote_dirs: set[str] = set()
    for item in remote_items:
        rel = _rel_path(item.path, remote_prefix, item.name)
        if item.is_dir:
            remote_dirs.add(rel)
        else:
            remote_manifest[rel] = item

    actions: list[SyncAction] = []
    dirs_to_create: set[str] = set()

    for rel_path, local_path in sorted(local_manifest.items()):
        full_remote = f"{remote_path}/{rel_path}"
        prev_state = prev_files.get(rel_path)

        if rel_path in remote_manifest:
            remote_item = remote_manifest[rel_path]

            if prev_state:
                # We synced this file before — use revision + hash tracking
                remote_rev_unchanged = (
                    remote_item.revision is not None
                    and prev_state.get("revision") is not None
                    and remote_item.revision == prev_state["revision"]
                )
                local_unchanged = _is_local_unchanged(local_path, prev_state)

                if remote_rev_unchanged and local_unchanged:
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=full_remote,
                            local_path=str(local_path),
                            reason="unchanged (rev + hash match)",
                        )
                    )
                elif local_unchanged:
                    # Remote changed, local didn't — skip upload (remote is newer)
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=full_remote,
                            local_path=str(local_path),
                            reason="remote newer (rev changed, local unchanged)",
                        )
                    )
                else:
                    # Local changed — upload
                    actions.append(
                        SyncAction(
                            op=SyncOp.UPDATE,
                            remote_path=full_remote,
                            local_path=str(local_path),
                            reason="local file changed",
                        )
                    )
            else:
                # No previous state — compare by size
                local_size = local_path.stat().st_size
                if remote_item.size is not None and local_size == remote_item.size:
                    # Same size, assume same content on first sync
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=full_remote,
                            local_path=str(local_path),
                            reason="same size (first sync)",
                        )
                    )
                else:
                    actions.append(
                        SyncAction(
                            op=SyncOp.UPDATE,
                            remote_path=full_remote,
                            local_path=str(local_path),
                            reason="size differs" if remote_item.size is not None else "no previous sync state",
                        )
                    )
        else:
            # New file — ensure parent dirs exist
            parts = rel_path.split("/")
            for i in range(1, len(parts)):
                parent_rel = "/".join(parts[:i])
                if parent_rel not in remote_dirs and parent_rel not in dirs_to_create:
                    dirs_to_create.add(parent_rel)
            actions.append(
                SyncAction(
                    op=SyncOp.UPLOAD,
                    remote_path=full_remote,
                    local_path=str(local_path),
                    reason="new file",
                )
            )

    # Dirs to create (sorted by depth so parents come first)
    mkdir_actions = [
        SyncAction(op=SyncOp.MKDIR, remote_path=f"{remote_path}/{d}", reason="new directory")
        for d in sorted(dirs_to_create, key=lambda p: p.count("/"))
    ]

    # Files to delete (remote-only)
    delete_actions: list[SyncAction] = []
    if delete:
        for rel_path, item in sorted(remote_manifest.items()):
            if rel_path not in local_manifest:
                delete_actions.append(
                    SyncAction(
                        op=SyncOp.DELETE,
                        remote_path=item.path,
                        reason="not in local",
                    )
                )

    return (
        mkdir_actions
        + [a for a in actions if a.op not in (SyncOp.SKIP,)]
        + delete_actions
        + [a for a in actions if a.op == SyncOp.SKIP]
    )


# ── Download sync planning ──────────────────────────────────────────


def plan_sync_download(
    remote_path: str,
    local_dir: Path,
    remote_items: list[RemoteItem],
    delete: bool = False,
) -> list[SyncAction]:
    """Plan a remote→local sync using revision tracking."""
    remote_prefix = remote_path.rstrip("/") + "/"
    local_manifest = build_local_manifest(local_dir) if local_dir.exists() else {}
    prev = load_manifest(local_dir) if local_dir.exists() else {"files": {}}
    prev_files = prev.get("files", {})

    actions: list[SyncAction] = []

    for item in remote_items:
        if item.is_dir:
            continue
        rel = _rel_path(item.path, remote_prefix, item.name)
        local_path = local_dir / rel
        prev_state = prev_files.get(rel)

        if rel in local_manifest:
            if prev_state:
                remote_rev_unchanged = (
                    item.revision is not None
                    and prev_state.get("revision") is not None
                    and item.revision == prev_state["revision"]
                )
                local_unchanged = _is_local_unchanged(local_path, prev_state)

                if remote_rev_unchanged and local_unchanged:
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=item.path,
                            local_path=str(local_path),
                            reason="unchanged (rev + hash match)",
                        )
                    )
                elif remote_rev_unchanged:
                    # Remote unchanged, local modified — skip download (local is newer)
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=item.path,
                            local_path=str(local_path),
                            reason="local newer (local changed, rev unchanged)",
                        )
                    )
                else:
                    actions.append(
                        SyncAction(
                            op=SyncOp.DOWNLOAD,
                            remote_path=item.path,
                            local_path=str(local_path),
                            reason="remote revision changed",
                        )
                    )
            else:
                # No previous state — compare by size
                local_size = local_path.stat().st_size
                if item.size is not None and local_size == item.size:
                    actions.append(
                        SyncAction(
                            op=SyncOp.SKIP,
                            remote_path=item.path,
                            local_path=str(local_path),
                            reason="same size (first sync)",
                        )
                    )
                else:
                    actions.append(
                        SyncAction(
                            op=SyncOp.DOWNLOAD,
                            remote_path=item.path,
                            local_path=str(local_path),
                            reason="size differs",
                        )
                    )
        else:
            actions.append(
                SyncAction(
                    op=SyncOp.DOWNLOAD,
                    remote_path=item.path,
                    local_path=str(local_path),
                    reason="new file",
                )
            )

    # Delete local files not in remote
    if delete:
        remote_files = set()
        for item in remote_items:
            if not item.is_dir:
                rel = _rel_path(item.path, remote_prefix, item.name)
                remote_files.add(rel)
        for rel_path in sorted(local_manifest):
            if rel_path not in remote_files:
                actions.append(
                    SyncAction(
                        op=SyncOp.DELETE,
                        remote_path="",
                        local_path=str(local_dir / rel_path),
                        reason="not in remote",
                    )
                )

    return actions


# ── Helpers ─────────────────────────────────────────────────────────


def _is_local_unchanged(local_path: Path, prev_state: dict) -> bool:
    """Check if a local file is unchanged since last sync.

    Fast path: check mtime + size first. If those match, skip hashing.
    Slow path: if mtime changed but size is same, compare hashes.
    """
    try:
        stat = local_path.stat()
    except OSError:
        return False

    prev_mtime = prev_state.get("local_mtime")
    prev_size = prev_state.get("size")

    if prev_mtime is not None and prev_size is not None and stat.st_mtime == prev_mtime and stat.st_size == prev_size:
        return True

    # mtime changed or missing — fall back to hash
    prev_hash = prev_state.get("local_hash")
    if prev_hash:
        return _hash_file(local_path) == prev_hash

    return False
