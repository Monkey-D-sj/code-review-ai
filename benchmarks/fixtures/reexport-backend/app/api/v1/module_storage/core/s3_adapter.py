import asyncio

import boto3
from botocore.exceptions import ClientError

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class S3StorageAdapter(BaseStorageAdapter):
    """S3 兼容对象存储适配器（boto3，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.S3

    def __init__(self, config) -> None:
        super().__init__(config)
        # 凭据为空时传空串而非 None，避免 boto3 走 EC2 实例元数据(IMDS)探测导致超时
        self.client = boto3.client(
            "s3",
            endpoint_url=self.config.endpoint,
            region_name=self.config.region,
            aws_access_key_id=self.config.username or "",
            aws_secret_access_key=self.config.password or "",
        )

    def _require_bucket(self) -> str:
        if not self.config.bucket:
            raise CustomException(msg="存储源未配置 bucket")
        return self.config.bucket

    def _sync_test_connection(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self._require_bucket())
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            # 403 表示凭据有效但无权限查看桶，连接本身是通的
            if code == "403":
                return True
            logger.warning(f"S3 连接测试失败: {code} {e}")
            return False
        except Exception as e:
            logger.warning(f"S3 连接测试失败: {e}")
            return False

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        try:
            self.client.upload_file(local_path, self._require_bucket(), remote_path)
        except Exception as e:
            raise CustomException(msg=f"S3 上传失败: {e!s}")
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        try:
            self.client.download_file(self._require_bucket(), remote_path, local_path)
        except Exception as e:
            raise CustomException(msg=f"S3 下载失败: {e!s}")
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        try:
            self.client.delete_object(Bucket=self._require_bucket(), Key=remote_path)
        except Exception as e:
            raise CustomException(msg=f"S3 删除失败: {e!s}")

    def _sync_exists(self, remote_path: str) -> bool:
        try:
            self.client.head_object(Bucket=self._require_bucket(), Key=remote_path)
            return True
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            logger.warning(f"S3 head_object 失败: {e}")
            return False
        except Exception:
            return False

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        try:
            resp = self.client.list_objects_v2(Bucket=self._require_bucket(), Prefix=prefix, Delimiter="/")
        except Exception as e:
            raise CustomException(msg=f"S3 列表失败: {e!s}")

        result: list[StorageObject] = []
        for cp in resp.get("CommonPrefixes", []):
            raw_key = cp.get("Prefix", "").rstrip("/")
            name = raw_key.rsplit("/", 1)[-1]
            result.append(StorageObject(name=name, key=self._strip_prefix(raw_key), is_dir=True))
        for obj in resp.get("Contents", []):
            raw_key = obj.get("Key", "")
            if raw_key == prefix:
                continue
            name = raw_key.rsplit("/", 1)[-1]
            result.append(
                StorageObject(
                    name=name,
                    key=self._strip_prefix(raw_key),
                    is_dir=False,
                    size=obj.get("Size"),
                    modified_time=obj.get("LastModified"),
                )
            )
        return result

    def _sync_get_url(self, remote_path: str, expire: int) -> str:
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._require_bucket(), "Key": remote_path},
                ExpiresIn=expire,
            )
        except Exception as e:
            raise CustomException(msg=f"S3 生成预签名 URL 失败: {e!s}")

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

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
