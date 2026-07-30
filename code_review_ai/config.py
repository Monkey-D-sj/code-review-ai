
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
    exclude=["*/test*", "*/migrations/*", "dist/*", "static/*", ".venv/*", ".claude/*", "assets/*", "node_modules/*"],
    community_detection=False,
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
    community_detection: bool
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
