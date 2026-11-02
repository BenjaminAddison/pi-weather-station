"""Shared helpers for the W3 system-integration tests (units, scripts, firewall, install.sh)."""

import os
import shutil
import subprocess
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = PI_DIR / "systemd"


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """Parse a systemd unit/drop-in: {section: {key: [values in order]}}.

    Repeated keys accumulate (as systemd does for ExecStop=, Environment=, ...).
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{path.name}: key outside a section: {raw!r}"
        assert "=" in line, f"{path.name}: not a key=value line: {raw!r}"
        key, value = line.split("=", 1)
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def find_bash() -> str | None:
    """A POSIX bash: Git Bash on Windows (never WSL's System32\\bash.exe), else PATH."""
    if os.name == "nt":
        candidates = []
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if os.environ.get(env):
                candidates.append(Path(os.environ[env]) / "Git" / "bin" / "bash.exe")
        for c in candidates:
            if c.is_file():
                return str(c)
        return None
    return shutil.which("bash")


def run_bash(bash: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [bash, *args], check=False, capture_output=True, text=True, env=full_env, timeout=120
    )
