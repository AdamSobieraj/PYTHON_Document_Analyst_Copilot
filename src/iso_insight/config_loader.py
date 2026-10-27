"""Profiled YAML configuration: ``default.yaml`` + ``<profile>.yaml`` + ENV + CLI.

The configuration mechanism (replicable per CLAUDE.md — see the "Profiled
YAML configuration" section there):

- ``config/default.yaml`` holds every key; a profile file
  (``config/<profile>.yaml``) overrides only what differs and is **deep-merged**
  over the default.
- Profile resolution: CLI ``--profile``/``--prof`` > ``APP_PROFILE`` env var
  > ``default``.
- ``APP__SECTION__KEY`` env vars override individual keys (double underscore =
  nesting level, matched case-insensitively) — the Docker/K8s steering knob;
  values arrive as raw strings.
- Run-level CLI flags via the ``CLI_OVERRIDES`` registry sit on top of
  everything.

Precedence, low to high: ``default.yaml`` < ``<profile>.yaml`` < ``APP__*`` env
< CLI flags.

This layer carries non-secret, run-level configuration (scripts, ``eval/``,
experiments). Process settings — endpoints, credentials, backend selection —
stay in :mod:`iso_insight.config` (pydantic-settings, env-only). Secrets never
go into YAML.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("iso_insight.config_loader")

ENV_PREFIX = "APP__"
PROFILE_ENV_VAR = "APP_PROFILE"
DEFAULT_PROFILE = "default"

#: Run-level CLI overrides: flag name (underscore form) -> dotted config key.
#: Highest-priority layer; kept deliberately small. Extend per project, e.g.
#: ``CLI_OVERRIDES = {"collection_name": "vector_db.collection_name"}``.
CLI_OVERRIDES: dict[str, str] = {}


def _repo_config_dir() -> Path:
    """The checked-in ``config/`` folder at the project root (src layout)."""
    return Path(__file__).resolve().parents[2] / "config"


def _merge_dicts(base: dict[str, Any], override: Mapping[str, Any]) -> None:
    """Recursively merge ``override`` into ``base`` in place (override wins)."""
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = value


def _set_dotted(data: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    """Set ``data[k1][k2]... = value``, creating missing branches."""
    if not keys:
        return
    target = data
    for key in keys[:-1]:
        child = target.get(key)
        if not isinstance(child, dict):
            child = {}
            target[key] = child
        target = child
    target[keys[-1]] = value


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)  # never steal -h from the app
    parser.add_argument("--prof", "--profile", dest="profile", type=str, default=None)
    for flag in CLI_OVERRIDES:
        parser.add_argument(f"--{flag.replace('_', '-')}", dest=flag, type=str, default=None)
    return parser


def _known_args(argv: Sequence[str] | None) -> argparse.Namespace:
    args, _ = _build_arg_parser().parse_known_args(argv if argv is not None else sys.argv[1:])
    return args


class Config:
    """Profiled configuration: deep-merged YAML layers + ENV/CLI overrides.

    Construct directly in tests (``config_dir=`` points at a tmp folder);
    use :func:`get_settings` for the process-wide instance.
    """

    def __init__(
        self,
        profile: str | None = None,
        config_dir: str | os.PathLike[str] | None = None,
        *,
        argv: Sequence[str] | None = None,
    ) -> None:
        load_dotenv()  # let .env feed APP__* overrides; real env vars still win
        self._config_dir = Path(config_dir) if config_dir is not None else _repo_config_dir()
        self.profile = profile if profile is not None else self._determine_profile(argv)
        self._data: dict[str, Any] = {}
        self._load_yaml_layers()
        self._apply_env_overrides()
        self._apply_cli_overrides(argv)
        logger.info("Profiled config loaded (profile=%s, dir=%s)", self.profile, self._config_dir)

    @property
    def data(self) -> Mapping[str, Any]:
        """The fully merged configuration (live dict — treat as read-only)."""
        return self._data

    def get(self, key_path: str, default: Any = None) -> Any:
        """Dot-notation lookup, e.g. ``config.get("workflow.max_iterations")``."""
        value: Any = self._data
        for key in key_path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def _determine_profile(self, argv: Sequence[str] | None) -> str:
        cli_profile: str | None = _known_args(argv).profile
        if cli_profile:
            logger.info("Profile from CLI: %s", cli_profile)
            return cli_profile
        env_profile = os.getenv(PROFILE_ENV_VAR)
        if env_profile:
            logger.info("Profile from environment: %s", env_profile)
            return env_profile
        return DEFAULT_PROFILE

    def _load_yaml_layers(self) -> None:
        self._data = self._load_yaml(self._config_dir / f"{DEFAULT_PROFILE}.yaml")
        if self.profile != DEFAULT_PROFILE:
            profile_path = self._config_dir / f"{self.profile}.yaml"
            _merge_dicts(self._data, self._load_yaml(profile_path))

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            logger.warning("Config file missing: %s", path)
            return {}
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.error("Cannot parse YAML %s: %s", path, exc)
            return {}
        if not isinstance(loaded, dict):
            logger.error("Top level of %s is not a mapping; ignoring it", path)
            return {}
        return loaded

    def _apply_env_overrides(self) -> None:
        for env_key, env_val in os.environ.items():
            if not env_key.startswith(ENV_PREFIX):
                continue
            keys = env_key[len(ENV_PREFIX) :].lower().split("__")
            _set_dotted(self._data, keys, env_val)
            logger.debug("Overridden from %s: %s", env_key, " > ".join(keys))

    def _apply_cli_overrides(self, argv: Sequence[str] | None) -> None:
        args = _known_args(argv)
        for flag, dotted_key in CLI_OVERRIDES.items():
            value: Any = getattr(args, flag)
            if value:
                logger.info("CLI override --%s -> %s", flag, dotted_key)
                _set_dotted(self._data, dotted_key.split("."), str(value))


_instance: Config | None = None


def get_settings() -> Config:
    """Process-wide profiled config: lazy singleton, the first caller wins."""
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance
