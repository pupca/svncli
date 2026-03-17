# Polarion SVN CLI (svncli)

[![CI](https://github.com/pupca/svncli/actions/workflows/ci.yml/badge.svg)](https://github.com/pupca/svncli/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

CLI tool for syncing files with Polarion's SVN repository when direct SVN access is not available — such as on PolarionX or other hosted environments.

Supports **macOS**, **Linux**, and **Windows**.

## Why?

Some Polarion deployments don't expose direct SVN access. You can browse and manage individual files through the web UI, but there's no built-in way to recursively upload folders, sync changes, or automate file management. **svncli** fills that gap.

## Installation

```bash
pip install svncli
```

For interactive browser login (optional):

```bash
pip install "svncli[interactive]"
playwright install chromium
```

### From source

```bash
git clone https://github.com/pupca/svncli.git
cd svncli
pip install -e ".[dev]"
```

## Quick start

### 1. Authenticate

Pick one of these methods:

```bash
# Auto-extract cookies from Chrome (requires being logged in via browser)
svncli login https://your-server.example.com

# Interactive login — opens a browser window (works with SSO/MFA)
svncli login -i https://your-server.example.com

# Manual cookie — paste from browser DevTools (Network tab → Copy as cURL → extract cookie)
svncli --cookie "JSESSIONID=ABC123; X-CSRF-Token=DEF456" ls https://your-server.example.com:MyProject
```

Sessions are saved to `~/.svncli/cookies.json` — you only need to log in once per server.

### 2. Browse

```bash
svncli ls https://your-server.example.com:MyProject/trunk
svncli ls -r https://your-server.example.com:MyProject/trunk   # recursive
```

### 3. Copy files

```bash
# Upload
svncli cp ./report.pdf https://your-server.example.com:MyProject/trunk/docs/report.pdf

# Download
svncli cp https://your-server.example.com:MyProject/trunk/docs/report.pdf ./report.pdf

# Copy a whole folder
svncli cp -r ./local-folder https://your-server.example.com:MyProject/trunk/folder
```

### 4. Sync

```bash
# Push local changes to remote
svncli sync ./src https://your-server.example.com:MyProject/trunk/src

# Pull remote changes to local
svncli sync https://your-server.example.com:MyProject/trunk/src ./src

# Preview what would change
svncli sync -n ./src https://your-server.example.com:MyProject/trunk/src

# Delete remote files not present locally
svncli sync --delete --force ./src https://your-server.example.com:MyProject/trunk/src
```

### 5. Cross-server copy

```bash
svncli cp -r https://server1.com:Project/src https://server2.com:Project/src
```

## Path format

Remote paths include the server URL followed by `:` and the repository path:

```
https://server.example.com:RepoName/folder/path
```

Local paths start with `/`, `./`, or `~/`:

```
./local-folder
/absolute/path
~/home-relative
```

## Commands

| Command | Description |
|---------|-------------|
| `svncli ls <remote>` | List remote directory (`-r` for recursive) |
| `svncli cp <src> <dst>` | Copy files (local↔remote or remote↔remote with `-r`) |
| `svncli sync <src> <dst>` | Sync files between local and remote |
| `svncli rm <remote>` | Delete a remote file or folder |
| `svncli mb <remote>` | Create a remote directory |
| `svncli login <server>` | Authenticate and save session cookies |
| `svncli logout [server]` | Remove saved session cookies |

## Global options

```
--cookie STRING    Cookie string (or SVNCLI_COOKIE env)
--browser NAME     Browser for cookie extraction (or SVNCLI_BROWSER env, default: chrome)
--no-verify-ssl    Disable SSL certificate verification
--timeout SECONDS  HTTP request timeout (default: 60)
-v, --verbose      Verbose output
--version          Show version
```

## Sync options

```
-n, --dry-run      Show what would change without doing it
--delete           Remove destination files not present in source
--force            Skip confirmation prompt for deletions
--exclude PATTERN  Exclude files matching glob pattern (repeatable)
```

## Authentication

Each server has its own saved session. svncli resolves cookies per server in this order:

1. `--cookie` flag or `SVNCLI_COOKIE` environment variable
2. Saved cookies from `~/.svncli/cookies.json` (from a previous `svncli login`)
3. Auto-extraction from browser cookie store (Chrome, Firefox, Edge, Safari, etc.)

### Automatic cookie extraction

By default, svncli reads cookies directly from your Chrome cookie store. This requires that you've already logged into Polarion in Chrome. To use a different browser:

```bash
svncli --browser firefox login https://your-server.com
# or
export SVNCLI_BROWSER=firefox
```

### Interactive login

For environments where automatic cookie extraction doesn't work (e.g., remote servers, locked-down browsers):

```bash
svncli login -i https://your-server.com
```

This opens a Chromium window. Log in normally (including SSO/MFA), then close the window. Cookies are saved automatically.

Requires the `interactive` extra: `pip install "svncli[interactive]"` and `playwright install chromium`.

### Manual cookie extraction

If neither automatic nor interactive login works, you can extract cookies manually from your browser:

1. Open your Polarion server in Chrome/Firefox
2. Log in normally
3. Open Developer Tools (F12 or Cmd+Option+I)
4. Go to the **Network** tab
5. Navigate to any page on the Polarion server (e.g. refresh the page)
6. Click on any request to the server
7. In the **Headers** section, find the `Cookie` request header
8. Copy the entire cookie value string (e.g. `JSESSIONID=ABC123; X-CSRF-Token=DEF456; ...`)

Then pass it to svncli:

```bash
# Via flag
svncli --cookie "JSESSIONID=ABC123; X-CSRF-Token=DEF456" ls MyProject

# Or via environment variable (recommended — avoids repeating it)
export SVNCLI_COOKIE="JSESSIONID=ABC123; X-CSRF-Token=DEF456"
svncli ls MyProject
```

> **Tip:** In Chrome, you can also right-click a request in the Network tab → **Copy → Copy as cURL**, then extract the cookie value from the `-b` or `--cookie` flag in the copied command.

## How sync works

svncli tracks sync state in manifest files stored in `~/.svncli/manifests/`. On each sync:

1. **First sync** — compares by file size; uploads/downloads differences
2. **Subsequent syncs** — uses SVN revision numbers + local file SHA-256 hashes:
   - Local file unchanged + remote revision unchanged → **skip** (fast, no hashing needed)
   - Local file changed → **upload**
   - Remote revision changed, local unchanged → **skip** (remote is newer)
   - New local file → **upload**
   - New remote file → **download** (in download direction)

The manifest enables efficient incremental syncs without downloading remote files to compare.

## Corporate environments

For servers with self-signed or corporate CA certificates:

```bash
svncli --no-verify-ssl ls MyProject
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `SVNCLI_COOKIE` | Cookie header string |
| `SVNCLI_BROWSER` | Browser for cookie extraction (default: `chrome`) |

## Development

```bash
git clone https://github.com/pupca/svncli.git
cd svncli
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run unit tests (no server needed)
pytest tests/test_sync.py tests/test_client.py tests/test_models.py tests/test_util.py -v

# Run full suite including E2E (requires server access)
export SVNCLI_SERVER="https://your-server.example.com"
export SVNCLI_E2E_ROOT="TestProject"
pytest tests/ -v --cov=svncli --cov-report=html
```

## License

[MIT](LICENSE)
