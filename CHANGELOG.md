# Changelog

## 1.0.0 (2026-03-17)

Initial release.

### Features
- `svncli ls` — list remote directories (with `-r` for recursive)
- `svncli cp` — upload/download files (with `-r` for directories)
- `svncli sync` — bidirectional sync with revision-based change tracking
- `svncli rm` — delete remote files/folders
- `svncli mb` — create remote directories
- `svncli login` — authenticate via browser cookie extraction or interactive login (`-i`)
- `svncli logout` — remove saved session cookies
- `--dry-run` support on all mutating commands
- `--exclude` glob patterns for sync
- `--delete` flag for sync (with `--force` to skip confirmation)
- Auto cookie extraction from Chrome/Firefox/Edge/Safari
- Interactive browser login via Playwright
- Cookie persistence in `~/.svncli/cookies.json`
- Revision + SHA-256 manifest tracking (`.svncli.json`) for efficient incremental sync
- SSL verification bypass for corporate CAs (`--no-verify-ssl`)
- Configurable HTTP timeout (`--timeout`)
