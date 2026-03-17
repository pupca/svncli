# Polarion SVN CLI (svncli)

CLI tool for syncing files with Polarion's SVN repository when direct SVN access is not available — such as on PolarionX or other hosted environments.

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

```bash
# Set your Polarion server URL
export SVNCLI_BASE_URL="https://your-polarion-server.example.com"

# Authenticate (extracts cookies from Chrome automatically)
svncli login

# Or use interactive browser login (works with SSO/MFA)
svncli login -i

# List files
svncli ls MyProject/trunk

# Upload a file
svncli cp ./report.pdf MyProject/trunk/docs/report.pdf

# Download a file
svncli cp MyProject/trunk/docs/report.pdf ./report.pdf

# Sync a local folder to remote
svncli sync ./src MyProject/trunk/src

# Sync remote to local
svncli sync MyProject/trunk/src ./src

# Preview changes without applying
svncli sync -n ./src MyProject/trunk/src
```

## Commands

| Command | Description |
|---------|-------------|
| `svncli ls <path>` | List remote directory (`-r` for recursive) |
| `svncli cp <src> <dst>` | Copy files between local and remote (`-r` for directories) |
| `svncli sync <src> <dst>` | Sync files between local and remote |
| `svncli rm <path>` | Delete a remote file or folder |
| `svncli mb <path>` | Create a remote directory |
| `svncli login` | Authenticate and save session cookies |
| `svncli logout` | Remove saved session cookies |

**Direction is determined by path format:**
- Local paths start with `/`, `./`, or `~`
- Everything else is treated as a remote path

## Global options

```
--base-url URL     Polarion server URL (or SVNCLI_BASE_URL env)
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

svncli resolves session cookies in this order:

1. `--cookie` flag or `SVNCLI_COOKIE` environment variable
2. Saved cookies from `~/.svncli/cookies.json` (from a previous `svncli login`)
3. Auto-extraction from browser cookie store (Chrome, Firefox, Edge, Safari, etc.)
4. Interactive login (`svncli login -i`) — opens a browser window

### Automatic cookie extraction

By default, svncli reads cookies directly from your Chrome cookie store. This requires that you've already logged into Polarion in Chrome. To use a different browser:

```bash
svncli --browser firefox login
# or
export SVNCLI_BROWSER=firefox
```

### Interactive login

For environments where automatic cookie extraction doesn't work (e.g., remote servers, locked-down browsers):

```bash
svncli login -i
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

svncli tracks sync state in a `.svncli.json` manifest file in the local directory. On each sync:

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
| `SVNCLI_BASE_URL` | Polarion server URL |
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
export SVNCLI_BASE_URL="https://your-server.example.com"
export SVNCLI_E2E_ROOT="TestProject"
pytest tests/ -v --cov=svncli --cov-report=html
```

## License

[MIT](LICENSE)
