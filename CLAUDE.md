# CLAUDE.md

## Project overview

Polarion SVN CLI (svncli) — a CLI tool and Python library for syncing files with Polarion's SVN repository when direct SVN access is not available (e.g. PolarionX, hosted environments). It reverse-engineers the JSP-based SVN Web Client web UI to provide `ls`, `cp`, `sync`, `rm`, `mkdir`, `login`/`logout` commands.

## Architecture

```
svncli/
├── api.py       # PolarionSVNClient — high-level Python API (multi-server, stateless)
├── cli.py       # CLI entry point (argparse, thin wrappers around api.py/client.py)
├── client.py    # SVNWebClient — low-level HTTP client (one per server, all JSP endpoint knowledge)
├── models.py    # RemoteItem, SyncAction, SyncOp dataclasses
├── sync.py      # Sync engine — manifest tracking, diff planning, no side effects
└── util.py      # Path parsing (server:path format), URL encoding, helpers
```

**Key isolation rule:** Only `client.py` knows about JSP endpoints, form fields, HTML parsing, and cookie handling. Everything else works with clean Python objects.

## Path format

Remote paths always include the server: `https://server.com:Repo/folder/path`
Local paths start with `/`, `./`, or `~/`
Bare paths (like `Repo/folder`) are rejected with a helpful error.

## Authentication

Cookies are saved per-server in `~/.svncli/cookies.json` (chmod 600).
Sync manifests are in `~/.svncli/manifests/` (not in the working directory).

Resolution order: saved cookies → browser extraction (browser-cookie3) → error with instructions.
`--cookie` and `--browser` are login-only flags, not global.

## Endpoints (reverse-engineered from HAR captures)

- `directoryContent.jsp?url=<path>` — GET, directory listing (hidden input arrays: names, types, sizes, revisions, dates, authors, comments, flags)
- `fileDownload.jsp?url=<path>&attachment=true` — GET, download file
- `downloadDirectory.jsp?url=<path>` — GET, download directory as zip
- `fileAddAction.jsp?url=<parent>` — POST multipart, upload file (fields: filepath, comment)
- `fileUpdateAction.jsp?url=<path>` — POST multipart, update existing file
- `directoryAddAction.jsp?url=<parent>` — POST, create directory (fields: directoryname, comment)
- `delete.jsp?url=<parent>` — POST, step 1 of delete (sends full listing with flags)
- `deleteAction.jsp?url=<parent>` — POST, step 2 of delete (fields: comment)

Path encoding: forward slashes as `%2F` in the `url=` query parameter.
Sizes in listings use space as thousands separator (e.g. `"3 138"`).
No CSRF tokens in forms — just session cookies.
Base URL auto-appends `/polarion/svnwebclient` if not present.

## Testing

```bash
# Unit tests only (no server needed)
pytest tests/test_sync.py tests/test_client.py tests/test_models.py tests/test_util.py -v

# Full suite (requires Polarion server access)
# Server config is in .test-servers (git-ignored, see format below)
source .test-servers
pytest tests/ -v --cov=svncli --cov-report=html

# Subprocess coverage (for CLI tests)
COVERAGE_PROCESS_START="pyproject.toml" pytest tests/ --cov=svncli
```

### `.test-servers` format

Create a `.test-servers` file in the project root (git-ignored) with your Polarion server details:

```bash
export SVNCLI_SERVER_A="https://your-primary-server.example.com"
export SVNCLI_ROOT_A="ProjectName"           # writable path on server A
export SVNCLI_SERVER_B="https://your-second-server.example.com"
export SVNCLI_ROOT_B="ProjectName/SubPath"   # writable path on server B
```

- `SERVER_A` + `ROOT_A` are required for E2E and CLI tests
- `SERVER_B` + `ROOT_B` are required for cross-server tests (skipped if not set)
- Both roots must be writable — tests create and delete temporary folders
- You must be authenticated (`svncli login`) to both servers before running

Test files:
- `test_sync.py` — sync engine unit tests (manifest, hashing, diff planning, exclude patterns)
- `test_client.py` — HTML parser, URL builder, path parsing
- `test_models.py` — data model string formatting
- `test_util.py` — utility functions
- `test_e2e.py` — end-to-end against live servers (single-server, CLI with server:path, cross-server)
- `test_cli.py` — CLI subprocess tests (help, errors, ls, cp, sync, rm, mb, login)
- `test_unhappy.py` — error handling (bad paths, bad auth, unreachable servers, edge cases)
- `test_api.py` — PolarionSVNClient Python API tests

Autouse fixture in test_sync.py monkeypatches `MANIFEST_DIR` to a temp dir.
E2E tests auto-create and clean up test folders with UUID names.

## Build & CI

- Python 3.10–3.14, macOS + Linux + Windows
- `pip install -e ".[dev]"` for development
- Ruff for linting/formatting (`ruff check`, `ruff format`)
- GitHub Actions: `.github/workflows/ci.yml` (test matrix + lint), `publish.yml` (PyPI on release)
- `browser-cookie3` for cookie extraction (not rookiepy — rookiepy doesn't support 3.13+)
- `playwright` is optional (`pip install "svncli[interactive]"`) for interactive login

## Conventions

- No environment variable fallbacks — server is always in the path, auth via `svncli login`
- `--cookie` and `--browser` are on the `login` subcommand only, not global
- All HTTP calls have `timeout=self.timeout` (configurable via `--timeout`, default 60s)
- `--delete` in sync requires `--force` or interactive confirmation
- Manifests stored in `~/.svncli/manifests/` keyed by SHA-256 hash of local dir path
- Progress output: `[1/5] upload file.txt... ok`
- Never mention Claude, AI, or co-authored-by AI in git commit messages
