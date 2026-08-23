import asyncio
import ftplib
import socket
import ssl
from datetime import datetime

from app.api.v1.module_storage.core.base import BaseStorageAdapter, StorageObject
from app.api.v1.module_storage.core.constants import StorageProtocol
from app.core.exceptions import CustomException
from app.core.logger import logger


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """隐式 FTPS（默认端口 990）连接子类：socket 直连即套 TLS。"""

    def connect(self, host: str = "", port: int = 0, timeout: int = -999, source_address=None) -> str:
        if host != "":
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        context = self.context
        if context is None:
            # FTP_TLS 默认 context 未设置时，构造宽松校验的客户端上下文（兼容自签名证书）
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.sock = context.wrap_socket(
            socket.create_connection((self.host, self.port), self.timeout, self.source_address),
            server_hostname=self.host,
        )
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class FtpStorageAdapter(BaseStorageAdapter):
    """FTP / FTPS 存储适配器（ftplib 标准库，同步调用经 asyncio.to_thread 包装）。"""

    protocol = StorageProtocol.FTP

    def _new_client(self) -> ftplib.FTP_TLS | ftplib.FTP:
        """建立连接并登录。FTP 明文 / FTPS（显式或隐式 TLS）按配置选择。"""
        if self.config.protocol == StorageProtocol.FTPS:
            client = _ImplicitFTP_TLS() if self.config.implicit_tls else ftplib.FTP_TLS()
        else:
            client = ftplib.FTP()
        client.connect(host=self.config.host, port=self.config.port, timeout=30)
        if isinstance(client, ftplib.FTP_TLS):
            if not self.config.implicit_tls:
                client.auth()  # 显式 FTPS：升级 TLS 通道
        client.login(user=self.config.username or "", passwd=self.config.password or "")
        if isinstance(client, ftplib.FTP_TLS):
            client.prot_p()  # 数据通道加密
        return client

    @staticmethod
    def _close_client(client: ftplib.FTP) -> None:
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass

    def _sync_test_connection(self) -> bool:
        client = self._new_client()
        try:
            client.pwd()
            return True
        except Exception as e:
            logger.warning(f"FTP 连接测试失败: {e}")
            return False
        finally:
            self._close_client(client)

    def _sync_upload(self, local_path: str, remote_path: str) -> str:
        client = self._new_client()
        try:
            with open(local_path, "rb") as f:
                client.storbinary(f"STOR {remote_path}", f)
        except Exception as e:
            raise CustomException(msg=f"FTP 上传失败: {e!s}")
        finally:
            self._close_client(client)
        return remote_path

    def _sync_download(self, remote_path: str, local_path: str) -> str:
        client = self._new_client()
        try:
            with open(local_path, "wb") as f:
                client.retrbinary(f"RETR {remote_path}", f.write)
        except Exception as e:
            raise CustomException(msg=f"FTP 下载失败: {e!s}")
        finally:
            self._close_client(client)
        return local_path

    def _sync_delete(self, remote_path: str) -> None:
        client = self._new_client()
        try:
            client.delete(remote_path)
        except Exception as e:
            raise CustomException(msg=f"FTP 删除失败: {e!s}")
        finally:
            self._close_client(client)

    def _sync_exists(self, remote_path: str) -> bool:
        client = self._new_client()
        try:
            try:
                client.size(remote_path)
                return True
            except ftplib.error_perm:
                # 部分服务器不支持 SIZE，退化为 NLST 判断
                try:
                    client.nlst(remote_path)
                    return True
                except ftplib.error_perm:
                    return False
        except Exception:
            return False
        finally:
            self._close_client(client)

    def _sync_list(self, prefix: str) -> list[StorageObject]:
        client = self._new_client()
        try:
            entries = list(client.mlsd(prefix))
            result: list[StorageObject] = []
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                modified_time = None
                raw_mtime = facts.get("modify")
                if raw_mtime:
                    try:
                        modified_time = datetime.strptime(raw_mtime, "%Y%m%d%H%M%S")
                    except ValueError:
                        modified_time = None
                result.append(
                    StorageObject(
                        name=name,
                        key=self._strip_prefix(f"{prefix}/{name}".strip("/")) if prefix else name,
                        is_dir=facts.get("type") == "dir",
                        size=int(facts["size"]) if facts.get("size") else None,
                        modified_time=modified_time,
                    )
                )
            return result
        except Exception as e:
            raise CustomException(msg=f"FTP 列表失败: {e!s}")
        finally:
            self._close_client(client)

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
