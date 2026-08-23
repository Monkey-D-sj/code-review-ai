import asyncio
import os
import shutil
from datetime import datetime

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class LocalStorageAdapter(BaseStorageAdapter):
    """本地磁盘/挂载目录存储适配器（零 SDK，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.LOCAL

    def __init__(self, config) -> None:
        super().__init__(config)
        self.root = config.host or ""

    def _abs_path(self, remote_path: str) -> str:
        """将远端相对路径映射为本地绝对路径，并防御路径穿越。"""
        if ".." in remote_path:
            raise CustomException(msg="本地存储不允许路径穿越（..）")
        return os.path.join(self.root, remote_path)

    def _sync_test_connection(self) -> bool:
        if not self.root or not os.path.isdir(self.root):
            logger.warning(f"本地存储根目录不可用: {self.root}")
            return False
        return os.access(self.root, os.R_OK | os.W_OK)

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        dest = self._abs_path(remote_path)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(local_path, dest)
        except Exception as e:
            raise CustomException(msg=f"本地存储上传失败: {e!s}")
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        src = self._abs_path(remote_path)
        try:
            shutil.copy2(src, local_path)
        except Exception as e:
            raise CustomException(msg=f"本地存储下载失败: {e!s}")
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        target = self._abs_path(remote_path)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
        except Exception as e:
            raise CustomException(msg=f"本地存储删除失败: {e!s}")

    def _sync_exists(self, remote_path: str) -> bool:
        return os.path.exists(self._abs_path(remote_path))

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        base_dir = self._abs_path(prefix) if prefix else self.root
        try:
            entries = os.scandir(base_dir)
        except OSError as e:
            raise CustomException(msg=f"本地存储列表失败: {e!s}")

        result: list[StorageObject] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
                stat = entry.stat()
            except OSError:
                continue
            key = f"{prefix}/{entry.name}".strip("/") if prefix else entry.name
            result.append(
                StorageObject(
                    name=entry.name,
                    key=self._strip_prefix(key),
                    is_dir=is_dir,
                    size=None if is_dir else stat.st_size,
                    modified_time=datetime.fromtimestamp(stat.st_mtime),
                )
            )
        return result

    # ── 异步公开接口 ────────────────────────────────────────────────

    async def test_connection(self) -> bool:
        return await asyncio.to_thread(self._sync_test_connection)

    async def upload(self, local_path: str, remote_path: str) -> str:
        full_key = self._join_key(remote_path)
        return await asyncio.to_thread(self._sync_upload, local_path, full_key)

    async def download(self, remote_path: str, local_path: str) -> str:
        full_key = self._join_key(remote_path)
        return await asyncio.to_thread(self._sync_download, full_key, local_path)

    async def delete(self, remote_path: str) -> None:
        full_key = self._join_key(remote_path)
        await asyncio.to_thread(self._sync_delete, full_key)

    async def exists(self, remote_path: str) -> bool:
        full_key = self._join_key(remote_path)
        return await asyncio.to_thread(self._sync_exists, full_key)

    async def list(self, prefix: str = "") -> list[StorageObject]:
        full_prefix = self._join_key(prefix) if prefix else self.config.full_prefix
        return await asyncio.to_thread(self._sync_list, full_prefix)
