"""Locate the project root so the code works from PyCharm, the terminal, tests and GitHub Actions alike."""
import os
from pathlib import Path


def project_root() -> Path:
    """MIZAN_HOME env var wins; otherwise walk up from this file to the folder containing pyproject.toml."""
    env = os.environ.get("MIZAN_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Cannot locate project root (no pyproject.toml found). Set MIZAN_HOME.")


def resolve(relative: str | os.PathLike) -> Path:
    """Resolve a config-style relative path against the project root (absolute paths pass through)."""
    p = Path(relative)
    return p if p.is_absolute() else project_root() / p
