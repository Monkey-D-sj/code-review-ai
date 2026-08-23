import asyncio
from datetime import UTC, datetime
from typing import Any

from obs import ObsClient

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class ObsStorageAdapter(BaseStorageAdapter):
    """华为云 OBS 存储适配器（esdk-obs-python，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.OBS

    def __init__(self, config) -> None:
        super().__init__(config)
        if not self.config.endpoint:
            raise CustomException(msg="OBS 存储源必须配置 endpoint")
        self.client = ObsClient(
            access_key_id=self.config.username or "",
            secret_access_key=self.config.password or "",
            server=self.config.endpoint,
        )

    def _require_bucket(self) -> str:
        if not self.config.bucket:
            raise CustomException(msg="存储源未配置 bucket")
        return self.config.bucket

    @staticmethod
    def _is_ok(resp: Any) -> bool:
        """判断 OBS 响应是否成功（status < 300）。SDK 未提供类型存根，故用 getattr 访问动态属性。"""
        status = getattr(resp, "status", None)
        return status is not None and status < 300

    @staticmethod
    def _error_desc(resp: Any) -> str:
        """提取 OBS 响应中的错误描述。"""
        code = getattr(resp, "errorCode", "") or ""
        message = getattr(resp, "errorMessage", "") or ""
        return f"{code} {message}".strip()

    def _sync_test_connection(self) -> bool:
        try:
            resp = self.client.listBuckets()
            if self._is_ok(resp):
                return True
            logger.warning(f"OBS 连接测试失败: {self._error_desc(resp)}")
            return False
        except Exception as e:
            logger.warning(f"OBS 连接测试失败: {e}")
            return False

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        try:
            resp = self.client.putFile(bucketName=self._require_bucket(), objectKey=remote_path, file_path=local_path)
            if not self._is_ok(resp):
                raise CustomException(msg=f"OBS 上传失败: {self._error_desc(resp)}")
        except CustomException:
            raise
        except Exception as e:
            raise CustomException(msg=f"OBS 上传失败: {e!s}")
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        try:
            resp = self.client.getObject(bucketName=self._require_bucket(), objectKey=remote_path, downloadPath=local_path)
            if not self._is_ok(resp):
                raise CustomException(msg=f"OBS 下载失败: {self._error_desc(resp)}")
        except CustomException:
            raise
        except Exception as e:
            raise CustomException(msg=f"OBS 下载失败: {e!s}")
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        try:
            resp = self.client.deleteObject(bucketName=self._require_bucket(), objectKey=remote_path)
            if not self._is_ok(resp):
                raise CustomException(msg=f"OBS 删除失败: {self._error_desc(resp)}")
        except CustomException:
            raise
        except Exception as e:
            raise CustomException(msg=f"OBS 删除失败: {e!s}")

    def _sync_exists(self, remote_path: str) -> bool:
        try:
            resp = self.client.getObjectMetadata(bucketName=self._require_bucket(), objectKey=remote_path)
            return self._is_ok(resp)
        except Exception:
            return False

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        try:
            resp = self.client.listObjects(bucketName=self._require_bucket(), prefix=prefix, delimiter="/")
        except Exception as e:
            raise CustomException(msg=f"OBS 列表失败: {e!s}")
        if not self._is_ok(resp):
            raise CustomException(msg=f"OBS 列表失败: {self._error_desc(resp)}")

        body = getattr(resp, "body", None)
        result: list[StorageObject] = []
        for common in getattr(body, "commonPrefixes", None) or []:
            raw_key = getattr(common, "prefix", "") or ""
            raw_key = raw_key.rstrip("/")
            result.append(StorageObject(name=raw_key.rsplit("/", 1)[-1], key=self._strip_prefix(raw_key), is_dir=True))
        for obj in getattr(body, "contents", None) or []:
            raw_key = getattr(obj, "key", "") or ""
            if raw_key == prefix:
                continue
            last_modified = getattr(obj, "lastModified", None)
            result.append(
                StorageObject(
                    name=raw_key.rsplit("/", 1)[-1],
                    key=self._strip_prefix(raw_key),
                    is_dir=False,
                    size=getattr(obj, "size", None),
                    modified_time=datetime.fromtimestamp(last_modified / 1000, tz=UTC) if last_modified else None,
                )
            )
        return result

    def _sync_get_url(self, remote_path: str, expire: int) -> str:
        try:
            resp = self.client.createSignedUrl("GET", bucketName=self._require_bucket(), objectKey=remote_path, expires=expire)
            return getattr(resp, "signedUrl", "")
        except Exception as e:
            raise CustomException(msg=f"OBS 生成预签名 URL 失败: {e!s}")

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

    async def get_url(self, remote_path: str, expire: int = 3600) -> str | None:
        full_key = self._join_key(remote_path)
        return await asyncio.to_thread(self._sync_get_url, full_key, expire)
