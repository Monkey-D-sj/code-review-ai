"""工作流内置业务节点

内置节点以“代码即配置”的方式将存储、中转、通知等模块能力封装为
开箱即用的工作流节点，用户无需编写 Python 代码即可在画布上串联
多个模块，形成通用业务闭环。

执行约定：
- 引擎在 ThreadPoolExecutor 线程中调用节点 handler（同步函数），
  因此 handler 内部通过 asyncio.run() 执行异步业务逻辑；
- handler 统一接收 **kwargs，其中 upstream / variables 由引擎注入，
  业务参数直接从 kwargs 中读取；
- 内置节点不依赖数据库中的节点类型记录，引擎执行时优先命中内置节点，
  其次才查询自定义节点类型。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.api.v1.module_storage.core.base import StorageAdapterConfig
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.api.v1.module_storage.core.encrypt import decrypt_password as decrypt_storage_password
from app.api.v1.module_storage.core.factory import StorageAdapterFactory
from app.api.v1.module_storage.source.crud import StorageSourceCRUD
from app.api.v1.module_system.notice.crud import NoticeCRUD
from app.core.base_schema import AuthSchema
from app.core.database import async_db_session
from app.core.logger import logger


@dataclass
class BuiltinNode:
    """内置业务节点定义"""

    code: str
    name: str
    category: str
    description: str
    handler: Callable[..., Any]
    args: str = ""
    kwargs: str = "{}"


BUILTIN_NODES: dict[str, BuiltinNode] = {}


def builtin_node(*, code: str, name: str, category: str = "action", description: str = "", args: str = "", kwargs: str = "{}") -> Callable:
    """注册内置节点。"""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        BUILTIN_NODES[code] = BuiltinNode(
            code=code,
            name=name,
            category=category,
            description=description,
            handler=fn,
            args=args,
            kwargs=kwargs,
        )
        return fn

    return deco


def get_builtin_node(code: str) -> BuiltinNode | None:
    return BUILTIN_NODES.get(code)


def builtin_node_options() -> list[dict]:
    """内置节点选项（合并进工作流设计器的节点类型选择）。"""
    return [
        {
            "id": None,
            "code": n.code,
            "name": n.name,
            "category": n.category,
            "description": n.description,
            "args": n.args,
            "kwargs": n.kwargs,
        }
        for n in BUILTIN_NODES.values()
    ]


def _run_async(coro: Any) -> Any:
    """在线程中运行协程（工作流引擎在 ThreadPoolExecutor 中执行节点，无事件循环）。"""
    return asyncio.run(coro)


def _system_auth() -> AuthSchema:
    """系统级调用上下文（不填充审计人，避免伪造用户）。"""
    return AuthSchema()


# ── 存储节点 ──────────────────────────────────────────────────────


async def _resolve_storage_config(session, source_id: int | None) -> StorageAdapterConfig:
    """解析存储源配置（密码解密），source_id 为空时取默认源/任一启用源。"""
    crud = StorageSourceCRUD(_system_auth(), session)
    if source_id:
        source = await crud.get_or_404(id=source_id, msg="存储源不存在")
        if source.status == 1:
            raise ValueError(f"存储源「{source.name}」已停用")
    else:
        source = await crud.get(status=0, is_default=True)
        if source is None:
            source = await crud.get(status=0)
        if source is None:
            raise ValueError("未配置可用的存储源，请先在存储源管理中创建")
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


@builtin_node(
    code="storage_upload",
    name="上传文件到存储源",
    description="将服务器本地文件上传到指定存储源，返回远端路径与访问URL。",
    kwargs='{"local_path": "/tmp/a.txt", "remote_path": "inbox/a.txt"}',
)
def storage_upload(**kwargs: Any) -> dict:
    local_path = kwargs.get("local_path")
    remote_path = kwargs.get("remote_path")
    if not local_path or not remote_path:
        raise ValueError("storage_upload 需要 local_path 与 remote_path 参数")
    return _run_async(_storage_upload(kwargs.get("source_id"), str(local_path), str(remote_path)))


async def _storage_upload(source_id: int | None, local_path: str, remote_path: str) -> dict:
    async with async_db_session() as session:
        config = await _resolve_storage_config(session, source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            await adapter.upload(local_path, remote_path)
            file_url = await adapter.get_url(remote_path)
        finally:
            await adapter.close()
    logger.info("工作流节点 storage_upload 完成: remote={}", remote_path)
    return {"file_path": remote_path, "file_url": file_url}


@builtin_node(
    code="storage_download",
    name="从存储源下载文件",
    description="将存储源文件下载到服务器本地，返回本地路径与文件名。",
    kwargs='{"remote_path": "inbox/a.txt"}',
)
def storage_download(**kwargs: Any) -> dict:
    remote_path = kwargs.get("remote_path")
    if not remote_path:
        raise ValueError("storage_download 需要 remote_path 参数")
    return _run_async(_storage_download(kwargs.get("source_id"), str(remote_path), kwargs.get("local_path")))


async def _storage_download(source_id: int | None, remote_path: str, local_path: str | None) -> dict:
    async with async_db_session() as session:
        config = await _resolve_storage_config(session, source_id)
        extension = os.path.splitext(remote_path)[1]
        fd, tmp_path = tempfile.mkstemp(suffix=extension)
        os.close(fd)
        adapter = StorageAdapterFactory.create(config)
        try:
            await adapter.download(remote_path, local_path or tmp_path)
        except Exception:
            os.unlink(tmp_path)
            raise
        finally:
            await adapter.close()
    target = local_path or tmp_path
    logger.info("工作流节点 storage_download 完成: local={}", target)
    return {"local_path": target, "file_name": os.path.basename(remote_path)}


@builtin_node(
    code="storage_url",
    name="获取文件访问URL",
    description="获取存储源文件的访问URL（预签名，默认1小时有效）。",
    kwargs='{"remote_path": "inbox/a.txt", "expire": 3600}',
)
def storage_url(**kwargs: Any) -> dict:
    remote_path = kwargs.get("remote_path")
    if not remote_path:
        raise ValueError("storage_url 需要 remote_path 参数")
    return _run_async(_storage_url(kwargs.get("source_id"), str(remote_path), int(kwargs.get("expire", 3600))))


async def _storage_url(source_id: int | None, remote_path: str, expire: int) -> dict:
    async with async_db_session() as session:
        config = await _resolve_storage_config(session, source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            file_url = await adapter.get_url(remote_path, expire=expire)
        finally:
            await adapter.close()
    return {"file_url": file_url}


@builtin_node(
    code="storage_exists",
    name="判断存储文件是否存在",
    description="检查存储源中指定文件是否存在，返回布尔结果，可作为条件节点使用。",
    category="condition",
    kwargs='{"remote_path": "inbox/a.txt"}',
)
def storage_exists(**kwargs: Any) -> dict:
    remote_path = kwargs.get("remote_path")
    if not remote_path:
        raise ValueError("storage_exists 需要 remote_path 参数")
    return _run_async(_storage_exists(kwargs.get("source_id"), str(remote_path)))


async def _storage_exists(source_id: int | None, remote_path: str) -> dict:
    async with async_db_session() as session:
        config = await _resolve_storage_config(session, source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            exists = await adapter.exists(remote_path)
        finally:
            await adapter.close()
    return {"exists": exists}


@builtin_node(
    code="storage_delete",
    name="删除存储文件",
    description="删除存储源中的指定文件。",
    kwargs='{"remote_path": "inbox/a.txt"}',
)
def storage_delete(**kwargs: Any) -> dict:
    remote_path = kwargs.get("remote_path")
    if not remote_path:
        raise ValueError("storage_delete 需要 remote_path 参数")
    return _run_async(_storage_delete(kwargs.get("source_id"), str(remote_path)))


async def _storage_delete(source_id: int | None, remote_path: str) -> dict:
    async with async_db_session() as session:
        config = await _resolve_storage_config(session, source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            await adapter.delete(remote_path)
        finally:
            await adapter.close()
    return {"deleted": True, "file_path": remote_path}


# ── 通知节点 ──────────────────────────────────────────────────────


@builtin_node(
    code="notice_send",
    name="发送站内通知",
    description="发送一条站内通知（默认已发布，类型为通知）。",
    kwargs='{"notice_title": "工作流通知", "notice_content": "任务执行完成"}',
)
def notice_send(**kwargs: Any) -> dict:
    title = kwargs.get("notice_title")
    if not title:
        raise ValueError("notice_send 需要 notice_title 参数")
    notice_type = str(kwargs.get("notice_type", "1"))
    status = int(kwargs.get("status", 1))
    content = kwargs.get("notice_content")
    return _run_async(_notice_send(str(title), str(content) if content is not None else None, notice_type, status))


async def _notice_send(title: str, content: str | None, notice_type: str, status: int) -> dict:
    async with async_db_session() as session:
        obj = await NoticeCRUD(_system_auth(), session).create(
            {
                "notice_title": title,
                "notice_type": notice_type,
                "notice_content": content,
                "status": status,
            }
        )
    logger.info("工作流节点 notice_send 完成: notice={}", obj.id)
    return {"notice_id": obj.id}


# ── AI 节点 ──────────────────────────────────────────────────────


@builtin_node(
    code="ai_chat",
    name="AI 对话/摘要",
    description="调用 AI 模型处理文本（需先在 AI 模块配置并启用模型），返回模型回复。",
    kwargs='{"message": "请总结以下内容: ..."}',
)
def ai_chat(**kwargs: Any) -> dict:
    message = kwargs.get("message")
    if not message:
        raise ValueError("ai_chat 需要 message 参数")
    session_id = kwargs.get("session_id")
    return _run_async(_ai_chat(str(message), str(session_id) if session_id else None))


async def _ai_chat(message: str, session_id: str | None) -> dict:
    from app.api.v1.module_ai.chat.service import ChatService

    service = ChatService(_system_auth())
    result = await service.chat_non_stream(message, session_id)
    logger.info("工作流节点 ai_chat 完成: session={}", result.get("session_id"))
    return {"response": result.get("response"), "session_id": result.get("session_id")}


# ── 通用检查节点 ──────────────────────────────────────────────────


@builtin_node(
    code="http_check",
    name="HTTP 健康检查",
    description="请求指定 URL，HTTP 2xx/3xx 视为健康，返回布尔结果，可作为条件节点使用。",
    category="condition",
    kwargs='{"url": "http://127.0.0.1:8000/api/v1/health", "timeout": 5}',
)
def http_check(**kwargs: Any) -> dict:
    url = kwargs.get("url")
    if not url:
        raise ValueError("http_check 需要 url 参数")
    return _run_async(_http_check(str(url), float(kwargs.get("timeout", 5))))


async def _http_check(url: str, timeout: float) -> dict:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        healthy = 200 <= resp.status_code < 400
        return {"healthy": healthy, "status_code": resp.status_code, "url": url}
    except Exception as e:
        return {"healthy": False, "status_code": None, "url": url, "error": str(e)}
