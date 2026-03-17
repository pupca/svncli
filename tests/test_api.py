"""Tests for the PolarionSVNClient programmatic API."""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

import pytest

from svncli import PolarionSVNClient, RemoteItem, SVNWebClientError, SyncAction, SyncOp


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"{name} not set")
    return val


@pytest.fixture(scope="module")
def server() -> str:
    return _get_env("SVNCLI_SERVER_A")


@pytest.fixture(scope="module")
def root() -> str:
    return _get_env("SVNCLI_ROOT_A")


@pytest.fixture(scope="module")
def client(server: str) -> PolarionSVNClient:
    c = PolarionSVNClient(verify_ssl=False)
    c.login(server)
    return c


@pytest.fixture()
def remote_folder(client: PolarionSVNClient, server: str, root: str):
    """Create a unique test folder, yield server:path, clean up."""
    name = f"_api_{uuid.uuid4().hex[:8]}"
    full = f"{server}:{root}/{name}"
    client.mkdir(full)
    yield full
    with contextlib.suppress(Exception):
        client.rm(full)


# ── Login / Auth ─────────────────────────────────────────────────────


class TestAuth:
    def test_login_auto(self, server: str):
        c = PolarionSVNClient(verify_ssl=False)
        c.login(server)
        # Should be able to ls after login
        items = c.ls(f"{server}:")
        assert isinstance(items, list)

    def test_not_authenticated_raises(self):
        c = PolarionSVNClient(verify_ssl=False)
        with pytest.raises(SVNWebClientError, match="Not authenticated"):
            c.ls("https://no-such-server-xyz.example.com:Repo")

    def test_logout(self, server: str):
        c = PolarionSVNClient(verify_ssl=False)
        c.login(server)
        c.logout(server)
        # After logout, should still work if browser cookies exist
        # (logout only removes saved cookies, browser extraction still works)


# ── ls ───────────────────────────────────────────────────────────────


class TestLs:
    def test_ls_returns_remote_items(self, client: PolarionSVNClient, server: str, root: str):
        items = client.ls(f"{server}:{root}")
        assert isinstance(items, list)
        assert all(isinstance(i, RemoteItem) for i in items)

    def test_ls_empty_folder(self, client: PolarionSVNClient, remote_folder: str):
        items = client.ls(remote_folder)
        assert items == []

    def test_ls_recursive(self, client: PolarionSVNClient, server: str, root: str):
        items = client.ls(f"{server}:{root}", recursive=True)
        assert isinstance(items, list)
        assert len(items) >= len(client.ls(f"{server}:{root}"))

    def test_ls_local_path_raises(self, client: PolarionSVNClient):
        with pytest.raises(SVNWebClientError, match="Expected remote"):
            client.ls("./local-path")


# ── mkdir / rm ───────────────────────────────────────────────────────


class TestMkdirRm:
    def test_mkdir_returns_true(self, client: PolarionSVNClient, server: str, root: str):
        name = f"_api_mkdir_{uuid.uuid4().hex[:8]}"
        path = f"{server}:{root}/{name}"
        result = client.mkdir(path)
        assert result is True
        # Verify
        items = client.ls(f"{server}:{root}")
        assert any(i.name == name for i in items)
        # Cleanup
        client.rm(path)

    def test_rm_returns_true(self, client: PolarionSVNClient, remote_folder: str):
        # remote_folder exists, rm it (fixture won't fail on cleanup)
        result = client.rm(remote_folder)
        assert result is True

    def test_rm_root_raises(self, client: PolarionSVNClient, server: str):
        with pytest.raises(SVNWebClientError, match="root"):
            client.rm(f"{server}:SingleItem")


# ── cp (single file) ────────────────────────────────────────────────


class TestCp:
    def test_upload_returns_true(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        f = tmp_path / "up.txt"
        f.write_text("upload content")
        result = client.cp(str(f), f"{remote_folder}/up.txt")
        assert result is True

        items = client.ls(remote_folder)
        assert any(i.name == "up.txt" for i in items)

    def test_download_returns_true(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        # Upload first
        f = tmp_path / "src.txt"
        f.write_text("download me")
        client.cp(str(f), f"{remote_folder}/src.txt")

        # Download
        dst = tmp_path / "dst.txt"
        result = client.cp(f"{remote_folder}/src.txt", str(dst))
        assert result is True
        assert dst.read_text() == "download me"

    def test_cp_two_local_raises(self, client: PolarionSVNClient):
        with pytest.raises(SVNWebClientError, match="remote"):
            client.cp("./a", "./b")


# ── cp_r (recursive) ────────────────────────────────────────────────


class TestCpR:
    def test_upload_recursive(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        local_dir = tmp_path / "src"
        local_dir.mkdir()
        (local_dir / "a.txt").write_text("aaa")
        (local_dir / "sub").mkdir()
        (local_dir / "sub" / "b.txt").write_text("bbb")

        actions = client.cp_r(str(local_dir), remote_folder)
        assert isinstance(actions, list)
        assert all(isinstance(a, SyncAction) for a in actions)
        uploads = [a for a in actions if a.op == SyncOp.UPLOAD]
        assert len(uploads) >= 2

        items = client.ls(remote_folder, recursive=True)
        names = [i.name for i in items]
        assert "a.txt" in names
        assert "b.txt" in names

    def test_download_recursive(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        # Upload some files first
        f1 = tmp_path / "x.txt"
        f1.write_text("xxx")
        client.cp(str(f1), f"{remote_folder}/x.txt")

        # Download recursively
        dst = tmp_path / "dst"
        actions = client.cp_r(remote_folder, str(dst))
        assert isinstance(actions, list)
        assert (dst / "x.txt").read_text() == "xxx"


# ── sync ─────────────────────────────────────────────────────────────


class TestSync:
    def test_sync_upload(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        local_dir = tmp_path / "sync_src"
        local_dir.mkdir()
        (local_dir / "s.txt").write_text("sync content")

        actions = client.sync(str(local_dir), remote_folder)
        assert isinstance(actions, list)
        uploads = [a for a in actions if a.op in (SyncOp.UPLOAD, SyncOp.MKDIR)]
        assert len(uploads) >= 1

        items = client.ls(remote_folder)
        assert any(i.name == "s.txt" for i in items)

    def test_sync_idempotent(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        local_dir = tmp_path / "sync_idem"
        local_dir.mkdir()
        (local_dir / "stable.txt").write_text("stable")

        # First sync
        client.sync(str(local_dir), remote_folder)

        # Second sync — all skips
        actions = client.sync(str(local_dir), remote_folder)
        non_skip = [a for a in actions if a.op != SyncOp.SKIP]
        assert len(non_skip) == 0

    def test_sync_dry_run(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        local_dir = tmp_path / "sync_dry"
        local_dir.mkdir()
        (local_dir / "ghost.txt").write_text("should not appear")

        actions = client.sync(str(local_dir), remote_folder, dry_run=True)
        uploads = [a for a in actions if a.op == SyncOp.UPLOAD]
        assert len(uploads) >= 1

        # Verify nothing was uploaded
        items = client.ls(remote_folder)
        assert not any(i.name == "ghost.txt" for i in items)

    def test_sync_with_exclude(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        local_dir = tmp_path / "sync_excl"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")
        (local_dir / "skip.log").write_text("skip")

        client.sync(str(local_dir), remote_folder, exclude=["*.log"])

        items = client.ls(remote_folder)
        names = [i.name for i in items]
        assert "keep.txt" in names
        assert "skip.log" not in names

    def test_sync_download(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        # Upload a file first
        f = tmp_path / "up.txt"
        f.write_text("pull me")
        client.cp(str(f), f"{remote_folder}/pull.txt")

        # Sync down
        dst = tmp_path / "sync_dst"
        actions = client.sync(remote_folder, str(dst))
        downloads = [a for a in actions if a.op == SyncOp.DOWNLOAD]
        assert len(downloads) >= 1
        assert (dst / "pull.txt").read_text() == "pull me"

    def test_sync_not_a_dir_raises(self, client: PolarionSVNClient, remote_folder: str, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("not a dir")
        with pytest.raises(SVNWebClientError, match="not a directory"):
            client.sync(str(f), remote_folder)


# ── Cross-server (requires SERVER_B) ────────────────────────────────


class TestCrossServer:
    @pytest.fixture(scope="class")
    def server_b(self) -> str:
        return _get_env("SVNCLI_SERVER_B")

    @pytest.fixture(scope="class")
    def root_b(self) -> str:
        return _get_env("SVNCLI_ROOT_B")

    @pytest.fixture(scope="class")
    def client_b(self, client: PolarionSVNClient, server_b: str) -> PolarionSVNClient:
        client.login(server_b)
        return client

    def test_sync_cross_server(
        self, client: PolarionSVNClient, server: str, root: str, server_b: str, root_b: str, tmp_path: Path
    ):
        src_name = f"_api_xsync_src_{uuid.uuid4().hex[:8]}"
        src = f"{server}:{root}/{src_name}"
        dst_name = f"_api_xsync_dst_{uuid.uuid4().hex[:8]}"
        dst = f"{server_b}:{root_b}/{dst_name}"

        client.mkdir(src)
        f = tmp_path / "cross.txt"
        f.write_text("cross-server via API")
        client.cp(str(f), f"{src}/cross.txt")

        try:
            actions = client.sync(src, dst)
            uploads = [a for a in actions if a.op in (SyncOp.UPLOAD, SyncOp.MKDIR)]
            assert len(uploads) >= 1

            items = client.ls(dst)
            assert any(i.name == "cross.txt" for i in items)
        finally:
            client.rm(src)
            with contextlib.suppress(Exception):
                client.rm(dst)
