"""Tests for the sync engine — manifest tracking, diff planning, and exclude patterns."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from svncli.models import RemoteItem, SyncOp
from svncli.sync import (
    _hash_file,
    _is_local_unchanged,
    _manifest_path,
    _matches_any,
    build_local_manifest,
    load_manifest,
    plan_sync_download,
    plan_sync_upload,
    save_manifest,
)

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_manifests(tmp_path_factory, monkeypatch):
    """Store manifests in a separate temp dir, not ~/.svncli/manifests/."""
    manifest_dir = tmp_path_factory.mktemp("svncli_manifests")
    monkeypatch.setattr("svncli.sync.MANIFEST_DIR", manifest_dir)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_remote(name: str, path: str, size: int, revision: int, is_dir: bool = False) -> RemoteItem:
    return RemoteItem(
        name=name,
        path=path,
        is_dir=is_dir,
        size=None if is_dir else size,
        revision=revision,
        last_modified=datetime(2026, 3, 17, 12, 0),
        author="test",
    )


def _write_file(path: Path, content: str = "hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ── build_local_manifest ────────────────────────────────────────────


class TestBuildLocalManifest:
    def test_basic(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "sub" / "b.txt")
        manifest = build_local_manifest(tmp_path)
        assert set(manifest.keys()) == {"a.txt", "sub/b.txt"}

    def test_includes_all_files(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "data.json")
        manifest = build_local_manifest(tmp_path)
        assert set(manifest.keys()) == {"a.txt", "data.json"}

    def test_exclude_patterns(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "b.log")
        _write_file(tmp_path / "sub" / "c.log")
        manifest = build_local_manifest(tmp_path, exclude=["*.log"])
        assert set(manifest.keys()) == {"a.txt"}

    def test_exclude_directory_glob(self, tmp_path: Path):
        _write_file(tmp_path / "keep.txt")
        # Note: in some sandboxes, writing under a directory literally named ".git"
        # is blocked for safety. Use a different directory name while still testing
        # that a directory-glob exclude works.
        _write_file(tmp_path / ".git_test" / "config")
        _write_file(tmp_path / ".git_test" / "objects" / "abc")
        manifest = build_local_manifest(tmp_path, exclude=[".git_test/*"])
        assert set(manifest.keys()) == {"keep.txt"}

    def test_empty_dir(self, tmp_path: Path):
        manifest = build_local_manifest(tmp_path)
        assert manifest == {}


# ── _matches_any ────────────────────────────────────────────────────


class TestMatchesAny:
    def test_filename_match(self):
        assert _matches_any("path/to/file.log", ["*.log"])

    def test_no_match(self):
        assert not _matches_any("path/to/file.txt", ["*.log"])

    def test_full_path_match(self):
        assert _matches_any(".git_test/config", [".git_test/*"])

    def test_multiple_patterns(self):
        assert _matches_any("file.pyc", ["*.log", "*.pyc"])


# ── Manifest persistence ────────────────────────────────────────────


class TestManifest:
    def test_save_and_load(self, tmp_path: Path):
        local_dir = tmp_path / "project"
        local_dir.mkdir()
        states = {
            "a.txt": {"revision": 5, "size": 100, "local_hash": "sha256:abc", "local_mtime": 1.0},
        }
        save_manifest(local_dir, "Repo/folder", states)
        loaded = load_manifest(local_dir)
        assert loaded["remote_path"] == "Repo/folder"
        assert loaded["files"]["a.txt"]["revision"] == 5
        assert loaded["local_dir"] == str(local_dir.resolve())

    def test_load_missing(self, tmp_path: Path):
        loaded = load_manifest(tmp_path)
        assert loaded == {"remote_path": "", "files": {}}

    def test_load_corrupt(self, tmp_path: Path):
        local_dir = tmp_path / "project"
        local_dir.mkdir()
        manifest_path = _manifest_path(local_dir)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("not json{{{")
        loaded = load_manifest(local_dir)
        assert loaded == {"remote_path": "", "files": {}}

    def test_manifest_stored_outside_project(self, tmp_path: Path):
        """Manifest should NOT be in the project directory."""
        local_dir = tmp_path / "project"
        local_dir.mkdir()
        save_manifest(local_dir, "Repo/folder", {"a.txt": {}})
        # No manifest file in the project dir
        assert not any(f.name.endswith(".json") for f in local_dir.iterdir())
        # Manifest exists somewhere in the test manifests dir
        manifest_dir = _manifest_path(local_dir).parent
        assert manifest_dir.exists()
        assert any(f.name.endswith(".json") for f in manifest_dir.iterdir())


# ── _hash_file ──────────────────────────────────────────────────────


class TestHashFile:
    def test_deterministic(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello world")
        h1 = _hash_file(f)
        h2 = _hash_file(f)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_content(self, tmp_path: Path):
        f1 = _write_file(tmp_path / "a.txt", "hello")
        f2 = _write_file(tmp_path / "b.txt", "world")
        assert _hash_file(f1) != _hash_file(f2)


# ── _is_local_unchanged ────────────────────────────────────────────


class TestIsLocalUnchanged:
    def test_same_mtime_and_size(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        stat = f.stat()
        prev = {"local_mtime": stat.st_mtime, "size": stat.st_size, "local_hash": _hash_file(f)}
        assert _is_local_unchanged(f, prev)

    def test_content_changed(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        prev = {"local_mtime": 0.0, "size": 5, "local_hash": "sha256:wrong"}
        assert not _is_local_unchanged(f, prev)

    def test_file_deleted(self, tmp_path: Path):
        prev = {"local_mtime": 1.0, "size": 5, "local_hash": "sha256:abc"}
        assert not _is_local_unchanged(tmp_path / "gone.txt", prev)

    def test_mtime_changed_but_hash_same(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        h = _hash_file(f)
        # mtime won't match, but hash will
        prev = {"local_mtime": 0.0, "size": 5, "local_hash": h}
        assert _is_local_unchanged(f, prev)


# ── plan_sync_upload ────────────────────────────────────────────────


class TestPlanSyncUpload:
    def test_new_files_no_remote(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "sub" / "b.txt")
        actions = plan_sync_upload(tmp_path, "Repo/target", remote_items=[], delete=False)
        ops = [(a.op, a.remote_path) for a in actions if a.op != SyncOp.SKIP]
        assert (SyncOp.MKDIR, "Repo/target/sub") in ops
        assert (SyncOp.UPLOAD, "Repo/target/a.txt") in ops
        assert (SyncOp.UPLOAD, "Repo/target/sub/b.txt") in ops

    def test_skip_unchanged_with_manifest(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        remote = [_make_remote("a.txt", "Repo/target/a.txt", size=5, revision=10)]
        # Save manifest as if we synced before at rev 10
        save_manifest(
            tmp_path,
            "Repo/target",
            {
                "a.txt": {
                    "revision": 10,
                    "size": f.stat().st_size,
                    "local_hash": _hash_file(f),
                    "local_mtime": f.stat().st_mtime,
                }
            },
        )
        actions = plan_sync_upload(tmp_path, "Repo/target", remote, delete=False)
        assert all(a.op == SyncOp.SKIP for a in actions)

    def test_upload_when_local_changed(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt", "changed content")
        remote = [_make_remote("a.txt", "Repo/target/a.txt", size=5, revision=10)]
        # Manifest says we synced "hello" at rev 10
        save_manifest(
            tmp_path,
            "Repo/target",
            {
                "a.txt": {
                    "revision": 10,
                    "size": 5,
                    "local_hash": "sha256:old",
                    "local_mtime": 0.0,
                }
            },
        )
        actions = plan_sync_upload(tmp_path, "Repo/target", remote, delete=False)
        updates = [a for a in actions if a.op == SyncOp.UPDATE]
        assert len(updates) == 1
        assert updates[0].reason == "local file changed"

    def test_skip_when_remote_newer(self, tmp_path: Path):
        """If remote revision changed but local hasn't, skip (remote is newer)."""
        f = _write_file(tmp_path / "a.txt", "hello")
        remote = [_make_remote("a.txt", "Repo/target/a.txt", size=5, revision=15)]
        save_manifest(
            tmp_path,
            "Repo/target",
            {
                "a.txt": {
                    "revision": 10,  # remote was at 10, now at 15
                    "size": f.stat().st_size,
                    "local_hash": _hash_file(f),
                    "local_mtime": f.stat().st_mtime,
                }
            },
        )
        actions = plan_sync_upload(tmp_path, "Repo/target", remote, delete=False)
        skips = [a for a in actions if a.op == SyncOp.SKIP]
        assert len(skips) == 1
        assert "remote newer" in skips[0].reason

    def test_delete_remote_only(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        remote = [
            _make_remote("a.txt", "Repo/target/a.txt", size=5, revision=10),
            _make_remote("orphan.txt", "Repo/target/orphan.txt", size=99, revision=5),
        ]
        actions = plan_sync_upload(tmp_path, "Repo/target", remote, delete=True)
        deletes = [a for a in actions if a.op == SyncOp.DELETE]
        assert len(deletes) == 1
        assert deletes[0].remote_path == "Repo/target/orphan.txt"

    def test_no_delete_without_flag(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        remote = [
            _make_remote("a.txt", "Repo/target/a.txt", size=5, revision=10),
            _make_remote("orphan.txt", "Repo/target/orphan.txt", size=99, revision=5),
        ]
        actions = plan_sync_upload(tmp_path, "Repo/target", remote, delete=False)
        deletes = [a for a in actions if a.op == SyncOp.DELETE]
        assert len(deletes) == 0

    def test_exclude_pattern(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "debug.log")
        actions = plan_sync_upload(tmp_path, "Repo/target", remote_items=[], exclude=["*.log"])
        paths = [a.remote_path for a in actions if a.op == SyncOp.UPLOAD]
        assert "Repo/target/a.txt" in paths
        assert "Repo/target/debug.log" not in paths

    def test_first_sync_same_size_skips(self, tmp_path: Path):
        """First sync with no manifest: same size → skip."""
        f = _write_file(tmp_path / "a.txt", "hello")
        size = f.stat().st_size
        remote = [_make_remote("a.txt", "Repo/target/a.txt", size=size, revision=10)]
        actions = plan_sync_upload(tmp_path, "Repo/target", remote)
        skips = [a for a in actions if a.op == SyncOp.SKIP]
        assert len(skips) == 1

    def test_mkdir_ordering(self, tmp_path: Path):
        """Parent dirs must be created before children."""
        _write_file(tmp_path / "a" / "b" / "c.txt")
        actions = plan_sync_upload(tmp_path, "Repo/target", remote_items=[])
        mkdirs = [a for a in actions if a.op == SyncOp.MKDIR]
        uploads = [a for a in actions if a.op == SyncOp.UPLOAD]
        # mkdirs come before uploads
        if mkdirs and uploads:
            all_actions = [a for a in actions if a.op != SyncOp.SKIP]
            mkdir_idx = max(all_actions.index(m) for m in mkdirs)
            upload_idx = min(all_actions.index(u) for u in uploads)
            assert mkdir_idx < upload_idx


# ── plan_sync_download ──────────────────────────────────────────────


class TestPlanSyncDownload:
    def test_new_remote_files(self, tmp_path: Path):
        remote = [
            _make_remote("a.txt", "Repo/src/a.txt", size=100, revision=5),
            _make_remote("b.txt", "Repo/src/b.txt", size=200, revision=6),
        ]
        actions = plan_sync_download("Repo/src", tmp_path, remote)
        downloads = [a for a in actions if a.op == SyncOp.DOWNLOAD]
        assert len(downloads) == 2

    def test_skip_unchanged_with_manifest(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        remote = [_make_remote("a.txt", "Repo/src/a.txt", size=5, revision=10)]
        save_manifest(
            tmp_path,
            "Repo/src",
            {
                "a.txt": {
                    "revision": 10,
                    "size": f.stat().st_size,
                    "local_hash": _hash_file(f),
                    "local_mtime": f.stat().st_mtime,
                }
            },
        )
        actions = plan_sync_download("Repo/src", tmp_path, remote)
        assert all(a.op == SyncOp.SKIP for a in actions)

    def test_download_when_remote_changed(self, tmp_path: Path):
        f = _write_file(tmp_path / "a.txt", "hello")
        remote = [_make_remote("a.txt", "Repo/src/a.txt", size=5, revision=15)]
        save_manifest(
            tmp_path,
            "Repo/src",
            {
                "a.txt": {
                    "revision": 10,  # was 10, now 15
                    "size": f.stat().st_size,
                    "local_hash": _hash_file(f),
                    "local_mtime": f.stat().st_mtime,
                }
            },
        )
        actions = plan_sync_download("Repo/src", tmp_path, remote)
        downloads = [a for a in actions if a.op == SyncOp.DOWNLOAD]
        assert len(downloads) == 1
        assert "revision changed" in downloads[0].reason

    def test_skip_when_local_modified(self, tmp_path: Path):
        """If local changed but remote didn't, skip download (local is newer)."""
        _write_file(tmp_path / "a.txt", "modified locally")
        remote = [_make_remote("a.txt", "Repo/src/a.txt", size=5, revision=10)]
        save_manifest(
            tmp_path,
            "Repo/src",
            {
                "a.txt": {
                    "revision": 10,
                    "size": 5,
                    "local_hash": "sha256:old",
                    "local_mtime": 0.0,
                }
            },
        )
        actions = plan_sync_download("Repo/src", tmp_path, remote)
        skips = [a for a in actions if a.op == SyncOp.SKIP]
        assert len(skips) == 1
        assert "local newer" in skips[0].reason

    def test_delete_local_only(self, tmp_path: Path):
        _write_file(tmp_path / "a.txt")
        _write_file(tmp_path / "orphan.txt")
        remote = [_make_remote("a.txt", "Repo/src/a.txt", size=5, revision=10)]
        actions = plan_sync_download("Repo/src", tmp_path, remote, delete=True)
        deletes = [a for a in actions if a.op == SyncOp.DELETE]
        assert len(deletes) == 1
        assert "orphan.txt" in deletes[0].local_path

    def test_skips_directories(self, tmp_path: Path):
        remote = [
            _make_remote("subdir", "Repo/src/subdir", size=0, revision=1, is_dir=True),
            _make_remote("a.txt", "Repo/src/a.txt", size=5, revision=10),
        ]
        actions = plan_sync_download("Repo/src", tmp_path, remote)
        # Only the file, not the dir
        assert len(actions) == 1
        assert actions[0].remote_path == "Repo/src/a.txt"
