
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
    dependency_markers=["Depends"],  # DI calls whose callable args become source->arg edges
    di_annotations=["Autowired", "Inject", "Resource", "MockBean"],  # annotation names tagging a Java field as an injection point
    exclude=["*/migrations/*", "dist/*", "static/*", ".venv/*", ".claude/*", "assets/*", "node_modules/*"],
    test_globs=["*/tests/*", "test_*.py", "*_test.py", "*/test/*", "*Test.java", "*Tests.java", "*.test.*", "*.spec.*", "*/__tests__/*"],  # per-language defaults; not */test* — would tag prod files whose name starts with "test" (e.g. testimpact.py)
    test_names=["test_*"],
    test_decorators=["Test", "ParameterizedTest"],  # decorator/annotation names tagging a node as a test (e.g. JUnit 5 @Test), matched like entry_names
    community_detection=False,
    community_weight="plain",
    path_aliases={},  # import specifier prefix -> repo-relative dir, e.g. {"@/": "src/"}
    external_service_url="http://localhost:3000",
    summary_source="diff",  # "none"|"diff" — attach each changed function's unified diff to the change summary
)


@dataclass
class Config:
    repo_path: str
    db_path: str
    diff_base: str
    watch_debounce_ms: int
    entry_names: list[str]
    entry_decorators: list[str]
    dependency_markers: list[str]
    di_annotations: list[str]
    exclude: list[str]
    test_globs: list[str]
    test_names: list[str]
    test_decorators: list[str]
    community_detection: bool
    community_weight: str
    path_aliases: dict[str, str]
    external_service_url: str
    summary_source: str


def _load_toml(repo_path: str) -> dict:
    for name in ("pyproject.toml", "cr-ai.toml"):
        p = Path(repo_path) / name
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            return data.get("tool", {}).get("code-review-ai", {})
    return {}


def _jsonc_clean(text: str) -> str:
    """Strip // and /* */ comments and trailing commas from a JSONC document
    (what tsconfig.json actually is), leaving string literals untouched.

    Comments are replaced with spaces so line numbers survive for later parse
    errors; a trailing comma before } or ] is dropped. Strict json.loads fails
    on real-world tsconfig files, which are JSONC, not JSON.
    """
    def _no_comments(src: str) -> str:
        out: list[str] = []
        i, n = 0, len(src)
        in_string = False
        while i < n:
            c, nxt = src[i], src[i + 1] if i + 1 < n else ""
            if in_string:
                out.append(c)
                if c == "\\" and nxt:
                    out.append(nxt)
                    i += 2
                    continue
                if c == '"':
                    in_string = False
                i += 1
                continue
            if c == '"':
                in_string = True
                out.append(c)
                i += 1
                continue
            if c == "/" and nxt == "/":
                while i < n and src[i] != "\n":
                    out.append(" ")
                    i += 1
                continue
            if c == "/" and nxt == "*":
                out.append(" ")
                i += 2
                while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                    out.append(" " if src[i] != "\n" else "\n")
                    i += 1
                i += 2
                out.append(" ")
                continue
            out.append(c)
            i += 1
        return "".join(out)

    cleaned = _no_comments(text)
    out: list[str] = []
    i, n = 0, len(cleaned)
    in_string = False
    while i < n:
        c = cleaned[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(cleaned[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and cleaned[j] in " \t\n\r":
                j += 1
            if j < n and cleaned[j] in "}]":
                i += 1  # trailing comma — drop
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _tsconfig_path_aliases(repo_path: str) -> dict:
    """Path aliases from <repo>/tsconfig.json compilerOptions.paths.

    Each `@/* -> src/*` entry becomes a prefix -> dir alias ({"@/": "src/"}),
    so import specifiers like `@/hooks/useSelectOptions` can be resolved to the
    module qname the graph derives from `src/hooks/useSelectOptions.ts`.
    Missing/unreadable tsconfig (e.g. pure Python repos) yields {}.
    """
    ts = Path(repo_path) / "tsconfig.json"
    if not ts.exists():
        return {}
    try:
        data = json.loads(_jsonc_clean(ts.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return {}
    paths = (data.get("compilerOptions") or {}).get("paths") or {}
    out: dict = {}
    for key, targets in paths.items():
        if not targets:
            continue
        prefix = key.rstrip("*")          # "@/*" -> "@/"
        target = targets[0].rstrip("*").lstrip("./")  # "src/*" -> "src/"
        if not prefix or not target:
            continue
        out[prefix] = target
    return out


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
            elif isinstance(DEFAULTS[key], dict):
                raw[key] = json.loads(env)
            else:
                raw[key] = env
    # Toolchain auto-detection: tsconfig.json paths, explicit config wins.
    raw["path_aliases"] = {**_tsconfig_path_aliases(raw["repo_path"]),
                           **(raw["path_aliases"] or {})}
    return Config(**raw)


_CONFIG_HASH_KEYS = ("diff_base", "entry_names", "entry_decorators",
                     "dependency_markers", "di_annotations", "exclude",
                     "test_globs", "test_names", "test_decorators",
                     "community_detection", "community_weight", "path_aliases")


def config_hash(config: Config) -> str:
    """Stable hash of the config keys that affect index shape. On change the
    incremental paths fall back to a full rebuild."""
    payload = {key: getattr(config, key) for key in _CONFIG_HASH_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
