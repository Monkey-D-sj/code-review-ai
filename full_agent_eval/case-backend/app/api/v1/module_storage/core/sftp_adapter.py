import asyncio
from datetime import UTC, datetime

import paramiko

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class SftpStorageAdapter(BaseStorageAdapter):
    """SFTP 存储适配器（paramiko，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.SFTP

    def _new_client(self) -> paramiko.SFTPClient:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.username or "",
            password=self.config.password or "",
            timeout=30,
            banner_timeout=30,
        )
        return ssh.open_sftp()

    @staticmethod
    def _ensure_remote_dir(client: paramiko.SFTPClient, remote_dir: str) -> None:
        """递归创建远端目录（mkdir -p）。"""
        parts = [p for p in remote_dir.split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            try:
                client.stat(current)
            except FileNotFoundError:
                client.mkdir(current)
            except OSError:
                pass

    def _sync_test_connection(self) -> bool:
        try:
            client = self._new_client()
            client.listdir(".")
            client.close()
            return True
        except Exception as e:
            logger.warning(f"SFTP 连接测试失败: {e}")
            return False

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        client = self._new_client()
        try:
            dir_part, _ = remote_path.rsplit("/", 1) if "/" in remote_path else ("", remote_path)
            if dir_part:
                self._ensure_remote_dir(client, dir_part)
            client.put(local_path, remote_path)
        except Exception as e:
            raise CustomException(msg=f"SFTP 上传失败: {e!s}")
        finally:
            client.close()
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        client = self._new_client()
        try:
            client.get(remote_path, local_path)
        except Exception as e:
            raise CustomException(msg=f"SFTP 下载失败: {e!s}")
        finally:
            client.close()
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        client = self._new_client()
        try:
            client.remove(remote_path)
        except Exception as e:
            raise CustomException(msg=f"SFTP 删除失败: {e!s}")
        finally:
            client.close()

    def _sync_exists(self, remote_path: str) -> bool:
        try:
            client = self._new_client()
            try:
                client.stat(remote_path)
                return True
            except FileNotFoundError:
                return False
            finally:
                client.close()
        except Exception:
            return False

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        client = self._new_client()
        try:
            attrs = client.listdir_attr(prefix)
            result: list[StorageObject] = []
            for attr in attrs:
                result.append(
                    StorageObject(
                        name=attr.filename,
                        key=self._strip_prefix(f"{prefix}/{attr.filename}".strip("/")) if prefix else attr.filename,
                        is_dir=bool(attr.st_mode and (attr.st_mode & 0o40000)),
                        size=attr.st_size,
                        modified_time=datetime.fromtimestamp(attr.st_mtime, tz=UTC) if attr.st_mtime else None,
                    )
                )
            return result
        except Exception as e:
            raise CustomException(msg=f"SFTP 列表失败: {e!s}")
        finally:
            client.close()

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
