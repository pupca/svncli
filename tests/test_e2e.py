"""End-to-end tests against real SVN Web Client instances.

Usage:
    export SVNCLI_SERVER_A="https://debian12-dtct4a-20250.polarion.net.plm.eds.com"
    export SVNCLI_ROOT_A="Popelak"
    export SVNCLI_SERVER_B="https://polarionx-demo.plmcloudsolutions.com"
    export SVNCLI_ROOT_B="Sandbox/Popelak"
    python -m pytest tests/test_e2e.py -v -s

Tests create temporary folders under the roots, run all operations,
and clean up after themselves. If SERVER_B is not set, cross-server tests are skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from svncli.client import SVNWebClient, extract_browser_cookies
from svncli.models import SyncOp
from svncli.sync import (
    _file_state,
    _rel_path,
    build_local_manifest,
    plan_sync_download,
    plan_sync_upload,
    save_manifest,
)

SVNCLI = [sys.executable, "-m", "svncli"]


# ── Helpers ──────────────────────────────────────────────────────────


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"{name} not set")
    return val


def _make_client(server: str) -> SVNWebClient:
    domain = server.split("//")[1].split("/")[0].split(":")[0]
    cookie = extract_browser_cookies(domain, "chrome")
    return SVNWebClient(server, cookie, verify_ssl=False)


def _run_cli(*args: str, expect_error: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    result = subprocess.run(
        [*SVNCLI, "--no-verify-ssl", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if not expect_error:
        assert result.returncode == 0, f"Command failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def server_a() -> str:
    return _get_env("SVNCLI_SERVER_A")


@pytest.fixture(scope="module")
def root_a() -> str:
    return _get_env("SVNCLI_ROOT_A")


@pytest.fixture(scope="module")
def client_a(server_a: str) -> SVNWebClient:
    return _make_client(server_a)


@pytest.fixture(scope="module")
def server_b() -> str:
    return _get_env("SVNCLI_SERVER_B")


@pytest.fixture(scope="module")
def root_b() -> str:
    return _get_env("SVNCLI_ROOT_B")


@pytest.fixture(scope="module")
def client_b(server_b: str) -> SVNWebClient:
    return _make_client(server_b)


@pytest.fixture(scope="module")
def test_folder_a(client_a: SVNWebClient, root_a: str):
    """Create a unique test folder on server A, clean up after."""
    folder_name = f"_e2e_a_{uuid.uuid4().hex[:8]}"
    folder_path = f"{root_a}/{folder_name}"
    client_a.mkdir(folder_path)
    yield folder_path
    try:
        client_a.delete_items(root_a, [folder_name])
    except Exception as e:
        print(f"\n⚠ Cleanup failed for server A {folder_path}: {e}")


@pytest.fixture(scope="module")
def test_folder_b(client_b: SVNWebClient, root_b: str):
    """Create a unique test folder on server B, clean up after."""
    folder_name = f"_e2e_b_{uuid.uuid4().hex[:8]}"
    folder_path = f"{root_b}/{folder_name}"
    client_b.mkdir(folder_path)
    yield folder_path
    try:
        client_b.delete_items(root_b, [folder_name])
    except Exception as e:
        print(f"\n⚠ Cleanup failed for server B {folder_path}: {e}")


# ── Single-server tests (server A) ──────────────────────────────────


class TestSingleServer:
    """Core operations against server A."""

    def test_ls_empty_folder(self, client_a: SVNWebClient, test_folder_a: str):
        items = client_a.ls(test_folder_a)
        assert items == []

    def test_mkdir(self, client_a: SVNWebClient, test_folder_a: str):
        client_a.mkdir(f"{test_folder_a}/subdir")
        items = client_a.ls(test_folder_a)
        names = [i.name for i in items]
        assert "subdir" in names
        assert next(i for i in items if i.name == "subdir").is_dir

    def test_upload_file(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        client_a.upload_file(f"{test_folder_a}/hello.txt", f)

        items = client_a.ls(test_folder_a)
        hello = next(i for i in items if i.name == "hello.txt")
        assert not hello.is_dir
        assert hello.size == 11

    def test_download_file(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        dest = tmp_path / "downloaded.txt"
        client_a.download_file(f"{test_folder_a}/hello.txt", dest)
        assert dest.read_text() == "hello world"

    def test_update_file(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        items_before = client_a.ls(test_folder_a)
        rev_before = next(i for i in items_before if i.name == "hello.txt").revision

        f = tmp_path / "hello.txt"
        f.write_text("hello world updated")
        client_a.update_file(f"{test_folder_a}/hello.txt", f)

        items_after = client_a.ls(test_folder_a)
        hello = next(i for i in items_after if i.name == "hello.txt")
        assert hello.size == 19
        assert hello.revision > rev_before

    def test_upload_to_subdir(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        f = tmp_path / "nested.txt"
        f.write_text("nested content")
        client_a.upload_file(f"{test_folder_a}/subdir/nested.txt", f)

        items = client_a.ls(f"{test_folder_a}/subdir")
        assert any(i.name == "nested.txt" for i in items)

    def test_ls_recursive(self, client_a: SVNWebClient, test_folder_a: str):
        all_items = client_a.ls_recursive(test_folder_a)
        paths = [i.path for i in all_items]
        assert any("hello.txt" in p for p in paths)
        assert any("nested.txt" in p for p in paths)

    def test_sync_upload_and_skip(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        """Upload sync, then verify second sync skips."""
        local_dir = tmp_path / "synctest"
        local_dir.mkdir()
        (local_dir / "stable.txt").write_text("stable")

        target = f"{test_folder_a}/sync1"
        client_a.mkdir(target)

        # First sync
        remote_items = client_a.ls_recursive(target)
        actions = plan_sync_upload(local_dir, target, remote_items)
        for a in actions:
            if a.op == SyncOp.UPLOAD:
                client_a.upload_file(a.remote_path, Path(a.local_path))

        # Save manifest
        remote_items = client_a.ls_recursive(target)
        prefix = target + "/"
        revisions = {_rel_path(i.path, prefix, i.name): i.revision for i in remote_items if not i.is_dir}
        local_m = build_local_manifest(local_dir)
        save_manifest(local_dir, target, {r: _file_state(p, revisions.get(r)) for r, p in local_m.items()})

        # Second sync — all skips
        remote_items = client_a.ls_recursive(target)
        actions2 = plan_sync_upload(local_dir, target, remote_items)
        assert all(a.op == SyncOp.SKIP for a in actions2)

    def test_sync_detects_change(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        local_dir = tmp_path / "synctest2"
        local_dir.mkdir()
        f = local_dir / "mutable.txt"
        f.write_text("original")

        target = f"{test_folder_a}/sync2"
        client_a.mkdir(target)

        # First sync + manifest
        remote_items = client_a.ls_recursive(target)
        actions = plan_sync_upload(local_dir, target, remote_items)
        for a in actions:
            if a.op == SyncOp.UPLOAD:
                client_a.upload_file(a.remote_path, Path(a.local_path))
        remote_items = client_a.ls_recursive(target)
        prefix = target + "/"
        revisions = {_rel_path(i.path, prefix, i.name): i.revision for i in remote_items if not i.is_dir}
        local_m = build_local_manifest(local_dir)
        save_manifest(local_dir, target, {r: _file_state(p, revisions.get(r)) for r, p in local_m.items()})

        # Modify
        time.sleep(0.1)
        f.write_text("changed!!!")

        # Should detect update
        remote_items = client_a.ls_recursive(target)
        actions2 = plan_sync_upload(local_dir, target, remote_items)
        updates = [a for a in actions2 if a.op == SyncOp.UPDATE]
        assert len(updates) == 1
        assert "mutable.txt" in updates[0].remote_path

    def test_download_sync(self, client_a: SVNWebClient, test_folder_a: str, tmp_path: Path):
        local_dir = tmp_path / "download_target"
        remote_items = client_a.ls_recursive(test_folder_a)
        file_items = [i for i in remote_items if not i.is_dir]

        actions = plan_sync_download(test_folder_a, local_dir, remote_items)
        downloads = [a for a in actions if a.op == SyncOp.DOWNLOAD]
        assert len(downloads) == len(file_items)

        for a in downloads:
            dest = Path(a.local_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            client_a.download_file(a.remote_path, dest)

        local_files = build_local_manifest(local_dir)
        assert len(local_files) == len(file_items)

    def test_delete_file(self, client_a: SVNWebClient, test_folder_a: str):
        items = client_a.ls(test_folder_a)
        assert any(i.name == "hello.txt" for i in items)
        client_a.delete_items(test_folder_a, ["hello.txt"])
        items = client_a.ls(test_folder_a)
        assert not any(i.name == "hello.txt" for i in items)

    def test_delete_subfolder(self, client_a: SVNWebClient, test_folder_a: str):
        items = client_a.ls(test_folder_a)
        assert any(i.name == "subdir" for i in items)
        client_a.delete_items(test_folder_a, ["subdir"])
        items = client_a.ls(test_folder_a)
        assert not any(i.name == "subdir" for i in items)


# ── CLI with server:path format ──────────────────────────────────────


class TestCLIServerPath:
    """Test CLI commands using the https://server:path format."""

    def test_cli_ls(self, server_a: str, root_a: str):
        result = _run_cli("ls", f"{server_a}:{root_a}")
        assert result.stdout.strip()

    def test_cli_mb_and_rm(self, server_a: str, root_a: str):
        name = f"_cli_path_{uuid.uuid4().hex[:8]}"
        path = f"{server_a}:{root_a}/{name}"

        _run_cli("mb", path)
        result = _run_cli("ls", f"{server_a}:{root_a}")
        assert name in result.stdout

        _run_cli("rm", path)
        result = _run_cli("ls", f"{server_a}:{root_a}")
        assert name not in result.stdout

    def test_cli_cp_upload_download(self, server_a: str, root_a: str, tmp_path: Path):
        name = f"_cli_cp_{uuid.uuid4().hex[:8]}"
        folder = f"{server_a}:{root_a}/{name}"
        _run_cli("mb", folder)

        try:
            src = tmp_path / "up.txt"
            src.write_text("upload test")
            _run_cli("cp", str(src), f"{folder}/up.txt")

            dst = tmp_path / "down.txt"
            _run_cli("cp", f"{folder}/up.txt", str(dst))
            assert dst.read_text() == "upload test"
        finally:
            _run_cli("rm", folder)

    def test_cli_sync_local_to_remote(self, server_a: str, root_a: str, tmp_path: Path):
        local_dir = tmp_path / "sync_src"
        local_dir.mkdir()
        (local_dir / "a.txt").write_text("aaa")

        name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder = f"{server_a}:{root_a}/{name}"
        _run_cli("mb", folder)

        try:
            result = _run_cli("sync", str(local_dir), folder)
            assert "upload" in result.stdout.lower() or "Completed" in result.stdout

            result2 = _run_cli("sync", str(local_dir), folder)
            assert "up to date" in result2.stdout.lower()
        finally:
            _run_cli("rm", folder)

    def test_cli_sync_remote_to_local(self, server_a: str, root_a: str, tmp_path: Path):
        # Upload some files first
        name = f"_cli_syncdown_{uuid.uuid4().hex[:8]}"
        folder = f"{server_a}:{root_a}/{name}"
        _run_cli("mb", folder)

        src = tmp_path / "src"
        src.mkdir()
        (src / "x.txt").write_text("xxx")
        _run_cli("sync", str(src), folder)

        try:
            dst = tmp_path / "dst"
            result = _run_cli("sync", folder, str(dst))
            assert "download" in result.stdout.lower() or "Completed" in result.stdout
            assert (dst / "x.txt").read_text() == "xxx"
        finally:
            _run_cli("rm", folder)

    def test_cli_login(self, server_a: str):
        result = _run_cli("login", server_a)
        assert "Session is valid" in result.stdout


# ── Cross-server tests (server A ↔ server B) ────────────────────────


class TestCrossServer:
    """Test copying between two different Polarion servers."""

    def test_cp_remote_to_remote(
        self,
        server_a: str,
        root_a: str,
        client_a: SVNWebClient,
        server_b: str,
        root_b: str,
        client_b: SVNWebClient,
        tmp_path: Path,
    ):
        """Copy a folder from server A to server B."""
        # Create source on A with files
        src_name = f"_xserver_src_{uuid.uuid4().hex[:8]}"
        src_path = f"{root_a}/{src_name}"
        client_a.mkdir(src_path)
        f1 = tmp_path / "cross1.txt"
        f1.write_text("cross-server content 1")
        f2 = tmp_path / "cross2.txt"
        f2.write_text("cross-server content 2")
        client_a.upload_file(f"{src_path}/cross1.txt", f1)
        client_a.upload_file(f"{src_path}/cross2.txt", f2)

        # Create dest on B
        dst_name = f"_xserver_dst_{uuid.uuid4().hex[:8]}"
        dst_path = f"{root_b}/{dst_name}"
        client_b.mkdir(dst_path)

        try:
            # Cross-server copy via CLI
            result = _run_cli("cp", "-r", f"{server_a}:{src_path}", f"{server_b}:{dst_path}")
            assert "Completed" in result.stdout or "upload" in result.stdout.lower()

            # Verify files landed on server B
            items = client_b.ls(dst_path)
            names = sorted(i.name for i in items)
            assert "cross1.txt" in names
            assert "cross2.txt" in names

            # Verify content
            content = client_b.download_file_to_buffer(f"{dst_path}/cross1.txt")
            assert content.decode() == "cross-server content 1"
        finally:
            client_a.delete_items(root_a, [src_name])
            client_b.delete_items(root_b, [dst_name])

    def test_ls_both_servers(self, server_a: str, root_a: str, server_b: str, root_b: str):
        """ls works independently on both servers."""
        result_a = _run_cli("ls", f"{server_a}:{root_a}")
        result_b = _run_cli("ls", f"{server_b}:{root_b}")
        assert result_a.stdout.strip()
        assert result_b.stdout.strip()
        # They should be different content
        assert result_a.stdout != result_b.stdout

    def test_login_both_servers(self, server_a: str, server_b: str):
        """Can authenticate to both servers."""
        result_a = _run_cli("login", server_a)
        assert "Session is valid" in result_a.stdout
        result_b = _run_cli("login", server_b)
        assert "Session is valid" in result_b.stdout
