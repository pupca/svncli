"""CLI entry point for svncli."""

from __future__ import annotations

import argparse
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
from .sync import _file_state, _rel_path, build_local_manifest, plan_sync_download, plan_sync_upload, save_manifest
from .util import ParsedPath, fmt_size, log_verbose, normalize_remote_path, parse_path, split_remote_path


def _extract_domain(base_url: str) -> str:
    """Extract domain from a URL for cookie extraction."""
    from urllib.parse import urlparse

    return urlparse(base_url).hostname or ""


def _resolve_cookie(server: str, args: argparse.Namespace) -> str:
    """Resolve cookie string for a server from saved cookies."""
    verbose = getattr(args, "verbose", False)

    cookie = load_saved_cookies(server)
    if cookie:
        log_verbose(f"Using saved cookies for {server}", verbose)
        return cookie

    print(f"Error: not authenticated to {server}.", file=sys.stderr)
    print("Run one of:", file=sys.stderr)
    print(f"  svncli login {server}", file=sys.stderr)
    print(f"  svncli login -i {server}", file=sys.stderr)
    print(f'  svncli login --cookie "JSESSIONID=..." {server}', file=sys.stderr)
    sys.exit(1)


# ── Client cache (one per server) ───────────────────────────────────

_client_cache: dict[str, SVNWebClient] = {}


def get_client_for_server(server: str, args: argparse.Namespace) -> SVNWebClient:
    """Get or create a client for a specific server URL."""
    if server in _client_cache:
        return _client_cache[server]
    cookie = _resolve_cookie(server, args)
    verify_ssl = not args.no_verify_ssl
    timeout = getattr(args, "timeout", 60)
    client = SVNWebClient(server, cookie, verify_ssl=verify_ssl, timeout=timeout)
    _client_cache[server] = client
    return client


def resolve_remote(raw: str, args: argparse.Namespace) -> tuple[SVNWebClient, str]:
    """Parse a remote path string and return (client, remote_path)."""
    try:
        parsed = parse_path(raw)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if parsed.is_local:
        print(f"Error: expected a remote path, got local: {raw}", file=sys.stderr)
        sys.exit(1)
    client = get_client_for_server(parsed.server, args)
    return client, parsed.path


# ── ls ──────────────────────────────────────────────────────────────


def cmd_ls(args: argparse.Namespace) -> None:
    client, remote_path = resolve_remote(args.path, args)
    items = client.ls_recursive(remote_path) if getattr(args, "recursive", False) else client.ls(remote_path)
    if not items:
        if not client.validate_session():
            print(f"Error: session expired. Cannot list: {remote_path}", file=sys.stderr)
            print(
                f"Run: svncli login {args.path.rsplit(':', 1)[0] if ':' in args.path else args.path}", file=sys.stderr
            )
            sys.exit(1)
        print(f"(empty directory: {remote_path})")
        return
    for item in items:
        date_str = item.last_modified.strftime("%Y-%m-%d %H:%M") if item.last_modified else ""
        size_str = fmt_size(item.size) if not item.is_dir else "DIR"
        rev_str = str(item.revision) if item.revision else ""
        print(f"{date_str:>16s}  {size_str:>10s}  r{rev_str:<8s}  {item.name}{'/' if item.is_dir else ''}")


# ── cp ──────────────────────────────────────────────────────────────


def cmd_cp(args: argparse.Namespace) -> None:
    try:
        src_parsed = parse_path(args.src)
        dst_parsed = parse_path(args.dst)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    recursive: bool = args.recursive

    if src_parsed.is_local and dst_parsed.is_remote:
        # Upload: local → remote
        dst_client = get_client_for_server(dst_parsed.server, args)
        _cp_upload(dst_client, Path(src_parsed.path), dst_parsed.path, recursive, args)
    elif src_parsed.is_remote and dst_parsed.is_local:
        # Download: remote → local
        src_client = get_client_for_server(src_parsed.server, args)
        _cp_download(src_client, src_parsed.path, Path(dst_parsed.path), recursive, args)
    elif src_parsed.is_remote and dst_parsed.is_remote:
        # Remote → remote (cross-server copy)
        if not recursive:
            print("Error: cross-server copy requires -r", file=sys.stderr)
            sys.exit(1)
        _cp_remote_to_remote(src_parsed, dst_parsed, args)
    else:
        print("Error: at least one path must be remote.", file=sys.stderr)
        print("Remote: https://server:Repo/path", file=sys.stderr)
        print("Local: /path, ./path, ~/path", file=sys.stderr)
        sys.exit(1)


def _cp_upload(client: SVNWebClient, src: Path, dst: str, recursive: bool, args: argparse.Namespace) -> None:
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
        try:
            remote_items = client.ls_recursive(dst)
        except (SVNWebClientError, requests.exceptions.RequestException):
            remote_items = []
        actions = plan_sync_upload(src, dst, remote_items, delete=False)
        _execute_actions(client, actions, args)
    else:
        print(f"Error: {src} does not exist", file=sys.stderr)
        sys.exit(1)


def _cp_download(client: SVNWebClient, src: str, dst: Path, recursive: bool, args: argparse.Namespace) -> None:
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
        if args.dry_run:
            print(f"(dry-run) download directory: {src} → {dst}")
            return
        log_verbose(f"Downloading {src} as zip...", args.verbose)
        zip_bytes = client.download_directory_zip_to_buffer(src)
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            # Validate zip entries to prevent path traversal (Zip Slip)
            dst_resolved = dst.resolve()
            for name in zf.namelist():
                target = (dst / name).resolve()
                if not str(target).startswith(str(dst_resolved)):
                    raise SVNWebClientError(f"Zip contains path traversal entry: {name}")
            zf.extractall(dst)
        print(f"download: {src} → {dst} ({len(zip_bytes)} bytes)")


def _cp_remote_to_remote(src: ParsedPath, dst: ParsedPath, args: argparse.Namespace) -> None:
    """Copy between two remote servers via a local temp directory."""
    import tempfile

    src_client = get_client_for_server(src.server, args)
    dst_client = get_client_for_server(dst.server, args)

    if args.dry_run:
        print(f"(dry-run) remote copy: {src} → {dst}")
        return

    with tempfile.TemporaryDirectory(prefix="svncli_") as tmp:
        tmp_dir = Path(tmp)
        # Download from source
        log_verbose(f"Downloading from {src}...", args.verbose)
        src_items = src_client.ls_recursive(src.path)
        src_prefix = src.path.rstrip("/") + "/"
        for item in src_items:
            if item.is_dir:
                continue
            rel = _rel_path(item.path, src_prefix, item.name)
            dest = tmp_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_client.download_file(item.path, dest)

        # Upload to destination
        log_verbose(f"Uploading to {dst}...", args.verbose)
        try:
            dst_items = dst_client.ls_recursive(dst.path)
        except (SVNWebClientError, requests.exceptions.RequestException):
            dst_items = []
        actions = plan_sync_upload(tmp_dir, dst.path, dst_items, delete=False)
        _execute_actions(dst_client, actions, args)


# ── sync ────────────────────────────────────────────────────────────


def cmd_sync(args: argparse.Namespace) -> None:
    try:
        src_parsed = parse_path(args.src)
        dst_parsed = parse_path(args.dst)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if src_parsed.is_local and dst_parsed.is_remote:
        dst_client = get_client_for_server(dst_parsed.server, args)
        log_verbose("Sync direction: local → remote (upload)", args.verbose)
        _sync_upload(dst_client, Path(src_parsed.path), dst_parsed.path, args)
    elif src_parsed.is_remote and dst_parsed.is_local:
        src_client = get_client_for_server(src_parsed.server, args)
        log_verbose("Sync direction: remote → local (download)", args.verbose)
        _sync_download(src_client, src_parsed.path, Path(dst_parsed.path), args)
    elif src_parsed.is_remote and dst_parsed.is_remote:
        log_verbose("Sync direction: remote → remote", args.verbose)
        _sync_remote_to_remote(src_parsed, dst_parsed, args)
    else:
        print("Error: at least one path must be remote.", file=sys.stderr)
        sys.exit(1)


def _sync_upload(client: SVNWebClient, local_dir: Path, remote_path: str, args: argparse.Namespace) -> None:
    remote_path = normalize_remote_path(remote_path)
    if not local_dir.is_dir():
        print(f"Error: {local_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    log_verbose(f"Listing remote: {remote_path} (recursive)...", args.verbose)
    _remote_exists = True
    try:
        remote_items = client.ls_recursive(remote_path)
    except (SVNWebClientError, requests.exceptions.RequestException):
        log_verbose(f"Remote path {remote_path} does not exist, will create it", args.verbose)
        remote_items = []
        _remote_exists = False

    log_verbose(f"Found {len(remote_items)} remote items", args.verbose)

    exclude = getattr(args, "exclude", [])
    actions = plan_sync_upload(local_dir, remote_path, remote_items, delete=args.delete, exclude=exclude)

    # If remote dir doesn't exist, prepend mkdir for the root
    if not remote_items and not _remote_exists:
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


def _sync_remote_to_remote(src: ParsedPath, dst: ParsedPath, args: argparse.Namespace) -> None:
    """Sync between two remote servers via a local temp directory."""
    import tempfile

    src_client = get_client_for_server(src.server, args)
    dst_client = get_client_for_server(dst.server, args)

    with tempfile.TemporaryDirectory(prefix="svncli_sync_") as tmp:
        tmp_dir = Path(tmp)

        # Step 1: download source to temp
        log_verbose(f"Downloading from {src}...", args.verbose)
        try:
            src_items = src_client.ls_recursive(src.path)
        except (SVNWebClientError, requests.exceptions.RequestException) as e:
            print(f"Error: cannot list source {src}: {e}", file=sys.stderr)
            sys.exit(1)

        src_prefix = src.path.rstrip("/") + "/"
        for item in src_items:
            if item.is_dir:
                continue
            rel = _rel_path(item.path, src_prefix, item.name)
            dest = tmp_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_client.download_file(item.path, dest)

        # Step 2: sync temp to destination
        log_verbose(f"Syncing to {dst}...", args.verbose)
        try:
            dst_items = dst_client.ls_recursive(dst.path)
        except (SVNWebClientError, requests.exceptions.RequestException):
            dst_items = []

        exclude = getattr(args, "exclude", [])
        actions = plan_sync_upload(tmp_dir, dst.path, dst_items, delete=args.delete, exclude=exclude)

        if not dst_items:
            actions.insert(0, SyncAction(op=SyncOp.MKDIR, remote_path=dst.path, reason="new directory"))

        _execute_actions(dst_client, actions, args)


# ── rm / mb ─────────────────────────────────────────────────────────


def cmd_login(args: argparse.Namespace) -> None:
    """Log in and save session cookies."""
    server = getattr(args, "server", None)
    if not server:
        print("Error: provide a server URL, e.g.: svncli login https://your-server.com", file=sys.stderr)
        sys.exit(1)
    verify_ssl = not args.no_verify_ssl
    timeout = getattr(args, "timeout", 60)
    manual_cookie = getattr(args, "cookie", None)

    if manual_cookie:
        # Method 3: manual cookie string
        cookie = manual_cookie
        save_cookies(server, cookie)
        print(f"Cookie saved for {server}")
    elif getattr(args, "interactive", False):
        # Method 2: interactive browser login
        try:
            cookie = interactive_login(server)
        except SVNWebClientError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Cookies saved for {server}")
    else:
        # Method 1: auto-extract from browser
        domain = _extract_domain(server)
        browser = getattr(args, "browser", None) or "chrome"
        try:
            cookie = extract_browser_cookies(domain, browser)
        except SVNWebClientError as e:
            print(f"Could not extract cookies from {browser}: {e}", file=sys.stderr)
            print("Try one of:", file=sys.stderr)
            print(f"  svncli login -i {server}", file=sys.stderr)
            print(f'  svncli login --cookie "JSESSIONID=..." {server}', file=sys.stderr)
            sys.exit(1)

        cookie_names = [p.split("=")[0].strip() for p in cookie.split(";")]
        print(f"Extracted {len(cookie_names)} cookies from {browser} for {domain}")
        save_cookies(server, cookie)
        print(f"Cookies saved for {server}")

    # Verify session works
    client = SVNWebClient(server, cookie, verify_ssl=verify_ssl, timeout=timeout)
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

    server = getattr(args, "server", None)
    if server and COOKIE_FILE.exists():
        try:
            import json

            data = json.loads(COOKIE_FILE.read_text())
            key = server.rstrip("/")
            if key in data:
                del data[key]
                if data:
                    COOKIE_FILE.write_text(json.dumps(data, indent=2))
                else:
                    COOKIE_FILE.unlink()
                print(f"Logged out from {server}")
                return
        except (json.JSONDecodeError, OSError):
            pass
    if not server and COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
        print("Removed all saved cookies.")
        return
    print("No saved cookies found.")


def cmd_rm(args: argparse.Namespace) -> None:
    client, remote_path = resolve_remote(args.path, args)
    parent, name = split_remote_path(remote_path)
    if not parent:
        print("Error: cannot delete repository root", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "dry_run", False):
        print(f"(dry-run) delete: {remote_path}")
        return
    client.delete_items(parent, [name])
    print(f"delete: {remote_path}")


def cmd_mb(args: argparse.Namespace) -> None:
    client, remote_path = resolve_remote(args.path, args)
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
                    parent, name = split_remote_path(action.remote_path)
                    if parent:
                        client.delete_items(parent, [name])
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
            rel = _rel_path(item.path, remote_prefix, item.name)
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
    from importlib.metadata import version

    parser.add_argument("--version", action="version", version=f"%(prog)s {version('svncli')}")
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
    _browsers = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "vivaldi", "safari", "librewolf", "arc"]
    p_login.add_argument("server", nargs="?", help="Server URL (e.g. https://your-server.com)")
    p_login.add_argument("-i", "--interactive", action="store_true", help="Open browser window for interactive login")
    p_login.add_argument("--cookie", help="Save a cookie string directly (from browser DevTools)")
    p_login.add_argument(
        "--browser",
        default="chrome",
        choices=_browsers,
        help="Browser to extract cookies from (default: chrome)",
    )
    p_login.set_defaults(func=cmd_login)

    # logout
    p_logout = sub.add_parser("logout", help="Remove saved session cookies")
    p_logout.add_argument("server", nargs="?", help="Server URL (omit to remove all)")
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
    except requests.exceptions.RequestException as e:
        print(f"Error: network request failed: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
