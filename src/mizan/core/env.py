"""Tiny .env support (standard library only). The project's .env file (git-ignored) holds secrets such as MIZAN_ADMIN_PASSWORD_HASH and
GEMINI_API_KEY. Values are taken literally: a '$' in a password hash is NOT expanded. Variables already set in the real environment win."""
from __future__ import annotations

import os
from pathlib import Path

from .io import atomic_write_text
from .paths import project_root


def parse_env_text(text: str) -> dict:
    out = {}
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def load_env(path: str | os.PathLike | None = None) -> list[str]:
    """Read the .env file into os.environ (never overriding a variable that is already set). Returns the names it set."""
    p = Path(path) if path else project_root() / ".env"
    try:
        values = parse_env_text(p.read_text(encoding="utf-8"))
    except OSError:
        return []
    set_now = []
    for k, v in values.items():
        if v != "" and k not in os.environ:
            os.environ[k] = v
            set_now.append(k)
    return set_now


def set_env_value(path: str | os.PathLike, key: str, value: str) -> Path:
    """Add or replace KEY=value in the .env file, keeping every other line as it is."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    new, done = [], False
    for line in lines:
        probe = line.strip()
        probe = probe[7:].lstrip() if probe.startswith("export ") else probe
        if not done and probe.split("=", 1)[0].strip() == key and "=" in probe and not probe.startswith("#"):
            new.append(f"{key}={value}")
            done = True
        else:
            new.append(line)
    if not done:
        new.append(f"{key}={value}")
    return atomic_write_text(p, "\n".join(new) + "\n")
