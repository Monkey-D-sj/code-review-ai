"""存储源定时清理处理器

供定时任务节点调用：扫描启用中的存储源，对超过保留期的远端对象做清理。
存储源密码统一通过 core 包导出的解密别名获取（跨模块复用，业务方不直接
依赖加密实现细节）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.api.v1.module_storage.core import decrypt_storage_password
from app.api.v1.module_storage.core.base import StorageAdapterConfig
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.api.v1.module_storage.core.factory import StorageAdapterFactory
from app.api.v1.module_storage.source.crud import StorageSourceCRUD
from app.api.v1.module_storage.source.model import StorageSourceModel
from app.core.base_schema import AuthSchema
from app.core.logger import logger


def _system_auth() -> AuthSchema:
    """定时任务以系统身份访问数据层。"""
    return AuthSchema(id=0, username="system", role="system")


def _build_adapter_config(source: StorageSourceModel) -> StorageAdapterConfig:
    """把存储源记录转成适配器配置，密码用解密别名还原为明文。"""
    return StorageAdapterConfig(
        protocol=StorageProtocol(source.protocol),
        host=source.host,
        port=source.port,
        username=source.username,
        password=decrypt_storage_password(source.password),
        bucket=source.bucket,
        endpoint=source.endpoint,
        region=source.region,
        path_prefix=source.path_prefix,
        is_secure=source.is_secure,
        implicit_tls=source.implicit_tls,
    )


async def cleanup_expired_storage_objects(session, keep_days: int = 90) -> dict:
    """遍历启用中的存储源，删除超过保留期的远端临时目录，返回清理数量。"""
    crud = StorageSourceCRUD(_system_auth(), session)
    sources = await crud.get_list(status=0)
    cutoff = datetime.now() - timedelta(days=keep_days)
    cleaned = 0
    for source in sources:
        adapter = StorageAdapterFactory.create(_build_adapter_config(source))
        stale = await _list_stale_remote_keys(adapter, source, cutoff)
        for remote_path in stale:
            await adapter.delete(remote_path)
            cleaned += 1
    logger.info("storage cleanup done: cleaned=%s cutoff=%s", cleaned, cutoff)
    return {"cleaned": cleaned, "cutoff": cutoff.isoformat()}


async def _list_stale_remote_keys(adapter, source: StorageSourceModel,
                                  cutoff: datetime) -> list[str]:
    """从适配器列出远端对象并过滤出早于 cutoff 的过期路径（示意实现）。"""
    return []
