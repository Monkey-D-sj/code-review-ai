"""Focused regression contracts used by the code-review evaluation cases."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.api.v1.module_storage.core import encrypt as encrypt_module
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.api.v1.module_storage.core.encrypt import decrypt_password, encrypt_password
from app.api.v1.module_storage.file.service import StorageFileService
from app.api.v1.module_storage.source.service import StorageSourceService
from app.api.v1.module_storage.transfer import engine as transfer_engine
from app.api.v1.module_storage.transfer.engine import (
    _build_config,
    _dt,
    _run_step,
    execute_transfer_task,
)
from app.api.v1.module_task.workflow.flows.handlers.builtin_nodes import (
    get_builtin_node,
)
from app.core.exceptions import CustomException
from app.utils.common_util import search_to_dict


class _Search(BaseModel):
    created_time: list[str] | None = None
    name: str | None = None


def _source(password: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        protocol=StorageProtocol.LOCAL.value,
        host="",
        port=0,
        status=0,  # 启用
        username=None,
        password=password,
        bucket=None,
        endpoint=None,
        region=None,
        path_prefix=None,
        is_secure=False,
        implicit_tls=False,
    )


def test_search_to_dict_builds_time_range() -> None:
    search = _Search(created_time=["2026-01-01", "2026-01-31"])

    assert search_to_dict(search) == {
        "created_time": ("between", ["2026-01-01", "2026-01-31"])
    }


def test_search_to_dict_excludes_none_fields() -> None:
    assert search_to_dict(_Search(name=None)) == {}


def test_storage_password_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(encrypt_module, "_fernet", None)

    cipher = encrypt_password("secret")

    assert cipher != "secret"
    assert decrypt_password(cipher) == "secret"


def test_invalid_storage_password_is_not_silenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encrypt_module, "_fernet", None)

    with pytest.raises(CustomException):
        decrypt_password("not-a-fernet-token")


@pytest.mark.asyncio
async def test_transfer_config_decrypts_password() -> None:
    source = _source(encrypt_password("secret"))

    class _DB:
        async def get(self, _model, _source_id):
            return source

    config = await _build_config(_DB(), 7)

    assert config is not None
    assert config.password == "secret"


@pytest.mark.asyncio
async def test_storage_file_lookup_forwards_source_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []

    async def fake_get_active_source(_self, source_id):
        seen.append(source_id)
        return _source()

    monkeypatch.setattr(
        StorageSourceService, "get_active_source", fake_get_active_source
    )
    await StorageFileService(SimpleNamespace(), SimpleNamespace())._get_source(23)

    assert seen == [23]


@pytest.mark.asyncio
async def test_transfer_task_stops_after_first_failed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        id=11,
        status="pending",
        source_type="local",
        source_path=None,
        source_id=None,
        source_size=0,
        total_size=0,
        transferred_size=0,
        progress=0,
        error_msg=None,
        started_at=None,
        finished_at=None,
    )
    steps = [
        SimpleNamespace(status="pending", finished_at=None),
        SimpleNamespace(status="pending", finished_at=None),
    ]

    class _DB:
        async def get(self, _model, _task_id):
            return task

        async def commit(self):
            return None

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    calls: list[object] = []

    async def fake_load_steps(_db, _task_id):
        return steps

    async def fake_run_step(_db, _task, step):
        calls.append(step)
        return False

    async def fake_broadcast(_task, _steps):
        return None

    monkeypatch.setattr(transfer_engine, "async_db_session", lambda: _Session())
    monkeypatch.setattr(transfer_engine, "_load_steps", fake_load_steps)
    monkeypatch.setattr(transfer_engine, "_run_step", fake_run_step)
    monkeypatch.setattr(transfer_engine, "_broadcast", fake_broadcast)
    monkeypatch.setattr(
        transfer_engine.transfer_task_registry, "is_canceled", lambda _id: False
    )
    monkeypatch.setattr(
        transfer_engine.transfer_task_registry, "clear", lambda _id: None
    )

    await execute_transfer_task(task.id)

    assert calls == [steps[0]]


# ── transfer/engine payload serialization ──────────────────────────────


def test_dt_serializes_aware_datetime_to_iso() -> None:
    value = datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)

    assert _dt(value) == "2026-01-31T23:59:59+00:00"


def test_dt_none_returns_none() -> None:
    assert _dt(None) is None


@pytest.mark.asyncio
async def test_running_step_reports_intermediate_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    step = SimpleNamespace(
        id=1,
        step_order=1,
        source_id=None,
        source_path=str(tmp_path / "definitely-missing.txt"),
        target_id=None,
        target_path="out/a.txt",
        status="pending",
        progress=0,
        speed=None,
        total_size=None,
        transferred_size=None,
        error_msg=None,
        started_at=None,
        finished_at=None,
    )
    task = SimpleNamespace(id=1, status="pending", error_msg=None, finished_at=None)
    seen: dict[str, int] = {}

    class _DB:
        async def commit(self) -> None:
            return None

    async def fake_load_steps(_db, _task_id):
        return [step]

    async def fake_broadcast(_task, _steps):
        # 只记录第一次广播：执行中状态下的进度
        seen.setdefault("progress", step.progress)

    monkeypatch.setattr(transfer_engine, "_load_steps", fake_load_steps)
    monkeypatch.setattr(transfer_engine, "_broadcast", fake_broadcast)

    await _run_step(_DB(), task, step)

    assert seen["progress"] == 50


# ── storage remote path validation ─────────────────────────────────────


def test_remote_path_rejects_traversal() -> None:
    with pytest.raises(CustomException):
        StorageFileService(
            SimpleNamespace(), SimpleNamespace()
        )._validate_remote_path("inbox/../secret.txt")
    with pytest.raises(CustomException):
        StorageFileService(
            SimpleNamespace(), SimpleNamespace()
        )._validate_remote_path("../etc/passwd")

    assert (
        StorageFileService(SimpleNamespace(), SimpleNamespace())
        ._validate_remote_path("inbox/a.txt")
        == "inbox/a.txt"
    )


# ── workflow builtin node registry ─────────────────────────────────────


def test_builtin_node_storage_url_is_registered() -> None:
    node = get_builtin_node("storage_url")

    assert node is not None
    assert node.code == "storage_url"


# ── 跨文件契约：source.path_prefix → 配置构造 → 适配器 key 拼接 ──────


@pytest.mark.asyncio
async def test_storage_config_keeps_source_path_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储源配置必须保留 path_prefix，适配器拼接后 key 带前缀。"""
    source = _source()
    source.path_prefix = "uploads"

    async def fake_get_active_source(_self, source_id):
        return source

    monkeypatch.setattr(
        StorageSourceService, "get_active_source", fake_get_active_source
    )
    config = await StorageFileService(SimpleNamespace(), SimpleNamespace())._get_source(23)

    assert config.path_prefix == "uploads"
