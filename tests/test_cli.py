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
def base_url() -> str:
    return _get_env("SVNCLI_BASE_URL")


@pytest.fixture(scope="module")
def e2e_root() -> str:
    return _get_env("SVNCLI_E2E_ROOT")


def _run(*args: str, base_url: str = "", expect_error: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if base_url:
        env["SVNCLI_BASE_URL"] = base_url
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
        assert "ls" in result.stdout
        assert "cp" in result.stdout
        assert "sync" in result.stdout
        assert "rm" in result.stdout
        assert "mb" in result.stdout

    def test_ls_help(self):
        result = subprocess.run([*SVNCLI, "ls", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--recursive" in result.stdout

    def test_sync_help(self):
        result = subprocess.run([*SVNCLI, "sync", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--delete" in result.stdout
        assert "--exclude" in result.stdout
        assert "--dry-run" in result.stdout

    def test_cp_help(self):
        result = subprocess.run([*SVNCLI, "cp", "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--recursive" in result.stdout


# ── Missing config errors ───────────────────────────────────────────


class TestConfigErrors:
    def test_missing_base_url(self):
        env = os.environ.copy()
        env.pop("SVNCLI_BASE_URL", None)
        result = subprocess.run(
            [*SVNCLI, "ls", "something"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "base-url" in result.stderr.lower() or "SVNCLI_BASE_URL" in result.stderr

    def test_missing_cookie_no_browser(self):
        """Without cookies and without browser access, should fail gracefully."""
        # This test is tricky — if Chrome is available it will extract cookies.
        # We test that the error message is helpful when extraction fails.
        pass  # Skip — can't reliably test without mocking browser access


# ── ls ──────────────────────────────────────────────────────────────


class TestLs:
    def test_ls_basic(self, base_url: str, e2e_root: str):
        result = _run("ls", e2e_root, base_url=base_url)
        assert result.stdout.strip()  # Should have output
        # Should have columnar output with dates and sizes
        lines = result.stdout.strip().split("\n")
        assert len(lines) >= 1

    def test_ls_empty_path(self, base_url: str):
        """Listing repository root should work."""
        result = _run("ls", "", base_url=base_url)
        assert result.stdout.strip()


# ── login ───────────────────────────────────────────────────────────


class TestLogin:
    def test_login(self, base_url: str):
        result = _run("login", base_url=base_url)
        assert "cookies" in result.stdout.lower() or "Extracted" in result.stdout
        assert "valid" in result.stdout.lower() or "Session" in result.stdout


# ── mb + rm ─────────────────────────────────────────────────────────


class TestMbRm:
    def test_mb_and_rm(self, base_url: str, e2e_root: str):
        folder_name = f"_cli_test_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"

        # Create
        result = _run("mb", folder_path, base_url=base_url)
        assert "mkdir" in result.stdout

        # Verify it exists
        result = _run("ls", e2e_root, base_url=base_url)
        assert folder_name in result.stdout

        # Delete
        result = _run("rm", folder_path, base_url=base_url)
        assert "delete" in result.stdout

        # Verify it's gone
        result = _run("ls", e2e_root, base_url=base_url)
        assert folder_name not in result.stdout

    def test_mb_dry_run(self, base_url: str, e2e_root: str):
        result = _run("mb", "-n", f"{e2e_root}/should_not_exist", base_url=base_url)
        assert "dry-run" in result.stdout

        # Verify it was NOT created
        result = _run("ls", e2e_root, base_url=base_url)
        assert "should_not_exist" not in result.stdout

    def test_rm_dry_run(self, base_url: str, e2e_root: str):
        """Dry-run rm should not delete anything."""
        folder_name = f"_cli_rm_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            result = _run("rm", "-n", folder_path, base_url=base_url)
            assert "dry-run" in result.stdout

            # Verify it still exists
            ls_result = _run("ls", e2e_root, base_url=base_url)
            assert folder_name in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)


# ── cp ──────────────────────────────────────────────────────────────


class TestCp:
    def test_cp_upload_and_download(self, base_url: str, e2e_root: str, tmp_path):
        folder_name = f"_cli_cp_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            # Upload
            local_file = tmp_path / "test.txt"
            local_file.write_text("cli test content")
            result = _run("cp", str(local_file), f"{folder_path}/test.txt", base_url=base_url)
            assert "upload" in result.stdout

            # Download
            dest = tmp_path / "downloaded.txt"
            result = _run("cp", f"{folder_path}/test.txt", str(dest), base_url=base_url)
            assert "download" in result.stdout
            assert dest.read_text() == "cli test content"
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_cp_upload_dry_run(self, base_url: str, e2e_root: str, tmp_path):
        """Dry-run upload should not create the file on remote."""
        folder_name = f"_cli_cp_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            local_file = tmp_path / "phantom.txt"
            local_file.write_text("should not appear")
            result = _run("cp", "-n", str(local_file), f"{folder_path}/phantom.txt", base_url=base_url)
            assert "dry-run" in result.stdout

            # Verify nothing uploaded
            ls_result = _run("ls", folder_path, base_url=base_url)
            assert "phantom.txt" not in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_cp_download_dry_run(self, base_url: str, e2e_root: str, tmp_path):
        dest = tmp_path / "nope.txt"
        result = _run("cp", "-n", f"{e2e_root}/anything", str(dest), base_url=base_url)
        assert "dry-run" in result.stdout
        assert not dest.exists()

    def test_cp_dir_without_recursive_flag(self, base_url: str, e2e_root: str, tmp_path):
        """Downloading a directory without -r should error."""
        result = _run("cp", e2e_root, str(tmp_path / "out"), base_url=base_url, expect_error=True)
        assert result.returncode != 0


# ── sync ────────────────────────────────────────────────────────────


class TestSync:
    def test_sync_upload_dry_run(self, base_url: str, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "a.txt").write_text("aaa")
        (local_dir / "b.txt").write_text("bbb")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            result = _run("sync", "-n", str(local_dir), folder_path, base_url=base_url)
            assert "dry-run" in result.stdout
            assert "upload" in result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_sync_full_cycle(self, base_url: str, e2e_root: str, tmp_path):
        """Upload sync, then verify second sync skips everything."""
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "x.txt").write_text("xxx")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            # First sync — should upload
            result1 = _run("sync", str(local_dir), folder_path, base_url=base_url)
            assert "upload" in result1.stdout.lower() or "Completed" in result1.stdout

            # Second sync — should be up to date
            result2 = _run("sync", str(local_dir), folder_path, base_url=base_url)
            assert "up to date" in result2.stdout.lower()
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_sync_upload_dry_run_no_side_effects(self, base_url: str, e2e_root: str, tmp_path):
        """Dry-run should not create any files on remote."""
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "ghost.txt").write_text("should not appear")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            result = _run("sync", "-n", str(local_dir), folder_path, base_url=base_url)
            assert "dry-run" in result.stdout
            assert "upload" in result.stdout

            # Verify nothing was actually uploaded
            ls_result = _run("ls", folder_path, base_url=base_url)
            assert "ghost.txt" not in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_sync_delete_dry_run_no_side_effects(self, base_url: str, e2e_root: str, tmp_path):
        """--delete --dry-run should show plan but not delete anything."""
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")
        (local_dir / "doomed.txt").write_text("will pretend to delete")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            # Upload both files
            _run("sync", str(local_dir), folder_path, base_url=base_url)

            # Remove one locally
            (local_dir / "doomed.txt").unlink()

            # Dry-run with --delete
            result = _run("sync", "-n", "--delete", str(local_dir), folder_path, base_url=base_url)
            assert "dry-run" in result.stdout
            assert "delete" in result.stdout.lower()

            # Verify doomed.txt still exists on remote
            ls_result = _run("ls", folder_path, base_url=base_url)
            assert "doomed.txt" in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_sync_with_exclude(self, base_url: str, e2e_root: str, tmp_path):
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")
        (local_dir / "skip.log").write_text("skip")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            _run("sync", "--exclude", "*.log", str(local_dir), folder_path, base_url=base_url)
            # Verify only keep.txt was uploaded
            ls_result = _run("ls", folder_path, base_url=base_url)
            assert "keep.txt" in ls_result.stdout
            assert "skip.log" not in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)

    def test_sync_with_delete(self, base_url: str, e2e_root: str, tmp_path):
        """--delete should remove remote files not in local."""
        local_dir = tmp_path / "syncdir"
        local_dir.mkdir()
        (local_dir / "keep.txt").write_text("keep")

        folder_name = f"_cli_sync_{uuid.uuid4().hex[:8]}"
        folder_path = f"{e2e_root}/{folder_name}"
        _run("mb", folder_path, base_url=base_url)

        try:
            # Upload two files first
            (local_dir / "extra.txt").write_text("will be removed")
            _run("sync", str(local_dir), folder_path, base_url=base_url)

            # Remove extra.txt locally
            (local_dir / "extra.txt").unlink()

            # Sync with --delete --force (force skips confirmation prompt)
            result = _run("sync", "--delete", "--force", str(local_dir), folder_path, base_url=base_url)
            assert "delete" in result.stdout.lower()

            # Verify extra.txt is gone from remote
            ls_result = _run("ls", folder_path, base_url=base_url)
            assert "keep.txt" in ls_result.stdout
            assert "extra.txt" not in ls_result.stdout
        finally:
            _run("rm", folder_path, base_url=base_url)
