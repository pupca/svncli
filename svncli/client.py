"""SVNWebClient HTTP client.

All knowledge of JSP endpoints, form fields, cookies, and HTML parsing
is isolated in this module.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path

import requests
import urllib3

from .models import RemoteItem
from .util import encode_svn_path, normalize_remote_path, split_remote_path

# ── Browser cookie extraction ───────────────────────────────────────


COOKIE_FILE = Path.home() / ".svncli" / "cookies.json"

DEFAULT_TIMEOUT = 60  # seconds


def extract_browser_cookies(domain: str, browser: str = "chrome") -> str:
    """Extract cookies for a domain from a browser's cookie store.

    Returns a cookie header string like 'name=value; name2=value2'.
    """
    try:
        import browser_cookie3
    except ImportError as err:
        raise SVNWebClientError(
            "browser-cookie3 is required for browser cookie extraction. Install it with: pip install browser-cookie3"
        ) from err

    browser_fn = getattr(browser_cookie3, browser, None)
    if browser_fn is None:
        raise SVNWebClientError(
            f"Unknown browser: {browser}. Supported: chrome, firefox, opera, edge, chromium, brave, vivaldi, safari"
        )

    try:
        cj = browser_fn(domain_name=domain)
    except Exception as e:
        raise SVNWebClientError(f"Failed to extract cookies from {browser}: {e}") from e

    cookies = [c for c in cj if domain in (c.domain or "")]
    if not cookies:
        raise SVNWebClientError(
            f"No cookies found for {domain} in {browser}. Make sure you are logged in via the browser first."
        )

    return "; ".join(f"{c.name}={c.value}" for c in cookies)


def interactive_login(base_url: str) -> str:
    """Open a browser window for the user to log in, then capture cookies.

    Returns a cookie header string.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise SVNWebClientError(
            "playwright is required for interactive login. "
            "Install it with: pip install playwright && playwright install chromium"
        ) from err

    login_url = base_url.rstrip("/") + "/polarion"
    domain = base_url.rstrip("/").split("//")[1].split("/")[0]

    print(f"Opening browser for login at {login_url}")
    print("Log in, then close the browser window to continue.")

    cookies = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(login_url)

        # Wait until user closes the browser window
        try:
            while True:
                # This will throw when the page/browser is closed
                page.wait_for_timeout(500)
                if not browser.contexts:
                    break
        except Exception:
            # Browser was closed — this is the expected exit path
            pass

        with contextlib.suppress(Exception):
            cookies = context.cookies()
        with contextlib.suppress(Exception):
            browser.close()

    domain_cookies = [c for c in cookies if domain in c.get("domain", "")]
    if not domain_cookies:
        raise SVNWebClientError("No cookies captured. Login may have failed.")

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in domain_cookies)
    save_cookies(base_url, cookie_str)
    return cookie_str


def save_cookies(base_url: str, cookie_str: str) -> None:
    """Save cookies to ~/.svncli/cookies.json keyed by base URL."""
    import os

    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if COOKIE_FILE.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data = json.loads(COOKIE_FILE.read_text())
    data[base_url.rstrip("/")] = cookie_str
    # Write atomically with correct permissions to avoid exposing cookies
    fd = os.open(str(COOKIE_FILE.parent / ".cookies.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        Path(COOKIE_FILE.parent / ".cookies.tmp").replace(COOKIE_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(COOKIE_FILE.parent / ".cookies.tmp").unlink()
        raise


def load_saved_cookies(base_url: str) -> str | None:
    """Load cookies from ~/.svncli/cookies.json for a base URL."""
    if not COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(COOKIE_FILE.read_text())
        return data.get(base_url.rstrip("/"))
    except (json.JSONDecodeError, OSError):
        return None


class SVNWebClientError(Exception):
    pass


class AuthenticationError(SVNWebClientError):
    pass


class NotFoundError(SVNWebClientError):
    pass


class SVNWebClient:
    """HTTP client for Polarion SVN Web Client (JSP-based)."""

    def __init__(self, base_url: str, cookie: str, verify_ssl: bool = True, timeout: int = DEFAULT_TIMEOUT) -> None:
        """
        Args:
            base_url: e.g. "https://cns.net.plm.eds.com"
            cookie: Raw cookie header string from browser.
            verify_ssl: Whether to verify SSL certificates (disable for corp CAs).
            timeout: HTTP request timeout in seconds.
        """
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/polarion/svnwebclient"):
            base_url += "/polarion/svnwebclient"
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        # Parse cookie string into the session jar
        self._set_cookies(cookie)

    def _set_cookies(self, cookie_str: str) -> None:
        """Parse 'key=value; key2=value2' string into session cookies."""
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                self.session.cookies.set(name.strip(), value.strip())

    def _url(self, jsp: str, path: str | None = None, **params: str) -> str:
        """Build a full URL for a JSP endpoint."""
        url = f"{self.base_url}/{jsp}"
        if path is not None:
            params["url"] = encode_svn_path(path)
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        return url

    def _get(self, jsp: str, path: str | None = None, **params: str) -> requests.Response:
        url = self._url(jsp, path, **params)
        resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp)
        return resp

    def _check_response(self, resp: requests.Response) -> None:
        """Detect auth failures and other errors."""
        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"Authentication failed ({resp.status_code}). Your session cookie may have expired.\n"
                f"Run: svncli login {self.base_url.split('/polarion')[0]}"
            )
        if "login" in resp.url.lower() and "svnwebclient" not in resp.url.lower():
            raise AuthenticationError(
                f"Session expired — redirected to login page.\nRun: svncli login {self.base_url.split('/polarion')[0]}"
            )
        # Some servers return 200 with a login form instead of redirecting
        if resp.status_code == 200 and ('name="j_username"' in resp.text or 'id="loginForm"' in resp.text):
            raise AuthenticationError(
                "Session expired — server returned a login form.\n"
                f"Run: svncli login {self.base_url.split('/polarion')[0]}"
            )
        if resp.status_code == 404:
            raise NotFoundError(f"Not found: {resp.url}")
        resp.raise_for_status()

    def validate_session(self) -> bool:
        """Check if the current session is still valid by probing the web client root."""
        resp = self.session.get(
            f"{self.base_url}/directoryContent.jsp",
            allow_redirects=True,
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            return False
        if "login" in resp.url.lower() and "svnwebclient" not in resp.url.lower():
            return False
        if 'name="j_username"' in resp.text or 'id="loginForm"' in resp.text:
            return False
        return resp.status_code == 200

    # ── Directory listing ────────────────────────────────────────────

    _HIDDEN_RE = re.compile(
        r'<input\s+type="hidden"\s+name="(?P<name>[^"]+)"'
        r'(?:\s+[a-z]+="[^"]*")*'
        r'\s+value="(?P<value>[^"]*)"',
        re.IGNORECASE,
    )

    def ls(self, remote_path: str) -> list[RemoteItem]:
        """List contents of a remote directory."""
        remote_path = normalize_remote_path(remote_path)
        resp = self._get("directoryContent.jsp", remote_path)
        return self._parse_directory_listing(resp.text, remote_path)

    def _parse_directory_listing(self, html: str, parent_path: str) -> list[RemoteItem]:
        """Parse hidden input arrays from the dir_list form."""
        # Extract all hidden inputs
        fields: dict[str, list[str]] = {}
        for m in self._HIDDEN_RE.finditer(html):
            name = m.group("name")
            value = unescape(m.group("value"))
            fields.setdefault(name, []).append(value)

        names = fields.get("names", [])
        types = fields.get("types", [])
        sizes = fields.get("sizes", [])
        revisions = fields.get("revisions", [])
        dates = fields.get("dates", [])
        authors = fields.get("authors", [])
        comments = fields.get("comments", [])

        items: list[RemoteItem] = []
        for i, name in enumerate(names):
            is_dir = "directory" in (types[i] if i < len(types) else "")
            size_str = sizes[i] if i < len(sizes) else ""
            # Size may have space as thousands separator (e.g. "3 138")
            size_clean = size_str.replace(" ", "").replace("\u00a0", "")
            size = None if is_dir or not size_clean.isdigit() else int(size_clean)

            rev_str = (revisions[i] if i < len(revisions) else "").replace(" ", "")
            revision = int(rev_str) if rev_str.isdigit() else None

            date_str = dates[i] if i < len(dates) else ""
            last_modified = None
            if date_str:
                with contextlib.suppress(ValueError):
                    last_modified = datetime.strptime(date_str, "%Y-%m-%d %H:%M")

            author = authors[i] if i < len(authors) else None
            comment = comments[i] if i < len(comments) else None

            child_path = f"{parent_path}/{name}" if parent_path else name
            items.append(
                RemoteItem(
                    name=name,
                    path=child_path,
                    is_dir=is_dir,
                    size=size,
                    revision=revision,
                    last_modified=last_modified,
                    author=author,
                    comment=comment,
                )
            )
        return items

    def ls_recursive(self, remote_path: str) -> list[RemoteItem]:
        """Recursively list all files and directories under a path."""
        remote_path = normalize_remote_path(remote_path)
        all_items: list[RemoteItem] = []
        stack = [remote_path]
        while stack:
            current = stack.pop()
            items = self.ls(current)
            for item in items:
                all_items.append(item)
                if item.is_dir:
                    stack.append(item.path)
        return all_items

    # ── File download ────────────────────────────────────────────────

    def download_file(self, remote_path: str, dest: Path) -> None:
        """Download a single file to a local path."""
        remote_path = normalize_remote_path(remote_path)
        url = self._url("fileDownload.jsp", remote_path, attachment="true")
        with self.session.get(url, stream=True, allow_redirects=True, timeout=self.timeout) as resp:
            self._check_response(resp)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

    def download_file_to_buffer(self, remote_path: str) -> bytes:
        """Download a single file and return its content as bytes."""
        remote_path = normalize_remote_path(remote_path)
        url = self._url("fileDownload.jsp", remote_path, attachment="true")
        resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp)
        return resp.content

    def download_directory_zip(self, remote_path: str, dest: Path) -> None:
        """Download a directory as a zip file."""
        remote_path = normalize_remote_path(remote_path)
        url = self._url("downloadDirectory.jsp", remote_path)
        with self.session.get(url, stream=True, allow_redirects=True, timeout=self.timeout) as resp:
            self._check_response(resp)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

    def download_directory_zip_to_buffer(self, remote_path: str) -> bytes:
        """Download a directory as a zip and return raw bytes."""
        remote_path = normalize_remote_path(remote_path)
        url = self._url("downloadDirectory.jsp", remote_path)
        resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp)
        return resp.content

    # ── Write operations ───────────────────────────────────────────────

    def upload_file(self, remote_path: str, local_path: Path, commit_message: str = "File was added remotely") -> None:
        """Upload a file to a remote directory.

        remote_path is the full path including the filename,
        e.g. "Repo/folder/file.txt". The parent directory must exist.
        """
        remote_path = normalize_remote_path(remote_path)
        parent_path, filename = split_remote_path(remote_path)

        url = self._url("fileAddAction.jsp", parent_path)
        with open(local_path, "rb") as f:
            files = {"filepath": (filename, f)}
            data = {"comment": commit_message}
            resp = self.session.post(url, files=files, data=data, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp)
        if "successfully added" not in resp.text and "successfully" not in resp.text.lower():
            raise SVNWebClientError(f"Upload may have failed for {remote_path}. Check server response.")

    def update_file(
        self, remote_path: str, local_path: Path, commit_message: str = "File was updated remotely"
    ) -> None:
        """Update (overwrite) an existing remote file.

        Uses fileUpdate.jsp flow for committing changes to existing files.
        Falls back to upload_file if update endpoint is not available.
        """
        # Try the update flow first — some SVN web clients use a different
        # endpoint for updating vs adding. If fileUpdateAction.jsp doesn't
        # exist, fall back to the add flow.
        remote_path = normalize_remote_path(remote_path)
        url = self._url("fileUpdateAction.jsp", remote_path)
        with open(local_path, "rb") as f:
            files = {"filepath": (local_path.name, f)}
            data = {"comment": commit_message}
            resp = self.session.post(url, files=files, data=data, allow_redirects=True, timeout=self.timeout)
        try:
            self._check_response(resp)
        except NotFoundError:
            # Update endpoint not available — fall back to add
            self.upload_file(remote_path, local_path, commit_message)

    def mkdir(self, remote_path: str, commit_message: str = "Directory was added remotely") -> None:
        """Create a remote directory."""
        remote_path = normalize_remote_path(remote_path)
        parent_path, dirname = split_remote_path(remote_path)

        url = self._url("directoryAddAction.jsp", parent_path)
        data = {"directoryname": dirname, "comment": commit_message}
        resp = self.session.post(url, data=data, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp)
        if "successfully added" not in resp.text and "successfully" not in resp.text.lower():
            raise SVNWebClientError(f"mkdir may have failed for {remote_path}. Check server response.")

    def delete_items(
        self, parent_path: str, item_names: list[str], commit_message: str = "Elements were deleted remotely"
    ) -> None:
        """Delete specific items from a remote directory.

        Two-step process:
        1. POST to delete.jsp with full listing data + flags marking items to delete
        2. POST to deleteAction.jsp with just the comment
        """
        parent_path = normalize_remote_path(parent_path)

        # First, get the directory listing to build the form data
        items = self.ls(parent_path)
        if not items:
            raise SVNWebClientError(f"Directory {parent_path} is empty or does not exist")

        names_to_delete = set(item_names)

        # Build form data matching the dir_list form structure
        # Each field is repeated for every item in the directory
        form_data: list[tuple[str, str]] = []
        for item in items:
            selected = item.name in names_to_delete
            form_data.append(("flags", "1" if selected else "0"))
            form_data.append(("types", f"images/{'directory' if item.is_dir else 'file'}.gif"))
            form_data.append(("names", item.name))
            form_data.append(("revisions", str(item.revision or "")))
            form_data.append(("sizes", "<DIR>" if item.is_dir else str(item.size or "")))
            date_str = item.last_modified.strftime("%Y-%m-%d %H:%M") if item.last_modified else ""
            form_data.append(("dates", date_str))
            form_data.append(("ages", ""))
            form_data.append(("authors", item.author or ""))
            form_data.append(("comments", item.comment or ""))
            if selected:
                form_data.append(("items", "on"))

        # Step 1: POST to delete.jsp → confirmation page
        url1 = self._url("delete.jsp", parent_path)
        resp1 = self.session.post(url1, data=form_data, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp1)

        # Step 2: POST to deleteAction.jsp → actual deletion
        url2 = self._url("deleteAction.jsp", parent_path)
        resp2 = self.session.post(url2, data={"comment": commit_message}, allow_redirects=True, timeout=self.timeout)
        self._check_response(resp2)

        if "successfully deleted" not in resp2.text and "successfully" not in resp2.text.lower():
            raise SVNWebClientError(f"Delete may have failed for {item_names} in {parent_path}")
