"""Profiled YAML configuration: profile resolution, merge layers, overrides.

Covers the mechanism contract (CLAUDE.md, Configuration): CLI ``--profile`` >
``APP_PROFILE`` env > ``default``; profile deep-merged over default;
``APP__SECTION__KEY`` env overrides (strings, branch-creating); the
``CLI_OVERRIDES`` registry on top; dot-notation access; graceful fallbacks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import iso_insight.config_loader as config_loader
from iso_insight.config_loader import Config

DEFAULT_YAML = """\
app:
  name: "Test App"
  env: "default"
workflow:
  max_iterations: 3
  low_confidence_threshold: 0.6
retrieval:
  top_k: 5
"""

DEV_YAML = """\
app:
  env: "dev"
workflow:
  max_iterations: 5
"""

QA_YAML = """\
app:
  env: "qa"
"""


@pytest.fixture(autouse=True)
def _clean_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient profile machinery (or the repo's .env) may leak into these tests."""
    monkeypatch.delenv("APP_PROFILE", raising=False)
    for key in [key for key in os.environ if key.startswith("APP__")]:
        monkeypatch.delenv(key, raising=False)
    # Config() calls load_dotenv(); in the test process it would pull the repo's
    # .env into os.environ and leak RAG_*/LLM_* values into every later test
    # (the loader tests use a tmp config/ and monkeypatched env, never the repo files).
    monkeypatch.setattr(config_loader, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "default.yaml").write_text(DEFAULT_YAML, encoding="utf-8")
    (tmp_path / "dev.yaml").write_text(DEV_YAML, encoding="utf-8")
    (tmp_path / "qa.yaml").write_text(QA_YAML, encoding="utf-8")
    return tmp_path


def _cfg(
    config_dir: Path,
    *,
    profile: str | None = None,
    argv: list[str] | None = None,
) -> Config:
    return Config(profile=profile, config_dir=config_dir, argv=argv if argv is not None else [])


# ── profile resolution ────────────────────────────────────────────────────


def test_default_profile_when_nothing_set(config_dir: Path) -> None:
    cfg = _cfg(config_dir)
    assert cfg.profile == "default"
    assert cfg.get("app.name") == "Test App"
    # YAML keeps native types: ints stay ints.
    assert cfg.get("workflow.max_iterations") == 3


def test_profile_from_env_var(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PROFILE", "dev")
    cfg = _cfg(config_dir)
    assert cfg.profile == "dev"
    assert cfg.get("app.env") == "dev"
    assert cfg.get("workflow.max_iterations") == 5
    # Untouched keys survive the deep merge.
    assert cfg.get("app.name") == "Test App"
    assert cfg.get("retrieval.top_k") == 5


def test_profile_from_cli_beats_env(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PROFILE", "dev")
    assert _cfg(config_dir, argv=["--profile", "qa"]).profile == "qa"
    assert _cfg(config_dir, argv=["--prof", "qa"]).profile == "qa"  # short alias


def test_explicit_profile_wins_over_everything(config_dir: Path) -> None:
    assert _cfg(config_dir, profile="dev").profile == "dev"


def test_missing_profile_file_falls_back_to_default(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_PROFILE", "does_not_exist")
    cfg = _cfg(config_dir)
    assert cfg.profile == "does_not_exist"
    assert cfg.get("app.name") == "Test App"  # base config only, no crash


# ── merge + overrides ─────────────────────────────────────────────────────


def test_env_override_nested_key(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP__WORKFLOW__MAX_ITERATIONS", "7")
    cfg = _cfg(config_dir, profile="dev")
    # ENV values arrive as strings — the documented behaviour of APP__* keys.
    assert cfg.get("workflow.max_iterations") == "7"
    # Sibling key from the profile is untouched.
    assert cfg.get("workflow.low_confidence_threshold") == 0.6


def test_env_override_creates_missing_branch(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP__EXPERIMENTS__E5__TOP_K", "10")
    cfg = _cfg(config_dir)
    assert cfg.get("experiments.e5.top_k") == "10"


def test_cli_override_registry(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_loader, "CLI_OVERRIDES", {"max_iter": "workflow.max_iterations"})
    cfg = _cfg(config_dir, argv=["--max-iter", "9"])
    assert cfg.get("workflow.max_iterations") == "9"


# ── access + fallbacks ────────────────────────────────────────────────────


def test_get_dot_notation_and_default(config_dir: Path) -> None:
    cfg = _cfg(config_dir)
    assert cfg.get("no.such.key") is None
    assert cfg.get("no.such.key", "dflt") == "dflt"
    assert cfg.get("app") == {"name": "Test App", "env": "default"}


def test_unparseable_yaml_falls_back_to_empty(tmp_path: Path) -> None:
    (tmp_path / "default.yaml").write_text("app: [unclosed", encoding="utf-8")
    cfg = _cfg(tmp_path)
    assert cfg.get("app.name") is None


def test_get_settings_is_a_lazy_singleton() -> None:
    first = config_loader.get_settings()
    assert config_loader.get_settings() is first


def test_repo_default_yaml_loads() -> None:
    """The checked-in config/ folder is itself valid (default profile, real dir)."""
    cfg = Config(profile="default")
    name = cfg.get("app.name")
    assert isinstance(name, str)
    assert name
