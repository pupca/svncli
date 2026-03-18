"""High-level Python API for Polarion SVN Web Client."""

from __future__ import annotations

import tempfile
from pathlib import Path

import requests

from .client import (
    SVNWebClient,
    SVNWebClientError,
    extract_browser_cookies,
    interactive_login,
    load_saved_cookies,
    save_cookies,
)
from .models import RemoteItem, SyncAction, SyncOp
from .sync import (
    _file_state,
    _rel_path,
    build_local_manifest,
    plan_sync_download,
    plan_sync_upload,
    save_manifest,
)
from .util import normalize_remote_path, parse_path, split_remote_path


def _parse_domain(server: str) -> str:
    """Extract domain from a server URL, e.g. 'https://host.example.com' → 'host.example.com'."""
    from urllib.parse import urlparse

    parsed = urlparse(server)
    if not parsed.hostname:
        raise SVNWebClientError(f"Invalid server URL: {server}")
    return parsed.hostname


class PolarionSVNClient:
    """High-level client for Polarion SVN — mirrors the CLI interface.

    Uses the same ``server:path`` format as the CLI for all operations.
    Manages authentication and connections to multiple servers.

    Example::

        client = PolarionSVNClient()
        client.login("https://server.com")
        items = client.ls("https://server.com:Project/trunk")
    """

    def __init__(self, *, verify_ssl: bool = True, timeout: int = 60) -> None:
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._clients: dict[str, SVNWebClient] = {}

    # ── Authentication ───────────────────────────────────────────────

    def login(
        self,
        server: str,
        *,
        cookie: str | None = None,
        browser: str = "chrome",
        interactive: bool = False,
    ) -> None:
        """Authenticate to a server and save the session.

        Args:
            server: Server URL, e.g. ``"https://server.example.com"``
            cookie: Cookie string from browser DevTools.
            browser: Browser to extract cookies from (default: chrome).
            interactive: Open a browser window for login.
        """
        if cookie:
            save_cookies(server, cookie)
        elif interactive:
            interactive_login(server)
        else:
            domain = _parse_domain(server)
            extracted = extract_browser_cookies(domain, browser)
            save_cookies(server, extracted)

        # Verify and cache
        client = self._get_or_create_client(server, force=True)
        client._get("directoryContent.jsp")

    def logout(self, server: str) -> None:
        """Remove saved session cookies for a server."""
        from .client import COOKIE_FILE

        self._clients.pop(server, None)
        if not COOKIE_FILE.exists():
            return
        import json

        try:
            data = json.loads(COOKIE_FILE.read_text())
            key = server.rstrip("/")
            if key in data:
                del data[key]
                if data:
                    COOKIE_FILE.write_text(json.dumps(data, indent=2))
                else:
                    COOKIE_FILE.unlink()
        except (json.JSONDecodeError, OSError):
            pass

    # ── Operations ───────────────────────────────────────────────────

    def ls(self, path: str, *, recursive: bool = False) -> list[RemoteItem]:
        """List a remote directory.

        Args:
            path: Remote path, e.g. ``"https://server.com:Repo/folder"``
            recursive: List all subdirectories recursively.

        Returns:
            List of RemoteItem objects.
        """
        client, remote_path = self._resolve(path)
        if recursive:
            return client.ls_recursive(remote_path)
        return client.ls(remote_path)

    def cp(self, src: str, dst: str) -> bool:
        """Copy a single file between local and remote.

        Args:
            src: Source path (local or remote).
            dst: Destination path (local or remote).

        Returns:
            True on success.

        Raises:
            SVNWebClientError: On server errors.
        """
        src_parsed = parse_path(src)
        dst_parsed = parse_path(dst)

        if src_parsed.is_local and dst_parsed.is_remote:
            client, remote_path = self._resolve(dst)
            client.upload_file(remote_path, Path(src_parsed.path))
            return True
        elif src_parsed.is_remote and dst_parsed.is_local:
            client, remote_path = self._resolve(src)
            client.download_file(remote_path, Path(dst_parsed.path))
            return True
        elif src_parsed.is_remote and dst_parsed.is_remote:
            # Remote-to-remote single file: download to temp, upload
            src_client, src_path = self._resolve(src)
            dst_client, dst_path = self._resolve(dst)
            content = src_client.download_file_to_buffer(src_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as f:
                f.write(content)
                tmp = Path(f.name)
            try:
                dst_client.upload_file(dst_path, tmp)
            finally:
                tmp.unlink(missing_ok=True)
            return True
        else:
            raise SVNWebClientError("cp requires at least one remote path")

    def cp_r(self, src: str, dst: str) -> list[SyncAction]:
        """Recursively copy a directory.

        Args:
            src: Source path (local or remote).
            dst: Destination path (local or remote).

        Returns:
            List of SyncAction objects describing what was copied.
        """
        src_parsed = parse_path(src)
        dst_parsed = parse_path(dst)

        if src_parsed.is_local and dst_parsed.is_remote:
            client, remote_path = self._resolve(dst)
            return self._upload_recursive(client, Path(src_parsed.path), remote_path)
        elif src_parsed.is_remote and dst_parsed.is_local:
            client, remote_path = self._resolve(src)
            return self._download_recursive(client, remote_path, Path(dst_parsed.path))
        elif src_parsed.is_remote and dst_parsed.is_remote:
            return self._remote_to_remote(src, dst, delete=False)
        else:
            raise SVNWebClientError("cp_r requires at least one remote path")

    def sync(
        self,
        src: str,
        dst: str,
        *,
        delete: bool = False,
        exclude: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[SyncAction]:
        """Sync files between source and destination.

        Args:
            src: Source path (local or remote).
            dst: Destination path (local or remote).
            delete: Remove destination files not present in source.
            exclude: Glob patterns to exclude.
            dry_run: Plan actions without executing.

        Returns:
            List of SyncAction objects describing what was done (or would be done).
        """
        src_parsed = parse_path(src)
        dst_parsed = parse_path(dst)

        if src_parsed.is_local and dst_parsed.is_remote:
            client, remote_path = self._resolve(dst)
            return self._sync_upload(client, Path(src_parsed.path), remote_path, delete, exclude, dry_run)
        elif src_parsed.is_remote and dst_parsed.is_local:
            client, remote_path = self._resolve(src)
            return self._sync_download(client, remote_path, Path(dst_parsed.path), delete, dry_run)
        elif src_parsed.is_remote and dst_parsed.is_remote:
            return self._remote_to_remote(src, dst, delete=delete, exclude=exclude, dry_run=dry_run)
        else:
            raise SVNWebClientError("sync requires at least one remote path")

    def rm(self, path: str) -> bool:
        """Delete a remote file or directory.

        Returns:
            True on success.

        Raises:
            SVNWebClientError: If the path doesn't exist or can't be deleted.
        """
        client, remote_path = self._resolve(path)
        parent, name = split_remote_path(remote_path)
        if not parent:
            raise SVNWebClientError("Cannot delete repository root")
        client.delete_items(parent, [name])
        return True

    def mkdir(self, path: str) -> bool:
        """Create a remote directory.

        Returns:
            True on success.

        Raises:
            SVNWebClientError: If the directory can't be created.
        """
        client, remote_path = self._resolve(path)
        client.mkdir(remote_path)
        return True

    # ── Internal helpers ─────────────────────────────────────────────

    def _resolve(self, raw: str) -> tuple[SVNWebClient, str]:
        """Parse a remote path and return (client, remote_path)."""
        parsed = parse_path(raw)
        if parsed.is_local:
            raise SVNWebClientError(f"Expected remote path, got local: {raw}")
        return self._get_or_create_client(parsed.server), parsed.path

    def _get_or_create_client(self, server: str, force: bool = False) -> SVNWebClient:
        """Get cached client or create a new one."""
        if not force and server in self._clients:
            return self._clients[server]
        cookie = load_saved_cookies(server)
        if not cookie:
            domain = _parse_domain(server)
            try:
                cookie = extract_browser_cookies(domain)
            except (SVNWebClientError, requests.exceptions.RequestException):
                raise SVNWebClientError(
                    f'Not authenticated to {server}. Call client.login("{server}") first.'
                ) from None
        client = SVNWebClient(server, cookie, verify_ssl=self._verify_ssl, timeout=self._timeout)
        self._clients[server] = client
        return client

    def _upload_recursive(self, client: SVNWebClient, local_dir: Path, remote_path: str) -> list[SyncAction]:
        """Upload a local directory recursively."""
        remote_exists = True
        try:
            remote_items = client.ls_recursive(remote_path)
        except (SVNWebClientError, requests.exceptions.RequestException):
            remote_items = []
            remote_exists = False
        actions = plan_sync_upload(local_dir, remote_path, remote_items, delete=False)
        if not remote_exists:
            actions.insert(0, SyncAction(op=SyncOp.MKDIR, remote_path=remote_path, reason="new directory"))
        self._execute(client, actions)
        return actions

    def _download_recursive(self, client: SVNWebClient, remote_path: str, local_dir: Path) -> list[SyncAction]:
        """Download a remote directory recursively."""
        remote_items = client.ls_recursive(remote_path)
        actions = plan_sync_download(remote_path, local_dir, remote_items)
        self._execute(client, [a for a in actions if a.op != SyncOp.SKIP])
        return actions

    def _sync_upload(
        self,
        client: SVNWebClient,
        local_dir: Path,
        remote_path: str,
        delete: bool,
        exclude: list[str] | None,
        dry_run: bool,
    ) -> list[SyncAction]:
        remote_path = normalize_remote_path(remote_path)
        if not local_dir.is_dir():
            raise SVNWebClientError(f"{local_dir} is not a directory")
        remote_exists = True
        try:
            remote_items = client.ls_recursive(remote_path)
        except (SVNWebClientError, requests.exceptions.RequestException):
            remote_items = []
            remote_exists = False
        actions = plan_sync_upload(local_dir, remote_path, remote_items, delete=delete, exclude=exclude)
        if not remote_exists:
            actions.insert(0, SyncAction(op=SyncOp.MKDIR, remote_path=remote_path, reason="new directory"))
        if not dry_run:
            self._execute(client, [a for a in actions if a.op != SyncOp.SKIP])
            self._save_manifest(client, local_dir, remote_path)
        return actions

    def _sync_download(
        self,
        client: SVNWebClient,
        remote_path: str,
        local_dir: Path,
        delete: bool,
        dry_run: bool,
    ) -> list[SyncAction]:
        remote_path = normalize_remote_path(remote_path)
        remote_items = client.ls_recursive(remote_path)
        actions = plan_sync_download(remote_path, local_dir, remote_items, delete=delete)
        if not dry_run:
            self._execute(client, [a for a in actions if a.op != SyncOp.SKIP])
            self._save_manifest_from_items(local_dir, remote_path, remote_items)
        return actions

    def _remote_to_remote(
        self,
        src: str,
        dst: str,
        delete: bool = False,
        exclude: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[SyncAction]:
        src_client, src_path = self._resolve(src)
        dst_client, dst_path = self._resolve(dst)
        with tempfile.TemporaryDirectory(prefix="svncli_") as tmp:
            tmp_dir = Path(tmp)
            src_items = src_client.ls_recursive(src_path)
            src_prefix = src_path.rstrip("/") + "/"
            for item in src_items:
                if item.is_dir:
                    continue
                rel = _rel_path(item.path, src_prefix, item.name)
                dest = tmp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                src_client.download_file(item.path, dest)
            dst_exists = True
            try:
                dst_items = dst_client.ls_recursive(dst_path)
            except (SVNWebClientError, requests.exceptions.RequestException):
                dst_items = []
                dst_exists = False
            actions = plan_sync_upload(tmp_dir, dst_path, dst_items, delete=delete, exclude=exclude)
            if not dst_exists:
                actions.insert(0, SyncAction(op=SyncOp.MKDIR, remote_path=dst_path, reason="new directory"))
            if not dry_run:
                self._execute(dst_client, [a for a in actions if a.op != SyncOp.SKIP])
        return actions

    def _execute(self, client: SVNWebClient, actions: list[SyncAction]) -> None:
        """Execute a list of sync actions."""
        for action in actions:
            if action.op == SyncOp.MKDIR:
                client.mkdir(action.remote_path)
            elif action.op == SyncOp.UPLOAD:
                client.upload_file(action.remote_path, Path(action.local_path))
            elif action.op == SyncOp.UPDATE:
                client.update_file(action.remote_path, Path(action.local_path))
            elif action.op == SyncOp.DOWNLOAD:
                Path(action.local_path).parent.mkdir(parents=True, exist_ok=True)
                client.download_file(action.remote_path, Path(action.local_path))
            elif action.op == SyncOp.DELETE:
                if action.local_path:
                    Path(action.local_path).unlink(missing_ok=True)
                else:
                    parent, name = split_remote_path(action.remote_path)
                    if parent:
                        client.delete_items(parent, [name])

    def _save_manifest(self, client: SVNWebClient, local_dir: Path, remote_path: str) -> None:
        try:
            remote_items = client.ls_recursive(remote_path)
        except (SVNWebClientError, requests.exceptions.RequestException):
            remote_items = []
        self._save_manifest_from_items(local_dir, remote_path, remote_items)

    def _save_manifest_from_items(self, local_dir: Path, remote_path: str, remote_items: list[RemoteItem]) -> None:
        prefix = remote_path.rstrip("/") + "/"
        revisions = {_rel_path(i.path, prefix, i.name): i.revision for i in remote_items if not i.is_dir}
        local_m = build_local_manifest(local_dir)
        file_states = {r: _file_state(p, revisions.get(r)) for r, p in local_m.items()}
        save_manifest(local_dir, remote_path, file_states)
