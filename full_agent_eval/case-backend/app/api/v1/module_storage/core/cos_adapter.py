import asyncio

from qcloud_cos import CosConfig, CosS3Client

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class CosStorageAdapter(BaseStorageAdapter):
    """腾讯云 COS 存储适配器（cos-python-sdk-v5，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.COS

    def __init__(self, config) -> None:
        super().__init__(config)
        if not self.config.region:
            raise CustomException(msg="COS 存储源必须配置 region")
        if not self.config.username or not self.config.password:
            raise CustomException(msg="COS 存储源必须配置 SecretId/SecretKey")
        cos_config = CosConfig(
            Region=self.config.region,
            SecretId=self.config.username or "",
            SecretKey=self.config.password or "",
            Scheme="https" if self.config.is_secure else "http",
        )
        self.client = CosS3Client(cos_config)

    def _require_bucket(self) -> str:
        if not self.config.bucket:
            raise CustomException(msg="存储源未配置 bucket")
        return self.config.bucket

    def _sync_test_connection(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except Exception as e:
            logger.warning(f"COS 连接测试失败: {e}")
            return False

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        try:
            self.client.upload_file(
                Bucket=self._require_bucket(),
                Key=remote_path,
                LocalFilePath=local_path,
                EnableMD5=False,
            )
        except Exception as e:
            raise CustomException(msg=f"COS 上传失败: {e!s}")
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        try:
            self.client.download_file(
                Bucket=self._require_bucket(),
                Key=remote_path,
                DestFilePath=local_path,
            )
        except Exception as e:
            raise CustomException(msg=f"COS 下载失败: {e!s}")
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        try:
            self.client.delete_object(Bucket=self._require_bucket(), Key=remote_path)
        except Exception as e:
            raise CustomException(msg=f"COS 删除失败: {e!s}")

    def _sync_exists(self, remote_path: str) -> bool:
        try:
            return self.client.object_exists(Bucket=self._require_bucket(), Key=remote_path)
        except Exception:
            return False

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        try:
            resp = self.client.list_objects(Bucket=self._require_bucket(), Prefix=prefix, Delimiter="/")
        except Exception as e:
            raise CustomException(msg=f"COS 列表失败: {e!s}")

        result: list[StorageObject] = []
        for common in resp.get("CommonPrefixes", []):
            raw_key = common.get("Prefix", "").rstrip("/")
            result.append(StorageObject(name=raw_key.rsplit("/", 1)[-1], key=self._strip_prefix(raw_key), is_dir=True))
        for obj in resp.get("Contents", []):
            raw_key = obj.get("Key", "")
            if raw_key == prefix:
                continue
            result.append(
                StorageObject(
                    name=raw_key.rsplit("/", 1)[-1],
                    key=self._strip_prefix(raw_key),
                    is_dir=False,
                    size=obj.get("Size"),
                    modified_time=obj.get("LastModified"),
                )
            )
        return result

    def _sync_get_url(self, remote_path: str, expire: int) -> str:
        try:
            return self.client.get_presigned_url(
                Method="GET",
                Bucket=self._require_bucket(),
                Key=remote_path,
                Expired=expire,
            )
        except Exception as e:
            raise CustomException(msg=f"COS 生成预签名 URL 失败: {e!s}")

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
