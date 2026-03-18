import contextlib
import subprocess
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """Auto-install pre-commit hooks on first test run."""
    hook = _repo_root / ".git" / "hooks" / "pre-commit"
    if hook.exists():
        return
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["pre-commit", "install"],
            cwd=_repo_root,
            capture_output=True,
        )
