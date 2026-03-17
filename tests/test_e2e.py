"""End-to-end tests against a real SVN Web Client instance.

Usage:
    export SVNCLI_SERVER="https://your-server.example.com"
    export SVNCLI_E2E_ROOT="Popelak"
    python -m pytest tests/test_e2e.py -v -s

The test creates a temporary folder under SVNCLI_E2E_ROOT, runs all operations,
and cleans up after itself.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from svncli.client import SVNWebClient, extract_browser_cookies
from svncli.models import SyncOp
from svncli.sync import (
    _file_state,
    build_local_manifest,
    plan_sync_download,
    plan_sync_upload,
    save_manifest,
)

# ── Fixtures ─────────────────────────────────────────────────────────


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"{name} not set")
    return val


@pytest.fixture(scope="module")
def client() -> SVNWebClient:
    server = _get_env("SVNCLI_SERVER")
    domain = server.split("//")[1].split("/")[0].split(":")[0]
    cookie = extract_browser_cookies(domain, "chrome")
    return SVNWebClient(server, cookie, verify_ssl=False)


@pytest.fixture(scope="module")
def e2e_root() -> str:
    return _get_env("SVNCLI_E2E_ROOT")


@pytest.fixture(scope="module")
def test_folder(client: SVNWebClient, e2e_root: str):
    """Create a unique test folder, yield its path, then delete it."""
    folder_name = f"_svncli_test_{uuid.uuid4().hex[:8]}"
    folder_path = f"{e2e_root}/{folder_name}"
    client.mkdir(folder_path)
    yield folder_path
    # Cleanup: delete the test folder
    try:
        client.delete_items(e2e_root, [folder_name])
    except Exception as e:
        print(f"\n⚠ Cleanup failed for {folder_path}: {e}")


# ── Tests (run in order) ────────────────────────────────────────────


class TestE2E:
    """Tests run in declaration order against the live server."""

    def test_01_ls_empty_folder(self, client: SVNWebClient, test_folder: str):
        items = client.ls(test_folder)
        assert items == []

    def test_02_mkdir(self, client: SVNWebClient, test_folder: str):
        client.mkdir(f"{test_folder}/subdir")
        items = client.ls(test_folder)
        names = [i.name for i in items]
        assert "subdir" in names
        subdir = next(i for i in items if i.name == "subdir")
        assert subdir.is_dir is True

    def test_03_upload_file(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        local_file = tmp_path / "hello.txt"
        local_file.write_text("hello world")
        client.upload_file(f"{test_folder}/hello.txt", local_file)

        items = client.ls(test_folder)
        names = [i.name for i in items]
        assert "hello.txt" in names
        hello = next(i for i in items if i.name == "hello.txt")
        assert hello.is_dir is False
        assert hello.size == 11  # len("hello world")

    def test_04_download_file(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        dest = tmp_path / "downloaded.txt"
        client.download_file(f"{test_folder}/hello.txt", dest)
        assert dest.read_text() == "hello world"

    def test_05_update_file(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        # Get revision before update
        items_before = client.ls(test_folder)
        hello_before = next(i for i in items_before if i.name == "hello.txt")
        rev_before = hello_before.revision

        local_file = tmp_path / "hello.txt"
        local_file.write_text("hello world updated")
        client.update_file(f"{test_folder}/hello.txt", local_file)

        items_after = client.ls(test_folder)
        hello_after = next(i for i in items_after if i.name == "hello.txt")
        assert hello_after.size == 19  # len("hello world updated")
        assert hello_after.revision > rev_before

    def test_06_upload_to_subdir(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        local_file = tmp_path / "nested.txt"
        local_file.write_text("nested content")
        client.upload_file(f"{test_folder}/subdir/nested.txt", local_file)

        items = client.ls(f"{test_folder}/subdir")
        names = [i.name for i in items]
        assert "nested.txt" in names

    def test_07_ls_recursive(self, client: SVNWebClient, test_folder: str):
        all_items = client.ls_recursive(test_folder)
        paths = [i.path for i in all_items]
        assert any("hello.txt" in p for p in paths)
        assert any("nested.txt" in p for p in paths)
        assert any("subdir" in p for p in paths)

    def test_08_sync_upload_dry_run(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        """Sync a local folder to remote — dry run should plan actions."""
        local_dir = tmp_path / "synctest"
        local_dir.mkdir()
        (local_dir / "new_file.txt").write_text("brand new")
        (local_dir / "sub2").mkdir()
        (local_dir / "sub2" / "deep.txt").write_text("deep content")

        sync_target = f"{test_folder}/synced"
        # Remote doesn't exist yet, so ls_recursive will fail
        try:
            remote_items = client.ls_recursive(sync_target)
        except Exception:
            remote_items = []

        actions = plan_sync_upload(local_dir, sync_target, remote_items)
        ops = [a.op for a in actions if a.op != SyncOp.SKIP]
        assert SyncOp.UPLOAD in ops

    def test_09_sync_upload_execute(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        """Actually sync a local folder to remote."""
        local_dir = tmp_path / "synctest"
        local_dir.mkdir()
        (local_dir / "file_a.txt").write_text("aaa")
        (local_dir / "file_b.txt").write_text("bbb")

        sync_target = f"{test_folder}/synced"
        client.mkdir(sync_target)

        remote_items = client.ls_recursive(sync_target)
        actions = plan_sync_upload(local_dir, sync_target, remote_items)

        # Execute uploads
        for action in actions:
            if action.op == SyncOp.UPLOAD:
                client.upload_file(action.remote_path, Path(action.local_path))
            elif action.op == SyncOp.MKDIR:
                client.mkdir(action.remote_path)

        # Verify
        items = client.ls(sync_target)
        names = sorted(i.name for i in items)
        assert "file_a.txt" in names
        assert "file_b.txt" in names

    def test_10_sync_skip_unchanged(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        """After syncing, a second sync should skip everything."""
        local_dir = tmp_path / "synctest2"
        local_dir.mkdir()
        f = local_dir / "stable.txt"
        f.write_text("stable content")

        sync_target = f"{test_folder}/synced2"
        client.mkdir(sync_target)

        # First sync
        remote_items = client.ls_recursive(sync_target)
        actions = plan_sync_upload(local_dir, sync_target, remote_items)
        for action in actions:
            if action.op == SyncOp.UPLOAD:
                client.upload_file(action.remote_path, Path(action.local_path))

        # Save manifest
        remote_items = client.ls_recursive(sync_target)
        remote_prefix = sync_target + "/"
        remote_revisions = {}
        for item in remote_items:
            if not item.is_dir:
                rel = item.path[len(remote_prefix) :] if item.path.startswith(remote_prefix) else item.name
                remote_revisions[rel] = item.revision
        local_manifest = build_local_manifest(local_dir)
        file_states = {}
        for rel_path, local_path in local_manifest.items():
            file_states[rel_path] = _file_state(local_path, remote_revisions.get(rel_path))
        save_manifest(local_dir, sync_target, file_states)

        # Second sync — should all be skips
        remote_items = client.ls_recursive(sync_target)
        actions2 = plan_sync_upload(local_dir, sync_target, remote_items)
        non_skip = [a for a in actions2 if a.op != SyncOp.SKIP]
        assert len(non_skip) == 0, f"Expected no actions, got: {non_skip}"

    def test_11_sync_detects_local_change(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        """After modifying a local file, sync should detect it."""
        local_dir = tmp_path / "synctest3"
        local_dir.mkdir()
        f = local_dir / "mutable.txt"
        f.write_text("original")

        sync_target = f"{test_folder}/synced3"
        client.mkdir(sync_target)

        # First sync + manifest
        remote_items = client.ls_recursive(sync_target)
        actions = plan_sync_upload(local_dir, sync_target, remote_items)
        for action in actions:
            if action.op == SyncOp.UPLOAD:
                client.upload_file(action.remote_path, Path(action.local_path))

        remote_items = client.ls_recursive(sync_target)
        remote_prefix = sync_target + "/"
        remote_revisions = {}
        for item in remote_items:
            if not item.is_dir:
                rel = item.path[len(remote_prefix) :] if item.path.startswith(remote_prefix) else item.name
                remote_revisions[rel] = item.revision
        local_manifest = build_local_manifest(local_dir)
        file_states = {}
        for rel_path, local_path in local_manifest.items():
            file_states[rel_path] = _file_state(local_path, remote_revisions.get(rel_path))
        save_manifest(local_dir, sync_target, file_states)

        # Modify the file
        time.sleep(0.1)  # ensure mtime changes
        f.write_text("modified content!!!")

        # Second sync — should detect change
        remote_items = client.ls_recursive(sync_target)
        actions2 = plan_sync_upload(local_dir, sync_target, remote_items)
        updates = [a for a in actions2 if a.op == SyncOp.UPDATE]
        assert len(updates) == 1
        assert "mutable.txt" in updates[0].remote_path

    def test_12_download_sync(self, client: SVNWebClient, test_folder: str, tmp_path: Path):
        """Download sync should pull remote files to local."""
        local_dir = tmp_path / "download_target"
        # test_folder has hello.txt, subdir/nested.txt from earlier tests
        remote_items = client.ls_recursive(test_folder)
        file_items = [i for i in remote_items if not i.is_dir]

        actions = plan_sync_download(test_folder, local_dir, remote_items)
        downloads = [a for a in actions if a.op == SyncOp.DOWNLOAD]
        assert len(downloads) == len(file_items)

        # Execute downloads
        for action in downloads:
            dest = Path(action.local_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(action.remote_path, dest)

        # Verify files exist locally
        local_files = build_local_manifest(local_dir)
        assert len(local_files) == len(file_items)

    def test_13_delete_file(self, client: SVNWebClient, test_folder: str):
        """Delete a single file from remote."""
        # hello.txt should still exist
        items_before = client.ls(test_folder)
        assert any(i.name == "hello.txt" for i in items_before)

        client.delete_items(test_folder, ["hello.txt"])

        items_after = client.ls(test_folder)
        assert not any(i.name == "hello.txt" for i in items_after)

    def test_14_delete_subfolder(self, client: SVNWebClient, test_folder: str):
        """Delete a subfolder from remote."""
        items_before = client.ls(test_folder)
        assert any(i.name == "subdir" for i in items_before)

        client.delete_items(test_folder, ["subdir"])

        items_after = client.ls(test_folder)
        assert not any(i.name == "subdir" for i in items_after)
