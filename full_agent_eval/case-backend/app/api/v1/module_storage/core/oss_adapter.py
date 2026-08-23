import asyncio
from datetime import UTC, datetime, timedelta

import alibabacloud_oss_v2 as oss

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class OssStorageAdapter(BaseStorageAdapter):
    """阿里云 OSS 存储适配器（alibabacloud_oss_v2 SDK，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.OSS

    def __init__(self, config) -> None:
        super().__init__(config)
        if not self.config.endpoint:
            raise CustomException(msg="OSS 存储源必须配置 endpoint")
        if not self.config.region:
            raise CustomException(msg="OSS 存储源必须配置 region（V4 签名要求，如 cn-hangzhou）")
        if not self.config.username or not self.config.password:
            raise CustomException(msg="OSS 存储源必须配置 AccessKeyId 与 AccessKeySecret")
        self.bucket_name = self.config.bucket or ""
        cfg = oss.config.load_default()
        cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(self.config.username, self.config.password)
        cfg.region = self.config.region
        cfg.endpoint = self.config.endpoint
        self.client = oss.Client(cfg)

    def _sync_test_connection(self) -> bool:
        try:
            self.client.get_bucket_info(oss.GetBucketInfoRequest(bucket=self.bucket_name))
            return True
        except oss.exceptions.ServiceError as e:
            logger.warning(f"OSS 连接测试失败: {e.code} {e.message}")
            return False
        except Exception as e:
            logger.warning(f"OSS 连接测试失败: {e}")
            return False

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        try:
            self.client.put_object_from_file(
                oss.PutObjectRequest(bucket=self.bucket_name, key=remote_path),
                local_path,
            )
        except Exception as e:
            raise CustomException(msg=f"OSS 上传失败: {e!s}")
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        try:
            self.client.get_object_to_file(
                oss.GetObjectRequest(bucket=self.bucket_name, key=remote_path),
                local_path,
            )
        except Exception as e:
            raise CustomException(msg=f"OSS 下载失败: {e!s}")
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        try:
            self.client.delete_object(oss.DeleteObjectRequest(bucket=self.bucket_name, key=remote_path))
        except Exception as e:
            raise CustomException(msg=f"OSS 删除失败: {e!s}")

    def _sync_exists(self, remote_path: str) -> bool:
        try:
            self.client.head_object(oss.HeadObjectRequest(bucket=self.bucket_name, key=remote_path))
            return True
        except oss.exceptions.ServiceError as e:
            if e.status_code == 404:
                return False
            logger.warning(f"OSS 判断文件存在失败: {e.code} {e.message}")
            return False
        except Exception:
            return False

    @staticmethod
    def _to_utc_dt(value: int | float | datetime | None) -> datetime | None:
        """兼容 SDK 返回的时间戳（int/float）与 datetime 两种类型。"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return datetime.fromtimestamp(value, tz=UTC)

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        try:
            paginator = self.client.list_objects_v2_paginator()
            result: list[StorageObject] = []
            for page in paginator.iter_page(oss.ListObjectsV2Request(bucket=self.bucket_name, prefix=prefix, delimiter="/")):
                for common in page.common_prefixes or []:
                    raw_key = (common.prefix or "").rstrip("/")
                    result.append(StorageObject(name=raw_key.rsplit("/", 1)[-1], key=self._strip_prefix(raw_key), is_dir=True))
                for obj in page.contents or []:
                    raw_key = obj.key or ""
                    if raw_key == prefix:
                        continue
                    result.append(
                        StorageObject(
                            name=raw_key.rsplit("/", 1)[-1],
                            key=self._strip_prefix(raw_key),
                            is_dir=False,
                            size=obj.size,
                            modified_time=self._to_utc_dt(obj.last_modified),
                        )
                    )
            return result
        except Exception as e:
            raise CustomException(msg=f"OSS 列表失败: {e!s}")

    def _sync_get_url(self, remote_path: str, expire: int) -> str | None:
        try:
            result = self.client.presign(
                oss.GetObjectRequest(bucket=self.bucket_name, key=remote_path),
                expires=timedelta(seconds=expire),
            )
            return result.url
        except Exception as e:
            raise CustomException(msg=f"OSS 生成预签名 URL 失败: {e!s}")

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
