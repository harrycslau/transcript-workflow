"""Configuration loading for Brain.

Precedence (highest first):
  1. Environment variables: BRAIN_CONFIG (config file location) and
     the env var named by each section's ``api_key_env`` (secrets).
  2. The selected YAML configuration file (default ``config/config.yaml``).
  3. Code defaults, which supplement optional settings.

The selected YAML file itself is REQUIRED. Missing or malformed
configuration raises :class:`ConfigError` with a concise message.

Relative paths in the ``storage`` section are resolved against the
project root (the directory containing ``pyproject.toml``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_RELATIVE = Path("config") / "config.yaml"


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or invalid."""


def project_root() -> Path:
    """Return the project root (directory containing pyproject.toml).

    Falls back to the current working directory when no marker is found.
    """
    marker = "pyproject.toml"
    start = Path(__file__).resolve()
    for parent in start.parents:
        if (parent / marker).exists():
            return parent
    return Path.cwd()


def config_file_path() -> Path:
    """Resolve the configuration file path.

    ``BRAIN_CONFIG`` (absolute or relative to the current working
    directory) overrides the default ``<project root>/config/config.yaml``.
    """
    override = os.environ.get("BRAIN_CONFIG", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    return project_root() / DEFAULT_CONFIG_RELATIVE


def load_env_files(root: Path | None = None) -> None:
    """Load ``.env`` from the given root (default: project root).

    Real environment variables always win (``override=False``).
    """
    base = root if root is not None else project_root()
    load_dotenv(base / ".env", override=False)


# --------------------------------------------------------------------------
# Defaults: code-level supplements for optional settings. The YAML file is
# still required; these fill in sections/keys the user omitted.
# --------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "storage": {
        "inbox": "./data/inbox",
        "database": "./data/database/brain.sqlite3",
        "transcripts": "./data/transcripts",
        "exports": "./data/exports",
        "logs": "./data/logs",
        "temp": "./data/temp",
    },
    "macwhisper": {
        "command": "/usr/local/bin/mw",
        "model": None,
        "language": "auto",
        "speakers": True,
        "output_format": "json",
    },
    "llm": {
        "provider": "openai_compatible",
        "base_url": "http://localhost:8000/v1",
        "model": "",
        "api_key_env": "BRAIN_LLM_API_KEY",
        "temperature": 0.2,
        "timeout_seconds": 600,
    },
    "embedding": {
        "base_url": "http://localhost:8000/v1",
        "model": "",
        "api_key_env": "BRAIN_LLM_API_KEY",
    },
    "retention": {
        "enabled": False,
        "audio_days": 3,
        "delete_mode": "permanent",
        "require_transcript": True,
        "require_summary": True,
    },
    "initial_tags": [],
}


@dataclass(frozen=True)
class StorageConfig:
    inbox: Path
    database: Path
    transcripts: Path
    exports: Path
    logs: Path
    temp: Path


@dataclass(frozen=True)
class MacWhisperConfig:
    command: str
    model: str | None
    language: str
    speakers: bool
    output_format: str


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    temperature: float
    timeout_seconds: int


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key_env: str


@dataclass(frozen=True)
class RetentionConfig:
    enabled: bool
    audio_days: int
    delete_mode: str
    require_transcript: bool
    require_summary: bool


@dataclass(frozen=True)
class TagSpec:
    name: str
    description: str


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    storage: StorageConfig
    macwhisper: MacWhisperConfig
    llm: LLMConfig
    embedding: EmbeddingConfig
    retention: RetentionConfig
    initial_tags: list[TagSpec] = field(default_factory=list)

    def api_key_for(self, api_key_env: str) -> str | None:
        """Return the secret named by ``api_key_env``, or None.

        Secrets are read from the environment on demand and never stored,
        logged, or included in ``repr``.
        """
        value = os.environ.get(api_key_env, "").strip()
        return value or None


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

_MISSING = object()


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, _MISSING)
    if value is _MISSING:
        return dict(_DEFAULTS.get(name, {}))
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a mapping, got {type(value).__name__}")
    merged = dict(_DEFAULTS.get(name, {}))
    merged.update(value)
    return merged


def _get(section: dict[str, Any], name: str, path: str, expected: type | tuple[type, ...]) -> Any:
    if name not in section:
        raise ConfigError(f"[{path}]: missing required key '{name}'")
    value = section[name]
    if value is None or not isinstance(value, expected):
        want = expected.__name__ if isinstance(expected, type) else "/".join(t.__name__ for t in expected)
        got = "null" if value is None else type(value).__name__
        raise ConfigError(f"[{path}]: '{name}' must be {want}, got {got}")
    return value


def _get_nonblank(section: dict[str, Any], name: str, path: str) -> str:
    value = _get(section, name, path, str)
    if not value.strip():
        raise ConfigError(f"[{path}]: '{name}' must not be blank")
    return value


def _get_number(section: dict[str, Any], name: str, path: str, expected: type | tuple[type, ...], *, positive: bool = False) -> Any:
    """Numeric getter that rejects booleans (YAML true/false are ints in Python)."""
    if name not in section:
        raise ConfigError(f"[{path}]: missing required key '{name}'")
    value = section[name]
    if isinstance(value, bool) or value is None or not isinstance(value, expected):
        want = expected.__name__ if isinstance(expected, type) else "/".join(t.__name__ for t in expected)
        got = "bool" if isinstance(value, bool) else "null" if value is None else type(value).__name__
        raise ConfigError(f"[{path}]: '{name}' must be {want}, got {got}")
    if positive and value <= 0:
        raise ConfigError(f"[{path}]: '{name}' must be a positive integer, got {value}")
    return value


def _resolve(base: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base / value).resolve()


def _parse_storage(raw: dict[str, Any], base: Path) -> StorageConfig:
    section = _section(raw, "storage")
    fields: dict[str, Path] = {}
    for key in ("inbox", "database", "transcripts", "exports", "logs", "temp"):
        value = _get_nonblank(section, key, "storage")
        fields[key] = _resolve(base, Path(value).expanduser())
    return StorageConfig(**fields)


def _parse_macwhisper(raw: dict[str, Any]) -> MacWhisperConfig:
    s = _section(raw, "macwhisper")
    model = s.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigError(f"[macwhisper]: 'model' must be a string or null, got {type(model).__name__}")
    return MacWhisperConfig(
        command=_get_nonblank(s, "command", "macwhisper"),
        model=model,
        language=_get(s, "language", "macwhisper", str),
        speakers=_get(s, "speakers", "macwhisper", bool),
        output_format=_get(s, "output_format", "macwhisper", str),
    )


def _parse_llm(raw: dict[str, Any]) -> LLMConfig:
    s = _section(raw, "llm")
    return LLMConfig(
        provider=_get_nonblank(s, "provider", "llm"),
        base_url=_get_nonblank(s, "base_url", "llm"),
        model=_get(s, "model", "llm", str),
        api_key_env=_get_nonblank(s, "api_key_env", "llm"),
        temperature=_get_number(s, "temperature", "llm", (int, float)),
        timeout_seconds=_get_number(s, "timeout_seconds", "llm", int, positive=True),
    )


def _parse_embedding(raw: dict[str, Any]) -> EmbeddingConfig:
    s = _section(raw, "embedding")
    return EmbeddingConfig(
        base_url=_get_nonblank(s, "base_url", "embedding"),
        model=_get(s, "model", "embedding", str),
        api_key_env=_get_nonblank(s, "api_key_env", "embedding"),
    )


def _parse_retention(raw: dict[str, Any]) -> RetentionConfig:
    s = _section(raw, "retention")
    return RetentionConfig(
        enabled=_get(s, "enabled", "retention", bool),
        audio_days=_get_number(s, "audio_days", "retention", int, positive=True),
        delete_mode=_get(s, "delete_mode", "retention", str),
        require_transcript=_get(s, "require_transcript", "retention", bool),
        require_summary=_get(s, "require_summary", "retention", bool),
    )


def _parse_tags(raw: dict[str, Any]) -> list[TagSpec]:
    tags = raw.get("initial_tags", _DEFAULTS["initial_tags"])
    if not isinstance(tags, list):
        raise ConfigError(f"[initial_tags] must be a list, got {type(tags).__name__}")
    parsed: list[TagSpec] = []
    for index, item in enumerate(tags):
        if not isinstance(item, dict):
            raise ConfigError(f"[initial_tags] entry {index} must be a mapping")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[initial_tags] entry {index}: 'name' must be a non-empty string")
        if not isinstance(description, str):
            raise ConfigError(f"[initial_tags] entry {index}: 'description' must be a string")
        parsed.append(TagSpec(name=name, description=description))
    return parsed


def load_config(path: Path | None = None, *, env_file: Path | None = None) -> AppConfig:
    """Load, validate, and return the application configuration.

    ``path`` overrides the config file location (``BRAIN_CONFIG`` is
    honoured when ``path`` is None). ``env_file`` overrides the ``.env``
    location (default: ``<project root>/.env``; real environment
    variables always win). Raises :class:`ConfigError` with a concise,
    user-facing message for missing, malformed, or invalid input.
    """
    if env_file is not None:
        load_dotenv(env_file, override=False)
    else:
        load_env_files()
    selected = path if path is not None else config_file_path()
    try:
        text = selected.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"Configuration file not found: {selected}\n"
            "Copy config/config.example.yaml to config/config.yaml to get started."
        ) from None
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {selected}: {exc}") from None

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {selected}: {exc}") from None

    if raw is None:
        raise ConfigError(f"Configuration file {selected} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration file {selected} must contain a YAML mapping")

    # Relative storage paths resolve against the project root, regardless
    # of where the config file itself lives.
    base = project_root()
    return AppConfig(
        config_path=selected,
        storage=_parse_storage(raw, base),
        macwhisper=_parse_macwhisper(raw),
        llm=_parse_llm(raw),
        embedding=_parse_embedding(raw),
        retention=_parse_retention(raw),
        initial_tags=_parse_tags(raw),
    )
