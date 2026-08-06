
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = dict(
    repo_path=".",
    db_path=".code-review-ai/index.db",
    diff_base="origin/main",
    watch_debounce_ms=500,
    entry_names=["main"],
    entry_decorators=[
        "app.route", "click.command",
        "router.get", "router.post", "celery.task",
    ],
    exclude=["*/migrations/*", "dist/*", "static/*", ".venv/*", ".claude/*", "assets/*", "node_modules/*"],
    test_globs=["*/tests/*", "test_*.py"],  # not */test* — would tag prod files whose name starts with "test" (e.g. testimpact.py)
    test_names=["test_*"],
    community_detection=False,
    community_weight="plain",
    external_service_url="http://localhost:3000",
)


@dataclass
class Config:
    repo_path: str
    db_path: str
    diff_base: str
    watch_debounce_ms: int
    entry_names: list[str]
    entry_decorators: list[str]
    exclude: list[str]
    test_globs: list[str]
    test_names: list[str]
    community_detection: bool
    community_weight: str
    external_service_url: str


def _load_toml(repo_path: str) -> dict:
    for name in ("pyproject.toml", "cr-ai.toml"):
        p = Path(repo_path) / name
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            return data.get("tool", {}).get("code-review-ai", {})
    return {}


def load_config(repo_path: str = ".") -> Config:
    raw = dict(DEFAULTS)
    raw.update({k: v for k, v in _load_toml(repo_path).items() if v is not None})
    # env overrides (CRAI_<UPPER_KEY>)
    for key in DEFAULTS:
        env = os.environ.get(f"CRAI_{key.upper()}")
        if env is not None:
            if isinstance(DEFAULTS[key], bool):
                raw[key] = env.lower() in ("1", "true", "yes", "on")
            elif isinstance(DEFAULTS[key], int):
                raw[key] = int(env)
            elif isinstance(DEFAULTS[key], list):
                raw[key] = env.split(",")
            else:
                raw[key] = env
    return Config(**raw)


_CONFIG_HASH_KEYS = ("diff_base", "entry_names", "entry_decorators", "exclude",
                     "test_globs", "test_names",
                     "community_detection", "community_weight")


def config_hash(config: Config) -> str:
    """Stable hash of the config keys that affect index shape. On change the
    incremental paths fall back to a full rebuild."""
    payload = {key: getattr(config, key) for key in _CONFIG_HASH_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
