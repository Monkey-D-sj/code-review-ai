"""Focused regression contracts for the system-module eval cases."""

from types import SimpleNamespace

import pytest

from app.api.v1.module_system.dict.crud import DictTypeCRUD
from app.api.v1.module_system.dict.service import DictTypeService
from app.api.v1.module_system.log.crud import LoginLogCRUD
from app.api.v1.module_system.log.schema import LoginLogQueryParam
from app.api.v1.module_system.log.service import LoginLogService
from app.api.v1.module_system.notice.crud import NoticeCRUD
from app.api.v1.module_system.notice.service import NoticeService
from app.api.v1.module_system.position.crud import PositionCRUD
from app.api.v1.module_system.position.service import PositionService
from app.core.exceptions import CustomException
from app.utils.common_util import get_parent_id_map, traversal_to_tree


# ── common_util hierarchy helpers ──────────────────────────────────────


def test_parent_id_map_maps_id_to_parent() -> None:
    depts = [
        SimpleNamespace(id=1, parent_id=None),
        SimpleNamespace(id=2, parent_id=1),
        SimpleNamespace(id=3, parent_id=2),
    ]

    assert get_parent_id_map(depts) == {1: None, 2: 1, 3: 2}


def test_traversal_tree_keeps_orphan_as_root() -> None:
    nodes = [
        {"id": 1, "parent_id": None, "name": "root"},
        {"id": 2, "parent_id": 99, "name": "orphan"},
    ]

    tree = traversal_to_tree(nodes)

    assert {n["id"] for n in tree} == {1, 2}


# ── position paging ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_position_page_second_page_offsets_by_one_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_page(self, *, offset, limit, order_by, search, out_schema, **kwargs):
        seen["offset"] = offset
        seen["limit"] = limit
        return None

    monkeypatch.setattr(PositionCRUD, "page", fake_page)

    await PositionService(SimpleNamespace(), SimpleNamespace()).page(
        page_no=2, page_size=2
    )

    assert seen == {"offset": 2, "limit": 2}


# ── dict type default ordering ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dict_type_page_defaults_to_ascending_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_page(self, *, offset, limit, order_by, search, out_schema, **kwargs):
        seen["order_by"] = order_by
        return None

    monkeypatch.setattr(DictTypeCRUD, "page", fake_page)

    await DictTypeService(SimpleNamespace(), SimpleNamespace()).page(
        page_no=1, page_size=10
    )

    assert seen["order_by"] == [{"id": "asc"}]


# ── login log search forwarding ────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_log_page_forwards_search_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_page(self, *, offset, limit, order_by, search, out_schema, **kwargs):
        seen["search"] = search
        return None

    monkeypatch.setattr(LoginLogCRUD, "page", fake_page)

    await LoginLogService(SimpleNamespace(), SimpleNamespace()).page(
        page_no=1, page_size=10, search=LoginLogQueryParam(username="admin")
    )

    assert seen["search"] == {"username": ("like", "admin")}


# ── notice status / uniqueness / batch delete ──────────────────────────


@pytest.mark.asyncio
async def test_notice_set_available_forwards_requested_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_set(self, *, ids, include_deleted=False, **kwargs):
        seen["ids"] = ids
        seen["status"] = kwargs.get("status")
        return None

    monkeypatch.setattr(NoticeCRUD, "set", fake_set)

    await NoticeService(SimpleNamespace(), SimpleNamespace()).set_available(
        SimpleNamespace(ids=[1, 2], status=0)
    )

    assert seen == {"ids": [1, 2], "status": 0}


@pytest.mark.asyncio
async def test_notice_create_rejects_duplicate_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self, *, notice_title, **kwargs):
        return SimpleNamespace(id=1, notice_title=notice_title)

    async def fake_create(self, *, data, **kwargs):
        raise AssertionError("重复标题不应继续创建")

    monkeypatch.setattr(NoticeCRUD, "get", fake_get)
    monkeypatch.setattr(NoticeCRUD, "create", fake_create)

    with pytest.raises(CustomException):
        await NoticeService(SimpleNamespace(), SimpleNamespace()).create(
            SimpleNamespace(notice_title="公告")
        )


@pytest.mark.asyncio
async def test_notice_delete_rejects_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_list(self, *, search, include_deleted=False, **kwargs):
        # 只有 id=1 存在
        return [SimpleNamespace(id=1)]

    async def fake_delete(self, ids, **kwargs):
        raise AssertionError("存在缺失 id 时不应执行删除")

    monkeypatch.setattr(NoticeCRUD, "get_list", fake_get_list)
    monkeypatch.setattr(NoticeCRUD, "delete", fake_delete)

    with pytest.raises(CustomException):
        await NoticeService(SimpleNamespace(), SimpleNamespace()).delete([1, 2])
