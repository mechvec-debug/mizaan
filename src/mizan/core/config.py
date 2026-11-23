"""Loads config/app.yaml (plus optional config/app.local.yaml override, never committed)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .env import load_env
from .errors import ConfigError
from .paths import project_root


def _read(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at top level")
    return data


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(config_dir: Path | None = None) -> dict[str, Any]:
    load_env()                                   # secrets from the project's .env (never overrides real environment variables)
    cdir = Path(config_dir) if config_dir else project_root() / "config"
    cfg = _read(cdir / "app.yaml")
    local = cdir / "app.local.yaml"
    if local.exists():
        cfg = _merge(cfg, _read(local))
    return cfg
