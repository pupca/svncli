"""Unhappy path tests — error handling, invalid inputs, edge cases."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import requests

from svncli.client import SVNWebClient, SVNWebClientError, extract_browser_cookies
from svncli.util import parse_path

SVNCLI = [sys.executable, "-m", "svncli"]


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        pytest.skip(f"{name} not set")
    return val


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*SVNCLI, "--no-verify-ssl", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def server() -> str:
    return _get_env("SVNCLI_SERVER_A")


@pytest.fixture(scope="module")
def root() -> str:
    return _get_env("SVNCLI_ROOT_A")


@pytest.fixture(scope="module")
def client(server: str) -> SVNWebClient:
    domain = server.split("//")[1].split("/")[0].split(":")[0]
    cookie = extract_browser_cookies(domain, "chrome")
    return SVNWebClient(server, cookie, verify_ssl=False)


# ── Path parsing errors ─────────────────────────────────────────────


class TestPathErrors:
    def test_bare_path_without_server(self):
        """Bare path like 'Repo/folder' should error."""
        with pytest.raises(ValueError, match="Cannot parse path"):
            parse_path("Repo/folder")

    def test_bare_single_word(self):
        with pytest.raises(ValueError, match="Cannot parse path"):
            parse_path("something")

    def test_empty_string(self):
        with pytest.raises(ValueError):
            parse_path("")

    def test_http_without_colon_path(self):
        """URL without :path should still parse (empty path)."""
        # This is actually valid — server with empty path
        # But the regex requires a colon after the host
        with pytest.raises(ValueError):
            parse_path("https://server.com")

    def test_valid_local_paths(self):
        assert parse_path("/absolute/path").is_local
        assert parse_path("./relative").is_local
        assert parse_path("~/home").is_local
        assert parse_path("../parent").is_local

    def test_valid_remote_paths(self):
        p = parse_path("https://server.com:Repo")
        assert p.is_remote
        assert p.server == "https://server.com"

    def test_remote_with_port(self):
        p = parse_path("https://server.com:8443:Repo/path")
        assert p.server == "https://server.com:8443"
        assert p.path == "Repo/path"


# ── CLI error handling ───────────────────────────────────────────────


class TestCLIErrors:
    def test_ls_bare_path_rejected(self):
        result = _run("ls", "SomePath")
        assert result.returncode != 0
        assert "Cannot parse path" in result.stderr or "https://" in result.stderr

    def test_cp_two_local_paths(self):
        result = _run("cp", "./src", "./dst")
        assert result.returncode != 0
        assert "remote" in result.stderr.lower()

    def test_sync_two_local_paths(self):
        result = _run("sync", "./src", "./dst")
        assert result.returncode != 0
        assert "remote" in result.stderr.lower()

    def test_rm_local_path(self):
        result = _run("rm", "./local")
        assert result.returncode != 0

    def test_mb_local_path(self):
        result = _run("mb", "./local")
        assert result.returncode != 0

    def test_login_no_server(self):
        result = _run("login")
        assert result.returncode != 0
        assert "server" in result.stderr.lower() or "provide" in result.stderr.lower()

    def test_cp_dir_without_recursive(self, server: str, root: str, tmp_path: Path):
        """Downloading a directory without -r should fail."""
        result = _run("cp", f"{server}:{root}", str(tmp_path / "out"))
        assert result.returncode != 0


# ── Server/network errors ────────────────────────────────────────────


class TestServerErrors:
    def test_nonexistent_server(self):
        """Connecting to a server that doesn't exist."""
        result = _run("ls", "https://this-server-does-not-exist-12345.example.com:Repo")
        assert result.returncode != 0

    def test_nonexistent_path(self, server: str):
        """Listing a path that doesn't exist on the server."""
        result = _run("ls", f"{server}:NonExistentProject_abc123/does/not/exist")
        assert result.returncode != 0

    def test_rm_nonexistent_path(self, server: str, root: str):
        """Deleting a file that doesn't exist."""
        result = _run("rm", f"{server}:{root}/this_file_definitely_does_not_exist_xyz")
        assert result.returncode != 0

    def test_mb_in_nonexistent_parent(self, server: str):
        """Creating a directory under a non-existent parent — server may accept or reject."""
        result = _run("mb", f"{server}:NoSuchParent_xyz/child")
        # Some servers auto-create parents, some reject — just verify no crash
        assert result.returncode in (0, 1)
        assert "Traceback" not in result.stderr

    def test_upload_to_nonexistent_parent(self, server: str, tmp_path: Path):
        """Uploading a file to a non-existent parent — server may accept or reject."""
        f = tmp_path / "test.txt"
        f.write_text("test")
        result = _run("cp", str(f), f"{server}:NoSuchParent_xyz/test.txt")
        # Some servers auto-create parents, some reject — just verify no crash
        assert result.returncode in (0, 1)

    def test_download_nonexistent_file(self, server: str, root: str, tmp_path: Path):
        """Downloading a file that doesn't exist."""
        dest = tmp_path / "nope.txt"
        result = _run("cp", f"{server}:{root}/this_file_does_not_exist_xyz.txt", str(dest))
        assert result.returncode != 0
        assert not dest.exists()


# ── Authentication errors ────────────────────────────────────────────


class TestAuthErrors:
    def test_expired_cookie(self, server: str, root: str):
        """Using an invalid cookie — should fail or return empty, not crash."""
        result = _run("--cookie", "JSESSIONID=INVALID_EXPIRED_SESSION", "ls", f"{server}:{root}")
        # Server may return 401 (fail) or an empty/different listing (succeed with no data)
        # The key assertion: it doesn't crash with a traceback
        assert "Traceback" not in result.stderr

    def test_empty_cookie(self, server: str, root: str):
        """Using an empty cookie string."""
        result = _run("--cookie", "", "ls", f"{server}:{root}")
        # Should fall back to saved cookies or browser extraction
        # If those work, it succeeds; if not, it fails — either is OK
        # The point is it doesn't crash
        assert result.returncode in (0, 1)

    def test_login_unreachable_server(self):
        """Login to a server that doesn't exist."""
        result = _run("login", "https://this-server-does-not-exist-12345.example.com")
        assert result.returncode != 0


# ── Sync edge cases ─────────────────────────────────────────────────


class TestSyncEdgeCases:
    def test_sync_empty_local_dir(self, server: str, root: str, tmp_path: Path):
        """Syncing an empty local directory should create remote dir with nothing in it."""
        import uuid

        local_dir = tmp_path / "empty"
        local_dir.mkdir()
        name = f"_empty_{uuid.uuid4().hex[:8]}"
        folder = f"{server}:{root}/{name}"

        result = _run("sync", str(local_dir), folder)
        assert result.returncode == 0
        # Cleanup
        _run("rm", folder)

    def test_sync_nonexistent_local_dir(self, server: str, root: str, tmp_path: Path):
        """Syncing from a local dir that doesn't exist should fail."""
        result = _run("sync", str(tmp_path / "no_such_dir"), f"{server}:{root}/target")
        assert result.returncode != 0

    def test_sync_local_file_not_dir(self, server: str, root: str, tmp_path: Path):
        """Syncing from a file (not directory) should fail."""
        f = tmp_path / "file.txt"
        f.write_text("not a dir")
        result = _run("sync", str(f), f"{server}:{root}/target")
        assert result.returncode != 0

    def test_dry_run_makes_no_changes(self, server: str, root: str, tmp_path: Path):
        """Dry run should not create anything on remote."""
        import uuid

        local_dir = tmp_path / "drytest"
        local_dir.mkdir()
        (local_dir / "phantom.txt").write_text("should not appear")

        name = f"_dry_{uuid.uuid4().hex[:8]}"
        folder = f"{server}:{root}/{name}"
        _run("mb", folder)

        try:
            result = _run("sync", "-n", str(local_dir), folder)
            assert result.returncode == 0
            assert "dry-run" in result.stdout

            ls_result = _run("ls", folder)
            assert "phantom.txt" not in ls_result.stdout
        finally:
            _run("rm", folder)


# ── Client-level error handling ──────────────────────────────────────


class TestClientErrors:
    def test_ls_nonexistent_path(self, client: SVNWebClient):
        """ls on a path that doesn't exist should raise."""
        with pytest.raises((SVNWebClientError, requests.exceptions.RequestException)):
            client.ls("ThisProjectDoesNotExist_xyz123/nope")

    def test_download_nonexistent_file(self, client: SVNWebClient, tmp_path: Path):
        """Downloading a non-existent file should raise."""
        with pytest.raises((SVNWebClientError, requests.exceptions.RequestException)):
            client.download_file("NonExistent_xyz/file.txt", tmp_path / "out.txt")

    def test_upload_nonexistent_local_file(self, client: SVNWebClient, root: str, tmp_path: Path):
        """Uploading a file that doesn't exist locally should raise."""
        with pytest.raises((SVNWebClientError, FileNotFoundError)):
            client.upload_file(f"{root}/test.txt", tmp_path / "does_not_exist.txt")

    def test_delete_from_empty_dir(self, client: SVNWebClient, root: str):
        """Deleting from an empty dir should raise (no items to delete)."""
        import uuid

        name = f"_del_test_{uuid.uuid4().hex[:8]}"
        client.mkdir(f"{root}/{name}")
        try:
            with pytest.raises(SVNWebClientError, match="empty"):
                client.delete_items(f"{root}/{name}", ["no_such_file.txt"])
        finally:
            client.delete_items(root, [name])

    def test_mkdir_in_nonexistent_parent(self, client: SVNWebClient):
        """Creating dir under non-existent parent should raise."""
        with pytest.raises((SVNWebClientError, requests.exceptions.RequestException)):
            client.mkdir("NoSuchParent_xyz123/child")

    def test_cookie_extraction_bad_browser(self):
        """Extracting cookies from unknown browser should raise."""
        with pytest.raises(SVNWebClientError, match="Unknown browser"):
            extract_browser_cookies("example.com", "netscape_navigator")

    def test_cookie_extraction_no_cookies(self):
        """Extracting cookies for a domain with no cookies should raise."""
        with pytest.raises(SVNWebClientError, match="No cookies found"):
            extract_browser_cookies("this-domain-has-no-cookies-12345.example.com", "chrome")
