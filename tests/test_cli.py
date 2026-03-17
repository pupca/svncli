"""Tests for CLI commands — invokes through subprocess against live server."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

SVNCLI = [sys.executable, "-m", "svncli"]


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"{name} not set")
    return val


@pytest.fixture(scope="module")
def server() -> str:
    return _get_env("SVNCLI_SERVER_A")


@pytest.fixture(scope="module")
def e2e_root(server: str) -> str:
    """Return server:root_path prefix for remote paths."""
    root = _get_env("SVNCLI_ROOT_A")
    return f"{server}:{root}"


def _run(*args: str, expect_error: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SVNCLI_BASE_URL", None)
    result = subprocess.run(
        [*SVNCLI, "--no-verify-ssl", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if not expect_error:
        assert result.returncode == 0, f"Command failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result


# ── help ────────────────────────────────────────────────────────────


class TestHelp:
    def test_main_help(self):
        result = subprocess.run([*SVNCLI, "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        for cmd in ("ls", "cp", "sync", "rm", "mb", "login", "logout"):
            assert cmd in result.stdout

    def test_ls_help(self):
        result = subprocess.run([*SVNCLI, "ls", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--recursive" in result.stdout

    def test_sync_help(self):
        result = subprocess.run([*SVNCLI, "sync", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--delete" in result.stdout
        assert "--exclude" in result.stdout

    def test_cp_help(self):
        result = subprocess.run([*SVNCLI, "cp", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--recursive" in result.stdout


# ── Error handling ──────────────────────────────────────────────────


class TestErrors:
    def test_bare_path_rejected(self):
        result = _run("ls", "SomePath", expect_error=True)
        assert result.returncode != 0


# ── ls ──────────────────────────────────────────────────────────────


class TestLs:
    def test_ls_basic(self, e2e_root: str):
        result = _run("ls", e2e_root)
        assert result.stdout.strip()

    def test_ls_empty_path(self, server: str):
        result = _run("ls", f"{server}:")
        assert result.stdout.strip()


# ── login ───────────────────────────────────────────────────────────


class TestLogin:
    def test_login(self, server: str):
        result = _run("login", server)
        assert "cookies" in result.stdout.lower() or "Extracted" in result.stdout
        assert "valid" in result.stdout.lower() or "Session" in result.stdout


# ── mb + rm ─────────────────────────────────────────────────────────


class TestMbRm:
    def test_mb_and_rm(self, e2e_root: str):
        folder_name = f"_cli_test_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"

        result = _run("mb", folder_path)
        assert "mkdir" in result.stdout

        result = _run("ls", e2e_root)
        assert folder_name in result.stdout

        result = _run("rm", folder_path)
        assert "delete" in result.stdout

        result = _run("ls", e2e_root)
        assert folder_name not in result.stdout

    def test_mb_dry_run(self, e2e_root: str):
        result = _run("mb", "-n", f"{e2e_root}/should_not_exist")
        assert "dry-run" in result.stdout

        result = _run("ls", e2e_root)
        assert "should_not_exist" not in result.stdout

    def test_rm_dry_run(self, e2e_root: str):
        folder_name = f"_cli_rm_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            result = _run("rm", "-n", folder_path)
            assert "dry-run" in result.stdout
            ls_result = _run("ls", e2e_root)
            assert folder_name in ls_result.stdout
        finally:
            _run("rm", folder_path)


# ── cp ──────────────────────────────────────────────────────────────


class TestCp:
    def test_cp_upload_and_download(self, e2e_root: str, tmp_path):
        folder_name = f"_cli_cp_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            local_file = tmp_path / "test.txt"
            local_file.write_text("cli test content")
            result = _run("cp", str(local_file), f"{folder_path}/test.txt")
            assert "upload" in result.stdout

            dest = tmp_path / "downloaded.txt"
            result = _run("cp", f"{folder_path}/test.txt", str(dest))
            assert "download" in result.stdout
            assert dest.read_text() == "cli test content"
        finally:
            _run("rm", folder_path)

    def test_cp_upload_dry_run(self, e2e_root: str, tmp_path):
        folder_name = f"_cli_cp_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            local_file = tmp_path / "phantom.txt"
            local_file.write_text("should not appear")
            result = _run("cp", "-n", str(local_file), f"{folder_path}/phantom.txt")
            assert "dry-run" in result.stdout
            ls_result = _run("ls", folder_path)
            assert "phantom.txt" not in ls_result.stdout
        finally:
            _run("rm", folder_path)

    def test_cp_download_dry_run(self, e2e_root: str, tmp_path):
        dest = tmp_path / "nope.txt"
        result = _run("cp", "-n", f"{e2e_root}/anything", str(dest))
        assert "dry-run" in result.stdout
        assert not dest.exists()

    def test_cp_dir_without_recursive_flag(self, e2e_root: str, tmp_path):
        result = _run("cp", e2e_root, str(tmp_path / "out"), expect_error=True)
        assert result.returncode != 0


# ── sync ────────────────────────────────────────────────────────────


class TestSync:
    def test_sync_upload_dry_run(self, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "a.txt").write_text("aaa")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            result = _run("sync", "-n", str(local_dir), folder_path)
            assert "dry-run" in result.stdout
            assert "upload" in result.stdout
        finally:
            _run("rm", folder_path)

    def test_sync_full_cycle(self, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "x.txt").write_text("xxx")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            result1 = _run("sync", str(local_dir), folder_path)
            assert "upload" in result1.stdout.lower() or "Completed" in result1.stdout

            result2 = _run("sync", str(local_dir), folder_path)
            assert "up to date" in result2.stdout.lower()
        finally:
            _run("rm", folder_path)

    def test_sync_with_exclude(self, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")
        (local_dir / "skip.log").write_text("skip")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            _run("sync", "--exclude", "*.log", str(local_dir), folder_path)
            ls_result = _run("ls", folder_path)
            assert "keep.txt" in ls_result.stdout
            assert "skip.log" not in ls_result.stdout
        finally:
            _run("rm", folder_path)

    def test_sync_with_delete(self, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path)

        try:
            (local_dir / "extra.txt").write_text("will be removed")
            _run("sync", str(local_dir), folder_path)
            (local_dir / "extra.txt").unlink()

            result = _run("sync", "--delete", "--force", str(local_dir), folder_path)
            assert "delete" in result.stdout.lower()

            ls_result = _run("ls", folder_path)
            assert "keep.txt" in ls_result.stdout
            assert "extra.txt" not in ls_result.stdout
        finally:
            _run("rm", folder_path)
