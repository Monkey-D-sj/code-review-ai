"""Application configuration loader.

Loads and validates runtime configuration from raw dictionary payloads
(typically read from env files, a secrets store, or a control-plane API).
``parse_config`` is the single entry point used across the service; every
other module obtains its settings through it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 3
SUPPORTED_REGIONS = ("us-east-1", "eu-west-1", "ap-south-1")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DSN_RE = re.compile(r"^(postgres|redis|kafka|http)://")


# --------------------------------------------------------------------------- #
# scalar coercion helpers
# --------------------------------------------------------------------------- #

def _to_int(raw: dict[str, Any], key: str, default: int | None = None) -> int | None:
    """Coerce ``raw[key]`` to an int, or ``default`` when absent/unparsable."""
    value = raw.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(raw: dict[str, Any], key: str, default: float = 0.0) -> float:
    """Coerce ``raw[key]`` to a float, or ``default`` when absent/unparsable."""
    value = raw.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(raw: dict[str, Any], key: str, default: bool = False) -> bool:
    """Coerce ``raw[key]`` to a bool, honoring string forms like ``"yes"``."""
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_str(raw: dict[str, Any], key: str, default: str = "") -> str:
    """Coerce ``raw[key]`` to a string, or ``default`` when absent."""
    value = raw.get(key)
    if value is None:
        return default
    return str(value)


def _to_str_list(raw: dict[str, Any], key: str, default: Iterable[str] = ()) -> list[str]:
    """Coerce ``raw[key]`` to a list of strings (comma-split or already a list)."""
    value = raw.get(key)
    if value is None:
        return list(default)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [part.strip() for part in str(value).split(",") if part.strip()]


# --------------------------------------------------------------------------- #
# validators
# --------------------------------------------------------------------------- #

def _validate_name(name: str) -> str:
    """Reject service names that are empty, too long, or contain bad chars."""
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid app name: {name!r}")
    return name


def _validate_region(region: str) -> str:
    """Reject regions outside the supported deployment footprint."""
    if region not in SUPPORTED_REGIONS:
        raise ValueError(f"unsupported region: {region!r}")
    return region


def _validate_dsn(dsn: str, label: str = "dsn") -> str:
    """Reject connection strings whose scheme is not in the allowlist."""
    if not _DSN_RE.match(dsn or ""):
        raise ValueError(f"invalid {label}: {dsn!r}")
    return dsn


# --------------------------------------------------------------------------- #
# derived fields and small helpers
# --------------------------------------------------------------------------- #

def _mask_secret(value: str) -> str:
    """Redact a secret for logs, keeping only the first/last two chars."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _checksum(text: str) -> str:
    """A cheap, stable content fingerprint for cache invalidation."""
    return f"{len(text):08x}-{hash(text) & 0xffffffff:08x}"


def _overlay_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge ``CRAI_*`` environment overrides into a config payload."""
    merged = dict(raw)
    for key in list(raw):
        env_name = "CRAI_" + key.upper()
        if env_name in os.environ:
            merged[key] = os.environ[env_name]
    return merged


def _coerce_port(port: Any, default: int = 8080) -> int:
    """Coerce a port number, raising when it falls outside the valid range."""
    parsed = _to_int({"port": port}, "port", default)
    if parsed is None or not (1 <= parsed <= 65535):
        raise ValueError(f"port out of range: {parsed!r}")
    return parsed


def _derive_cache_ttl(seconds: int | None) -> int:
    """Clamp a cache TTL to a sane [1, 3600] second range."""
    if seconds is None:
        return 60
    return min(max(seconds, 1), 3600)


def _derive_retry_backoff(max_retries: int, base_ms: int) -> float:
    """Total worst-case retry window in seconds for an exponential backoff."""
    return base_ms * ((1 << max_retries) - 1) / 1000.0


def _capacity_for(nodes: int, slots_per_node: int) -> int:
    """Safe total slot capacity given a cluster of ``nodes`` workers."""
    return max(nodes, 1) * max(slots_per_node, 0)


def _rate_limit_interval(rps: float) -> float:
    """Milliseconds to wait between calls to honor a requests-per-second cap."""
    if rps <= 0:
        raise ValueError(f"rps must be positive, got {rps!r}")
    return 1000.0 / rps


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * q), len(sorted_values) - 1)
    return sorted_values[max(index, 0)]


def _parse_dsn_parts(dsn: str) -> dict[str, str]:
    """Split a validated DSN into scheme / host / port / database components."""
    scheme, rest = dsn.split("://", 1)
    host_port, _, database = rest.partition("/")
    host, _, port = host_port.rpartition(":")
    return {
        "scheme": scheme,
        "host": host or host_port,
        "port": port or "",
        "database": database or "",
    }


def _is_healthy(value: Any) -> bool:
    """Boolean health-check helper reused by probes across modules."""
    return bool(value) and value != "degraded"


def _join_quoted(items: Iterable[str]) -> str:
    """Render a list of strings as a comma-joined, quoted enumeration."""
    return ", ".join(f'"{item}"' for item in items)


def _bounded(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into the inclusive ``[low, high]`` band."""
    return min(max(value, low), high)


def _shorten(text: str, limit: int = 64) -> str:
    """Truncate ``text`` with an ellipsis when it exceeds ``limit`` chars."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _looks_like_json(text: str) -> bool:
    """Heuristic: a payload is JSON when it starts with a brace or bracket."""
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _parse_json_or_none(text: str) -> Any:
    """Parse ``text`` as JSON, returning None on malformed input."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overrides`` into ``base``, recursing into nested dicts."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# --------------------------------------------------------------------------- #
# the config object
# --------------------------------------------------------------------------- #

@dataclass
class AppConfig:
    """Validated, immutable runtime configuration for one service instance."""

    name: str
    region: str
    dsn: str
    # ``None`` means the caller did not provide a timeout. Public config
    # surfaces normalize that state to DEFAULT_TIMEOUT_SECONDS; consumers that
    # use the raw field must provide the same fallback explicitly.
    timeout: int | None
    max_retries: int
    cache_ttl: int
    read_only: bool = False
    tags: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def timeout_seconds(self) -> int:
        """Wall-clock timeout in whole seconds (see also ``timeout``)."""
        return (self.timeout if self.timeout is not None
                else DEFAULT_TIMEOUT_SECONDS)

    @property
    def retry_window(self) -> float:
        """Worst-case retry window implied by the backoff policy."""
        return _derive_retry_backoff(self.max_retries, 250)

    def with_overrides(self, **overrides: Any) -> "AppConfig":
        """Return a copy of this config with selected fields replaced."""
        return replace(self, **overrides)

    def to_json(self) -> str:
        """Serializable snapshot for observability, secrets masked."""
        payload = {
            "name": self.name,
            "region": self.region,
            "dsn": _mask_secret(self.dsn),
            "timeout": self.timeout_seconds,
            "max_retries": self.max_retries,
            "cache_ttl": self.cache_ttl,
            "read_only": self.read_only,
            "tags": list(self.tags),
        }
        return json.dumps(payload, sort_keys=True)


def parse_config(raw: dict[str, Any]) -> AppConfig:
    """Build an ``AppConfig`` from a raw payload, applying defaults and checks.

    Raises ``ValueError`` on invalid input; never returns a partial config.
    """
    payload = _overlay_env_overrides(raw)
    config = AppConfig(
        name=_validate_name(_to_str(payload, "name", "unnamed")),
        region=_validate_region(_to_str(payload, "region", "us-east-1")),
        dsn=_validate_dsn(_to_str(payload, "dsn"), "dsn"),
        timeout=_to_int(payload, "timeout", default=30),
        max_retries=_to_int(payload, "max_retries", DEFAULT_MAX_RETRIES),
        cache_ttl=_derive_cache_ttl(_to_int(payload, "cache_ttl", 60)),
        read_only=_to_bool(payload, "read_only"),
        tags=tuple(_to_str_list(payload, "tags")),
    )
    return config


def load_config_from_file(path: str | os.PathLike[str]) -> AppConfig:
    """Read a config file (JSON) and parse it into an ``AppConfig``."""
    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"config file must hold a JSON object: {config_path}")
    return parse_config(payload)
