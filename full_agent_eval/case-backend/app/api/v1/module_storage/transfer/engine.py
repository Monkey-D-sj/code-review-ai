"""文件传输任务执行引擎

- parallel（多目标）：单源依次输出到多个目标端点
- chain（链式）：步骤串联，上一步目标端点即下一步源，链条长度不限
- 进度按步骤粒度统计（SDK 无逐字节回调），实时写入 DB 并经 WebSocket 推送
- 后台任务在独立 DB 会话中运行，不阻塞请求；取消采用内存标志（当前步骤执行完毕后生效）
"""

import os
import tempfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.module_storage.core.base import StorageAdapterConfig
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.api.v1.module_storage.core.encrypt import decrypt_password
from app.api.v1.module_storage.core.factory import StorageAdapterFactory
from app.api.v1.module_storage.source.model import StorageSourceModel
from app.api.v1.module_storage.transfer.registry import transfer_task_registry
from app.api.v1.module_storage.transfer.ws_manager import transfer_ws_manager
from app.core.database import async_db_session
from app.core.logger import logger

from .model import StorageTransferStepModel, StorageTransferTaskModel

# 单步最大可显示进度（步骤执行中为流动状态，完成后置 100）
_STEP_RUNNING_PROGRESS = 50


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _step_payload(step: StorageTransferStepModel) -> dict:
    return {
        "id": step.id,
        "step_order": step.step_order,
        "source_id": step.source_id,
        "source_path": step.source_path,
        "target_id": step.target_id,
        "target_path": step.target_path,
        "status": step.status,
        "progress": step.progress,
        "speed": step.speed,
        "total_size": step.total_size,
        "transferred_size": step.transferred_size,
        "error_msg": step.error_msg,
        "started_at": _dt(step.started_at),
        "finished_at": _dt(step.finished_at),
    }


def _task_payload(task: StorageTransferTaskModel, steps: list[StorageTransferStepModel]) -> dict:
    return {
        "id": task.id,
        "name": task.name,
        "task_type": task.task_type,
        "source_type": task.source_type,
        "source_id": task.source_id,
        "source_path": task.source_path,
        "source_name": task.source_name,
        "source_size": task.source_size,
        "status": task.status,
        "total_size": task.total_size,
        "transferred_size": task.transferred_size,
        "progress": task.progress,
        "speed": task.speed,
        "error_msg": task.error_msg,
        "started_at": _dt(task.started_at),
        "finished_at": _dt(task.finished_at),
        "steps": [_step_payload(s) for s in steps],
    }


async def _broadcast(task: StorageTransferTaskModel, steps: list[StorageTransferStepModel]) -> None:
    await transfer_ws_manager.send_to_user(
        task.created_id,
        {"type": "task_update", "data": _task_payload(task, steps)},
    )


async def _build_config(db: AsyncSession, source_id: int) -> StorageAdapterConfig | None:
    source = await db.get(StorageSourceModel, source_id)
    if source is None or source.status == 1:
        return None
    return StorageAdapterConfig(
        protocol=StorageProtocol(source.protocol),
        host=source.host,
        port=source.port,
        username=source.username,
        password=decrypt_password(source.password),
        bucket=source.bucket,
        endpoint=source.endpoint,
        region=source.region,
        path_prefix=source.path_prefix,
        is_secure=source.is_secure,
        implicit_tls=source.implicit_tls,
    )


async def _resolve_source_size(adapter, source_path: str) -> int:
    """尽力获取远端源文件大小（对象存储/FTP 均可通过 list 精确匹配）。"""
    try:
        objects = await adapter.list(source_path)
        for obj in objects:
            if not obj.is_dir and obj.key == source_path and obj.size:
                return obj.size
    except Exception:
        pass
    return 0


async def _run_step(db: AsyncSession, task: StorageTransferTaskModel, step: StorageTransferStepModel) -> bool:
    """执行单个传输步骤，成功返回 True。"""
    started_at = datetime.now(UTC)
    step.status = "running"
    step.started_at = started_at
    step.progress = _STEP_RUNNING_PROGRESS
    await db.commit()
    await _broadcast(task, await _load_steps(db, task.id))

    temp_path: str | None = None
    src_adapter = None
    dst_adapter = None
    try:
        # 解析源：本地临时文件直接使用；远端源下载到临时文件
        if step.source_id is not None:
            src_config = await _build_config(db, step.source_id)
            if src_config is None:
                raise RuntimeError(f"源存储源 {step.source_id} 不存在或已停用")
            src_adapter = StorageAdapterFactory.create(src_config)
            fd, temp_path = tempfile.mkstemp(prefix="transfer_", suffix=os.path.splitext(step.target_path)[1])
            os.close(fd)
            await src_adapter.download(step.source_path or "", temp_path)
        else:
            temp_path = step.source_path or ""

        if not temp_path or not os.path.exists(temp_path):
            raise RuntimeError("源文件不存在")

        size = os.path.getsize(temp_path)
        dst_config = await _build_config(db, step.target_id)
        if dst_config is None:
            raise RuntimeError(f"目标存储源 {step.target_id} 不存在或已停用")
        dst_adapter = StorageAdapterFactory.create(dst_config)
        await dst_adapter.upload(temp_path, step.target_path)

        elapsed = (datetime.now(UTC) - started_at).total_seconds() or 0.01
        speed = size / elapsed
        step.total_size = size
        step.transferred_size = size
        step.speed = speed
        step.status = "success"
        step.progress = 100
        step.finished_at = datetime.now(UTC)
        task.transferred_size += size
        task.speed = speed
        if task.total_size > 0:
            task.progress = min(99, int(task.transferred_size * 100 / task.total_size))
        await db.commit()
        await _broadcast(task, await _load_steps(db, task.id))
        return True
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        step.status = "failed"
        step.error_msg = msg
        step.finished_at = datetime.now(UTC)
        task.status = "failed"
        task.error_msg = msg
        task.finished_at = datetime.now(UTC)
        await db.commit()
        await _broadcast(task, await _load_steps(db, task.id))
        logger.warning("传输任务 {}(步骤 {}) 失败: {}", task.id, step.step_order, msg)
        return False
    finally:
        if src_adapter is not None:
            await src_adapter.close()
        if dst_adapter is not None:
            await dst_adapter.close()
        if temp_path and step.source_id is not None and os.path.exists(temp_path):
            os.unlink(temp_path)


async def _load_steps(db: AsyncSession, task_id: int) -> list[StorageTransferStepModel]:
    result = await db.execute(
        select(StorageTransferStepModel).where(StorageTransferStepModel.task_id == task_id).order_by(StorageTransferStepModel.step_order)
    )
    return list(result.scalars().all())


async def execute_transfer_task(task_id: int) -> None:
    """后台执行传输任务（由创建接口以 asyncio.create_task 启动）。"""
    async with async_db_session() as db:
        task = await db.get(StorageTransferTaskModel, task_id)
        if task is None or task.status != "pending":
            return
        steps = await _load_steps(db, task_id)
        if not steps:
            task.status = "failed"
            task.error_msg = "任务没有可执行的步骤"
            task.finished_at = datetime.now(UTC)
            await db.commit()
            return

        # 解析源文件大小，用于总进度估算
        if task.source_type == "local" and task.source_path and os.path.exists(task.source_path):
            task.source_size = os.path.getsize(task.source_path)
        elif task.source_type == "remote" and task.source_id:
            config = await _build_config(db, task.source_id)
            if config is None:
                task.status = "failed"
                task.error_msg = f"源存储源 {task.source_id} 不存在或已停用"
                task.finished_at = datetime.now(UTC)
                await db.commit()
                await _broadcast(task, steps)
                return
            adapter = StorageAdapterFactory.create(config)
            try:
                task.source_size = await _resolve_source_size(adapter, task.source_path or "")
            finally:
                await adapter.close()
        # 总字节 = 源大小 × 步骤数（每步传输一次源文件，parallel 与 chain 相同）
        task.total_size = (task.source_size or 0) * len(steps)
        task.status = "running"
        task.started_at = datetime.now(UTC)
        await db.commit()
        await _broadcast(task, steps)

        completed = 0
        canceled = False
        for step in steps:
            if transfer_task_registry.is_canceled(task_id):
                canceled = True
                break
            if await _run_step(db, task, step):
                completed += 1
            else:
                break

        if canceled:
            task.status = "canceled"
            task.error_msg = None
            for step in steps:
                if step.status == "pending":
                    step.status = "canceled"
                    step.finished_at = datetime.now(UTC)
        elif completed == len(steps):
            task.status = "success"
            task.progress = 100
        task.finished_at = datetime.now(UTC)
        transfer_task_registry.clear(task_id)
        await db.commit()
        await _broadcast(task, steps)
        logger.info("传输任务 {} 结束: {}", task_id, task.status)

        # 清理本地源临时文件
        if task.source_type == "local" and task.source_path and os.path.exists(task.source_path):
            try:
                os.unlink(task.source_path)
            except OSError:
                pass
