"""CLI entry point for svncli."""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests

from .client import (
    AuthenticationError,
    SVNWebClient,
    SVNWebClientError,
    extract_browser_cookies,
    interactive_login,
    load_saved_cookies,
    save_cookies,
)
from .models import RemoteItem, SyncAction, SyncOp
from .sync import _file_state, build_local_manifest, plan_sync_download, plan_sync_upload, save_manifest
from .util import fmt_size, log_verbose, normalize_remote_path


def _resolve_base_url(args: argparse.Namespace) -> str:
    base_url = args.base_url or os.environ.get("SVNCLI_BASE_URL", "")
    if not base_url:
        print("Error: --base-url or SVNCLI_BASE_URL required", file=sys.stderr)
        sys.exit(1)
    return base_url


def _extract_domain(base_url: str) -> str:
    """Extract domain from a URL for cookie extraction."""
    from urllib.parse import urlparse

    return urlparse(base_url).hostname or ""


def _resolve_cookie(args: argparse.Namespace, base_url: str) -> str:
    """Resolve cookie string from multiple sources in priority order:
    1. --cookie flag
    2. SVNCLI_COOKIE env var
    3. Saved cookies from ~/.svncli/cookies.json
    4. Auto-extract from browser cookie store (rookiepy)
    """
    verbose = getattr(args, "verbose", False)

    # 1. Explicit cookie
    cookie = args.cookie or os.environ.get("SVNCLI_COOKIE", "")
    if cookie:
        log_verbose("Using cookie from --cookie/SVNCLI_COOKIE", verbose)
        return cookie

    # 2. Saved cookies from previous login
    cookie = load_saved_cookies(base_url)
    if cookie:
        log_verbose("Using saved cookies from ~/.svncli/cookies.json", verbose)
        return cookie

    # 3. Auto-extract from browser
    browser = getattr(args, "browser", None) or os.environ.get("SVNCLI_BROWSER", "chrome")
    domain = _extract_domain(base_url)
    try:
        cookie = extract_browser_cookies(domain, browser)
        log_verbose(f"Extracted cookies from {browser} for {domain}", verbose)
        return cookie
    except SVNWebClientError:
        pass

    # 4. Nothing found
    print("Error: no session cookies found.", file=sys.stderr)
    print("Options:", file=sys.stderr)
    print("  svncli login              — interactive browser login", file=sys.stderr)
    print("  --cookie 'KEY=VAL; ...'   — provide cookie string", file=sys.stderr)
    print("  SVNCLI_COOKIE env var     — provide cookie string", file=sys.stderr)
    print("  Log in via Chrome/Firefox — cookies are auto-extracted", file=sys.stderr)
    sys.exit(1)


def get_client(args: argparse.Namespace) -> SVNWebClient:
    base_url = _resolve_base_url(args)
    cookie = _resolve_cookie(args, base_url)
    verify_ssl = not args.no_verify_ssl
    timeout = getattr(args, "timeout", 60)
    return SVNWebClient(base_url, cookie, verify_ssl=verify_ssl, timeout=timeout)


# ── ls ──────────────────────────────────────────────────────────────


def cmd_ls(args: argparse.Namespace) -> None:
    client = get_client(args)
    remote_path = normalize_remote_path(args.path)
    items = client.ls_recursive(remote_path) if getattr(args, "recursive", False) else client.ls(remote_path)
    if not items:
        print(f"(empty directory: {remote_path})")
        return
    for item in items:
        date_str = item.last_modified.strftime("%Y-%m-%d %H:%M") if item.last_modified else ""
        size_str = fmt_size(item.size) if not item.is_dir else "DIR"
        rev_str = str(item.revision) if item.revision else ""
        print(f"{date_str:>16s}  {size_str:>10s}  r{rev_str:<8s}  {item.name}{'/' if item.is_dir else ''}")


# ── cp ──────────────────────────────────────────────────────────────


def cmd_cp(args: argparse.Namespace) -> None:
    client = get_client(args)
    src: str = args.src
    dst: str = args.dst
    recursive: bool = args.recursive

    # Determine direction: if src looks like a remote path (no leading / or .)
    # This is a simplified heuristic — in practice the user will know
    src_is_local = _is_local_path(src)
    dst_is_local = _is_local_path(dst)

    if src_is_local and not dst_is_local:
        # Upload
        _cp_upload(client, Path(src), dst, recursive, args)
    elif not src_is_local and dst_is_local:
        # Download
        _cp_download(client, src, Path(dst), recursive, args)
    else:
        print("Error: cp requires one local and one remote path.", file=sys.stderr)
        print("Local paths start with / or . or ~ ; remote paths are bare (e.g. Repo/folder)", file=sys.stderr)
        sys.exit(1)


def _is_local_path(path: str) -> bool:
    """Heuristic: local paths start with / . ~ or look like relative file paths."""
    return path.startswith(("/", ".", "~"))


def _cp_upload(client: SVNWebClient, src: Path, dst: str, recursive: bool, args: argparse.Namespace) -> None:
    dst = normalize_remote_path(dst)
    if src.is_file():
        if args.dry_run:
            print(f"(dry-run) upload: {src} → {dst}")
            return
        client.upload_file(dst, src)
        print(f"upload: {src} → {dst}")
    elif src.is_dir():
        if not recursive:
            print("Error: use -r to copy directories", file=sys.stderr)
            sys.exit(1)
        # Recursive upload = sync without delete
        remote_items = client.ls_recursive(dst)
        actions = plan_sync_upload(src, dst, remote_items, delete=False)
        _execute_actions(client, actions, args)
    else:
        print(f"Error: {src} does not exist", file=sys.stderr)
        sys.exit(1)


def _cp_download(client: SVNWebClient, src: str, dst: Path, recursive: bool, args: argparse.Namespace) -> None:
    src = normalize_remote_path(src)
    if not recursive:
        if args.dry_run:
            print(f"(dry-run) download: {src} → {dst}")
            return
        try:
            client.download_file(src, dst)
        except (SVNWebClientError, requests.exceptions.RequestException) as e:
            print(f"Error: download failed for {src}: {e}", file=sys.stderr)
            print("Hint: if this is a directory, use -r.", file=sys.stderr)
            sys.exit(1)
        print(f"download: {src} → {dst}")
    else:
        # Recursive download — use zip for efficiency
        if args.dry_run:
            print(f"(dry-run) download directory: {src} → {dst}")
            return
        log_verbose(f"Downloading {src} as zip...", args.verbose)
        zip_bytes = client.download_directory_zip_to_buffer(src)
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            zf.extractall(dst)
        print(f"download: {src} → {dst} ({len(zip_bytes)} bytes)")


# ── sync ────────────────────────────────────────────────────────────


def cmd_sync(args: argparse.Namespace) -> None:
    client = get_client(args)
    src: str = args.src
    dst: str = args.dst

    src_is_local = _is_local_path(src)
    dst_is_local = _is_local_path(dst)

    if src_is_local and not dst_is_local:
        log_verbose("Sync direction: local → remote (upload)", args.verbose)
        _sync_upload(client, Path(src), dst, args)
    elif not src_is_local and dst_is_local:
        log_verbose("Sync direction: remote → local (download)", args.verbose)
        _sync_download(client, src, Path(dst), args)
    else:
        print("Error: sync requires one local and one remote path.", file=sys.stderr)
        sys.exit(1)


def _sync_upload(client: SVNWebClient, local_dir: Path, remote_path: str, args: argparse.Namespace) -> None:
    remote_path = normalize_remote_path(remote_path)
    if not local_dir.is_dir():
        print(f"Error: {local_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    log_verbose(f"Listing remote: {remote_path} (recursive)...", args.verbose)
    try:
        remote_items = client.ls_recursive(remote_path)
    except (SVNWebClientError, requests.exceptions.RequestException):
        log_verbose(f"Remote path {remote_path} does not exist, will create it", args.verbose)
        remote_items = []

    log_verbose(f"Found {len(remote_items)} remote items", args.verbose)

    exclude = getattr(args, "exclude", [])
    actions = plan_sync_upload(local_dir, remote_path, remote_items, delete=args.delete, exclude=exclude)

    # If remote dir doesn't exist, prepend mkdir for the root
    if not remote_items:
        actions.insert(0, SyncAction(op=SyncOp.MKDIR, remote_path=remote_path, reason="new directory"))

    executed = _execute_actions(client, actions, args)

    # Save manifest after successful sync
    if executed and not args.dry_run:
        try:
            fresh_remote = client.ls_recursive(remote_path)
        except SVNWebClientError:
            fresh_remote = []
        _save_sync_manifest(local_dir, remote_path, fresh_remote, args.verbose)


def _sync_download(client: SVNWebClient, remote_path: str, local_dir: Path, args: argparse.Namespace) -> None:
    remote_path = normalize_remote_path(remote_path)
    log_verbose(f"Listing remote: {remote_path} (recursive)...", args.verbose)
    remote_items = client.ls_recursive(remote_path)
    log_verbose(f"Found {len(remote_items)} remote items", args.verbose)

    actions = plan_sync_download(remote_path, local_dir, remote_items, delete=args.delete)
    executed = _execute_actions(client, actions, args)

    # Save manifest after successful sync
    if executed and not args.dry_run:
        _save_sync_manifest(local_dir, remote_path, remote_items, args.verbose)


# ── rm / mb ─────────────────────────────────────────────────────────


def cmd_login(args: argparse.Namespace) -> None:
    """Log in and save session cookies."""
    base_url = _resolve_base_url(args)
    verify_ssl = not args.no_verify_ssl
    timeout = getattr(args, "timeout", 60)

    if getattr(args, "interactive", False):
        # Interactive: open browser window
        try:
            cookie = interactive_login(base_url)
        except SVNWebClientError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print("Cookies saved to ~/.svncli/cookies.json")
    else:
        # Non-interactive: try browser extraction
        domain = _extract_domain(base_url)
        browser = args.browser or os.environ.get("SVNCLI_BROWSER", "chrome")
        try:
            cookie = extract_browser_cookies(domain, browser)
        except SVNWebClientError as e:
            print(f"Could not extract cookies from {browser}: {e}", file=sys.stderr)
            print("Hint: use 'svncli login -i' for interactive browser login", file=sys.stderr)
            sys.exit(1)

        cookie_names = [p.split("=")[0].strip() for p in cookie.split(";")]
        print(f"Extracted {len(cookie_names)} cookies from {browser} for {domain}")

        # Save for future use
        save_cookies(base_url, cookie)
        print("Cookies saved to ~/.svncli/cookies.json")

    # Verify session works
    client = SVNWebClient(base_url, cookie, verify_ssl=verify_ssl, timeout=timeout)
    try:
        client._get("directoryContent.jsp")
        print("Session is valid.")
    except AuthenticationError:
        print("Session expired or invalid.", file=sys.stderr)
        sys.exit(1)
    except (SVNWebClientError, requests.exceptions.RequestException) as e:
        print(f"Connection test failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_logout(args: argparse.Namespace) -> None:
    """Remove saved session cookies."""
    from .client import COOKIE_FILE

    base_url = args.base_url or os.environ.get("SVNCLI_BASE_URL", "")
    if base_url and COOKIE_FILE.exists():
        # Remove just this server's cookies
        try:
            import json

            data = json.loads(COOKIE_FILE.read_text())
            key = base_url.rstrip("/")
            if key in data:
                del data[key]
                if data:
                    COOKIE_FILE.write_text(json.dumps(data, indent=2))
                else:
                    COOKIE_FILE.unlink()
                print(f"Logged out from {base_url}")
                return
        except (json.JSONDecodeError, OSError):
            pass
    if not base_url and COOKIE_FILE.exists():
        # No base URL — remove all saved cookies
        COOKIE_FILE.unlink()
        print("Removed all saved cookies.")
        return
    print("No saved cookies found.")


def cmd_rm(args: argparse.Namespace) -> None:
    client = get_client(args)
    remote_path = normalize_remote_path(args.path)
    # Split into parent dir + item name
    parts = remote_path.rsplit("/", 1)
    if len(parts) == 2:
        parent, name = parts
    else:
        print("Error: cannot delete repository root", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "dry_run", False):
        print(f"(dry-run) delete: {remote_path}")
        return
    client.delete_items(parent, [name])
    print(f"delete: {remote_path}")


def cmd_mb(args: argparse.Namespace) -> None:
    client = get_client(args)
    remote_path = normalize_remote_path(args.path)
    if args.dry_run:
        print(f"(dry-run) mkdir: {remote_path}")
        return
    client.mkdir(remote_path)
    print(f"mkdir: {remote_path}")


# ── Action executor ─────────────────────────────────────────────────


def _execute_actions(client: SVNWebClient, actions: list[SyncAction], args: argparse.Namespace) -> bool:
    """Execute sync actions. Returns True if sync completed (even if no-op)."""
    non_skip = [a for a in actions if a.op != SyncOp.SKIP]
    skipped = [a for a in actions if a.op == SyncOp.SKIP]

    if not non_skip:
        print("Everything is up to date.")
        if args.verbose and skipped:
            print(f"({len(skipped)} files unchanged)")
        return True

    # Show plan
    for action in non_skip:
        print(str(action))

    if args.dry_run:
        print(f"\n(dry-run) {len(non_skip)} operations planned, {len(skipped)} unchanged")
        return False

    # Confirm if deletes are planned and --force not set
    deletes = [a for a in non_skip if a.op == SyncOp.DELETE]
    if deletes and not getattr(args, "force", False):
        print(f"\n{len(deletes)} file(s) will be DELETED. Use --force to skip this prompt.")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            print("Aborted.")
            return False

    # Execute
    total = len(non_skip)
    errors = 0
    for i, action in enumerate(non_skip, 1):
        target = action.remote_path.rsplit("/", 1)[-1]
        print(f"  [{i}/{total}] {action.op.value} {target}...", end="", flush=True)
        try:
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
                    parts = action.remote_path.rsplit("/", 1)
                    if len(parts) == 2:
                        client.delete_items(parts[0], [parts[1]])
            print(" ok")
        except (SVNWebClientError, requests.exceptions.RequestException, OSError) as e:
            print(f" FAILED: {e}", file=sys.stderr)
            errors += 1

    print(f"\nCompleted: {total - errors}/{total} operations, {len(skipped)} unchanged")
    if errors:
        print(f"  {errors} errors", file=sys.stderr)
    return errors == 0


# ── Manifest persistence ────────────────────────────────────────────


def _save_sync_manifest(
    local_dir: Path,
    remote_path: str,
    remote_items: list[RemoteItem],
    verbose: bool = False,
) -> None:
    """Save sync manifest after a successful sync (upload or download)."""
    log_verbose("Saving sync manifest...", verbose)
    remote_prefix = remote_path.rstrip("/") + "/"
    remote_revisions: dict[str, int | None] = {}
    for item in remote_items:
        if not item.is_dir:
            rel = item.path[len(remote_prefix) :] if item.path.startswith(remote_prefix) else item.name
            remote_revisions[rel] = item.revision

    local_manifest = build_local_manifest(local_dir)
    file_states: dict[str, dict] = {}
    for rel_path, local_path in local_manifest.items():
        file_states[rel_path] = _file_state(local_path, remote_revisions.get(rel_path))

    save_manifest(local_dir, remote_path, file_states)
    log_verbose(f"Manifest saved ({len(file_states)} files)", verbose)


# ── Argument parser ─────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svncli",
        description="CLI tool for syncing files with Polarion SVN",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")
    parser.add_argument(
        "--base-url", help="Polarion server URL, e.g. https://host.example.com (or SVNCLI_BASE_URL env)"
    )
    parser.add_argument("--cookie", help="Browser cookie string (or SVNCLI_COOKIE env)")
    parser.add_argument("--browser", help="Browser to extract cookies from (default: chrome, or SVNCLI_BROWSER env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable SSL certificate verification")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP request timeout in seconds (default: 60)")

    sub = parser.add_subparsers(dest="command", required=True)

    # ls
    p_ls = sub.add_parser("ls", help="List remote directory")
    p_ls.add_argument("path", help="Remote path")
    p_ls.add_argument("-r", "--recursive", action="store_true", help="List recursively")
    p_ls.set_defaults(func=cmd_ls)

    # cp
    p_cp = sub.add_parser("cp", help="Copy files (upload or download)")
    p_cp.add_argument("src", help="Source path")
    p_cp.add_argument("dst", help="Destination path")
    p_cp.add_argument("-r", "--recursive", action="store_true", help="Recursive copy")
    p_cp.add_argument("-n", "--dry-run", action="store_true", help="Show plan without executing")
    p_cp.set_defaults(func=cmd_cp, delete=False)

    # sync
    p_sync = sub.add_parser("sync", help="Sync local ↔ remote (like aws s3 sync)")
    p_sync.add_argument("src", help="Source path")
    p_sync.add_argument("dst", help="Destination path")
    p_sync.add_argument("-n", "--dry-run", action="store_true", help="Show plan without executing")
    p_sync.add_argument("--delete", action="store_true", help="Delete dest files not in source")
    p_sync.add_argument("--exclude", action="append", default=[], help="Exclude glob pattern (repeatable)")
    p_sync.add_argument("--force", action="store_true", help="Skip confirmation prompts")
    p_sync.set_defaults(func=cmd_sync)

    # login
    p_login = sub.add_parser("login", help="Authenticate and save session cookies")
    p_login.add_argument("-i", "--interactive", action="store_true", help="Open browser window for interactive login")
    p_login.set_defaults(func=cmd_login)

    # logout
    p_logout = sub.add_parser("logout", help="Remove saved session cookies")
    p_logout.set_defaults(func=cmd_logout)

    # rm
    p_rm = sub.add_parser("rm", help="Delete remote item")
    p_rm.add_argument("path", help="Remote path")
    p_rm.add_argument("-n", "--dry-run", action="store_true", help="Show plan without executing")
    p_rm.set_defaults(func=cmd_rm)

    # mb
    p_mb = sub.add_parser("mb", help="Create remote directory")
    p_mb.add_argument("path", help="Remote path")
    p_mb.add_argument("-n", "--dry-run", action="store_true", help="Show plan without executing")
    p_mb.set_defaults(func=cmd_mb)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except SVNWebClientError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
