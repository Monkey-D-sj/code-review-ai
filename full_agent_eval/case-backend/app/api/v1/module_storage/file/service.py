import os
import tempfile
from pathlib import Path

import aiofiles
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.module_storage.core.base import StorageAdapterConfig, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.api.v1.module_storage.core.encrypt import decrypt_password
from app.api.v1.module_storage.core.factory import StorageAdapterFactory
from app.api.v1.module_storage.source.service import StorageSourceService
from app.core.base_schema import AuthSchema
from app.core.exceptions import CustomException
from app.utils.upload_util import UploadUtil


class StorageFileService:
    """存储文件操作服务（上传/下载/删除/列表/预签名URL）"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        self.auth = auth
        self.db = db

    # ── 内部工具 ────────────────────────────────────────────────────

    @staticmethod
    def _validate_remote_path(remote_path: str) -> str:
        """规范化并校验远端相对路径（禁止路径穿越）。"""
        if not remote_path or not remote_path.strip():
            raise CustomException(msg="请提供文件路径")
        parts = [p for p in remote_path.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts) or "\x00" in remote_path:
            raise CustomException(msg="非法的文件路径")
        return "/".join(parts)

    async def _get_source(self, source_id: int | None) -> StorageAdapterConfig:
        """获取存储源并构造适配器配置（密码已解密）。"""
        source = await StorageSourceService(self.auth, self.db).get_active_source(source_id)
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

    @staticmethod
    async def _save_to_temp(file: UploadFile, suffix: str = "") -> str:
        """将上传文件内容落盘到系统临时目录，返回临时路径。"""
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            async with aiofiles.open(path, "wb") as f:
                while chunk := await file.read(1024 * 1024):
                    await f.write(chunk)
        except Exception:
            os.unlink(path)
            raise
        finally:
            await file.seek(0)
        return path

    # ── 业务方法 ────────────────────────────────────────────────────

    async def upload(
        self,
        source_id: int | None,
        file: UploadFile,
        remote_path: str | None = None,
    ) -> dict:
        """上传文件到远端存储。remote_path 为空时自动生成安全文件名。"""
        if not file or not file.filename:
            raise CustomException(msg="请选择要上传的文件")

        if not UploadUtil.check_path_traversal(file.filename):
            raise CustomException(msg="文件名包含非法字符")
        extension = UploadUtil.get_extension_from_filename(file.filename)
        if not extension:
            raise CustomException(msg="无法识别文件类型")
        if UploadUtil.is_dangerous_extension(extension):
            raise CustomException(msg=f"不允许上传此类型的文件: {extension}")
        UploadUtil.check_file_size(file)

        # 确定远端路径
        if remote_path:
            if remote_path.endswith("/"):
                # 以 / 结尾视为目录：保留原文件名，拼接到目录下
                dir_path = self._validate_remote_path(remote_path)
                target = f"{dir_path}/{file.filename}"
            else:
                target = self._validate_remote_path(remote_path)
                if not target.endswith(extension):
                    target = f"{target}{extension}"
        else:
            target = UploadUtil.generate_safe_filename(file.filename, extension)

        config = await self._get_source(source_id)
        temp_path = await self._save_to_temp(file, suffix=extension)
        adapter = StorageAdapterFactory.create(config)
        try:
            await adapter.upload(temp_path, target)
            file_url = await adapter.get_url(target)
        finally:
            await adapter.close()
            os.unlink(temp_path)

        return {
            "file_path": target,
            "file_name": Path(target).name,
            "origin_name": file.filename,
            "file_url": file_url,
        }

    async def download(self, source_id: int | None, remote_path: str) -> tuple[str, str]:
        """下载远端文件到临时目录，返回 (本地临时路径, 文件名)。"""
        target = self._validate_remote_path(remote_path)
        config = await self._get_source(source_id)
        extension = Path(target).suffix
        fd, temp_path = tempfile.mkstemp(suffix=extension)
        os.close(fd)
        adapter = StorageAdapterFactory.create(config)
        try:
            local_path = await adapter.download(target, temp_path)
        except Exception:
            os.unlink(temp_path)
            raise
        finally:
            await adapter.close()
        return local_path, Path(target).name

    async def delete(self, source_id: int | None, remote_path: str) -> None:
        target = self._validate_remote_path(remote_path)
        config = await self._get_source(source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            await adapter.delete(target)
        finally:
            await adapter.close()

    async def exists(self, source_id: int | None, remote_path: str) -> bool:
        target = self._validate_remote_path(remote_path)
        config = await self._get_source(source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            return await adapter.exists(target)
        finally:
            await adapter.close()

    async def list(self, source_id: int | None, prefix: str = "") -> list[StorageObject]:
        safe_prefix = self._validate_remote_path(prefix) if prefix else ""
        config = await self._get_source(source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            return await adapter.list(safe_prefix)
        finally:
            await adapter.close()

    async def get_url(self, source_id: int | None, remote_path: str, expire: int = 3600) -> str | None:
        target = self._validate_remote_path(remote_path)
        config = await self._get_source(source_id)
        adapter = StorageAdapterFactory.create(config)
        try:
            return await adapter.get_url(target, expire=expire)
        finally:
            await adapter.close()

    async def copy_or_move(
        self,
        source_id: int | None,
        source_path: str,
        target_id: int,
        target_path: str,
        move: bool = False,
    ) -> dict:
        """复制/移动文件：跨端点时下载到临时再上传；同端点 move 即重命名。"""
        src = self._validate_remote_path(source_path)
        dst = self._validate_remote_path(target_path)
        if move and source_id == target_id and src == dst:
            raise CustomException(msg="源路径与目标路径相同")
        source_config = await self._get_source(source_id)
        target_config = await self._get_source(target_id)
        fd, temp_path = tempfile.mkstemp(suffix=Path(dst).suffix)
        os.close(fd)
        src_adapter = StorageAdapterFactory.create(source_config)
        dst_adapter = StorageAdapterFactory.create(target_config)
        try:
            await src_adapter.download(src, temp_path)
            await dst_adapter.upload(temp_path, dst)
            if move:
                await src_adapter.delete(src)
        finally:
            await src_adapter.close()
            await dst_adapter.close()
            os.unlink(temp_path)
        return {"source_path": src, "target_path": dst}
