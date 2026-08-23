import asyncio
import os
import tempfile
from datetime import UTC, datetime

import aiofiles
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.module_storage.source.service import StorageSourceService
from app.api.v1.module_storage.transfer.engine import execute_transfer_task
from app.api.v1.module_storage.transfer.registry import transfer_task_registry
from app.core.base_schema import AuthSchema, PageResultSchema
from app.core.exceptions import CustomException
from app.utils.common_util import search_to_dict

from .crud import StorageTransferTaskCRUD
from .model import StorageTransferStepModel
from .schema import TransferStepOutSchema, TransferTargetSchema, TransferTaskCreateSchema, TransferTaskOutSchema, TransferTaskQueryParam


class StorageTransferService:
    """文件传输任务服务（创建 / 查询 / 取消 / 删除）"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        self.auth = auth
        self.db = db

    # ── 内部工具 ────────────────────────────────────────────────────

    def _crud(self) -> StorageTransferTaskCRUD:
        return StorageTransferTaskCRUD(self.auth, self.db)

    async def _validate_targets(self, targets: list[TransferTargetSchema]) -> None:
        """校验目标存储源均存在且启用。"""
        source_service = StorageSourceService(self.auth, self.db)
        for target in targets:
            await source_service.get_active_source(target.target_id)

    @staticmethod
    def _build_steps(data: TransferTaskCreateSchema, local_source_path: str | None = None) -> list[dict]:
        """展开步骤：chain 下每步源继承上一步的目标；parallel 下每步源均为任务源。

        local 源时任务源为服务端临时文件，需显式传入 local_source_path。
        """
        steps: list[dict] = []
        if data.task_type == "chain":
            prev_id, prev_path = data.source_id, local_source_path or data.source_path
            for order, target in enumerate(data.targets):
                steps.append(
                    {
                        "step_order": order,
                        "source_id": prev_id,
                        "source_path": prev_path,
                        "target_id": target.target_id,
                        "target_path": target.target_path,
                    }
                )
                prev_id, prev_path = target.target_id, target.target_path
        else:
            for order, target in enumerate(data.targets):
                steps.append(
                    {
                        "step_order": order,
                        "source_id": data.source_id,
                        "source_path": local_source_path or data.source_path,
                        "target_id": target.target_id,
                        "target_path": target.target_path,
                    }
                )
        return steps

    async def _persist(self, data: TransferTaskCreateSchema, local_info: dict | None = None) -> int:
        """落库任务与步骤（pending），随后启动后台执行。"""
        task = await self._crud().create(
            {
                "name": data.name,
                "task_type": data.task_type,
                "source_type": data.source_type,
                "source_id": data.source_id,
                "source_path": local_info["source_path"] if local_info else data.source_path,
                "source_name": local_info["source_name"] if local_info else ((data.source_path or "").rsplit("/", 1)[-1] or None),
                "source_size": local_info.get("source_size") if local_info else None,
                "status": "pending",
            }
        )
        local_source_path = local_info["source_path"] if local_info else None
        for step_data in self._build_steps(data, local_source_path=local_source_path):
            self.db.add(StorageTransferStepModel(task_id=task.id, **step_data))
        await self.db.commit()
        await self._launch(task.id)
        return task.id

    async def _launch(self, task_id: int) -> None:
        """启动后台执行任务（不阻塞请求）。"""
        asyncio.create_task(execute_transfer_task(task_id))

    # ── 创建 ────────────────────────────────────────────────────────

    async def create(self, data: TransferTaskCreateSchema) -> int:
        """创建远端源传输任务。"""
        source_service = StorageSourceService(self.auth, self.db)
        if data.source_type == "remote":
            await source_service.get_active_source(data.source_id)
        await self._validate_targets(data.targets)
        return await self._persist(data)

    async def create_local(self, data: TransferTaskCreateSchema, file: UploadFile) -> int:
        """创建本地源传输任务：文件保存到服务端临时目录，执行完毕后自动清理。"""
        if not file or not file.filename:
            raise CustomException(msg="请选择要上传的文件")
        await self._validate_targets(data.targets)
        fd, temp_path = tempfile.mkstemp(prefix="transfer_upload_", suffix=os.path.splitext(file.filename)[1])
        os.close(fd)
        try:
            async with aiofiles.open(temp_path, "wb") as f:
                while chunk := await file.read(1024 * 1024):
                    await f.write(chunk)
        except Exception:
            os.unlink(temp_path)
            raise
        finally:
            await file.seek(0)
        return await self._persist(
            data,
            local_info={"source_path": temp_path, "source_name": file.filename, "source_size": os.path.getsize(temp_path)},
        )

    # ── 查询 ────────────────────────────────────────────────────────

    async def page(
        self,
        search: TransferTaskQueryParam | None,
        page_no: int,
        page_size: int,
        order_by: list[dict] | None = None,
    ) -> PageResultSchema[TransferTaskOutSchema]:
        result = await self._crud().page(
            offset=(page_no - 1) * page_size,
            limit=page_size,
            order_by=order_by or [{"id": "desc"}],
            search=search_to_dict(search),
        )
        return PageResultSchema[TransferTaskOutSchema](
            page_no=result.page_no,
            page_size=result.page_size,
            total=result.total,
            has_next=result.has_next,
            items=[TransferTaskOutSchema.model_validate(obj) for obj in result.items],
        )

    async def detail(self, task_id: int) -> TransferTaskOutSchema:
        task = await self._crud().get_or_404(id=task_id)
        out = TransferTaskOutSchema.model_validate(task)
        result = await self.db.execute(
            select(StorageTransferStepModel)
            .where(
                StorageTransferStepModel.task_id == task_id,
                StorageTransferStepModel.is_deleted.is_(False),
            )
            .order_by(StorageTransferStepModel.step_order)
        )
        out.steps = [TransferStepOutSchema.model_validate(step) for step in result.scalars().all()]
        return out

    # ── 操作 ────────────────────────────────────────────────────────

    async def cancel(self, task_id: int) -> None:
        task = await self._crud().get_or_404(id=task_id)
        if task.status == "pending":
            task.status = "canceled"
            task.finished_at = datetime.now(UTC)
            await self.db.flush()
        elif task.status == "running":
            transfer_task_registry.mark_cancel(task_id)

    async def delete(self, ids: list[int]) -> None:
        for task_id in ids:
            transfer_task_registry.mark_cancel(task_id)
        await self._crud().delete(ids=ids)
