from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from app.api.v1.module_storage.core.constants import StorageProtocol


class StorageAdapterConfig(BaseModel):
    """存储适配器配置（从 StorageSourceModel 剥离加密字段后注入，解耦 ORM 与协议层）"""

    protocol: StorageProtocol = Field(description="存储协议")
    host: str = Field(description="主机地址")
    port: int = Field(description="端口")
    username: str | None = Field(default=None, description="用户名/AccessKey")
    password: str | None = Field(default=None, description="密码/SecretKey（已解密）")
    bucket: str | None = Field(default=None, description="桶名（对象存储）或根目录（FTP/SFTP）")
    endpoint: str | None = Field(default=None, description="接入点（对象存储）")
    region: str | None = Field(default=None, description="区域（对象存储）")
    path_prefix: str | None = Field(default=None, description="统一路径前缀")
    is_secure: bool = Field(default=False, description="是否启用 TLS（FTPS）")
    implicit_tls: bool = Field(default=False, description="FTPS 是否隐式 TLS（默认显式）")

    @property
    def full_prefix(self) -> str:
        """规范化后的完整路径前缀（去除首尾斜杠）。"""
        prefix = self.path_prefix or ""
        return prefix.strip("/")


class StorageObject(BaseModel):
    """远端文件对象信息"""

    name: str = Field(description="文件/目录名")
    key: str = Field(description="相对路径（含前缀时自动剥离）")
    is_dir: bool = Field(default=False, description="是否目录")
    size: int | None = Field(default=None, description="大小（字节）")
    modified_time: datetime | None = Field(default=None, description="修改时间")


class BaseStorageAdapter(ABC):
    """存储协议适配器抽象基类

    所有适配器均在事件循环中通过 ``asyncio.to_thread`` 包装同步 SDK 调用，
    避免阻塞事件循环。适配器实例按请求创建（无连接池复用），用完由调用方关闭。
    """

    def __init__(self, config: StorageAdapterConfig) -> None:
        self.config = config

    def _join_key(self, remote_path: str) -> str:
        """将用户传入的远端相对路径拼接路径前缀，得到协议层完整 key。"""
        remote_path = remote_path.strip("/")
        if self.config.full_prefix:
            return f"{self.config.full_prefix}/{remote_path}"
        return remote_path

    def _strip_prefix(self, key: str) -> str:
        """从协议层完整 key 剥离路径前缀，返回用户可见的相对路径。"""
        prefix = self.config.full_prefix
        if prefix and key.startswith(f"{prefix}/"):
            return key[len(prefix) + 1 :]
        return key

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试连接是否可用。"""

    @abstractmethod
    async def upload(self, local_path: str, remote_path: str) -> str:
        """上传本地文件到远端，返回远端完整 key。"""

    @abstractmethod
    async def download(self, remote_path: str, local_path: str) -> str:
        """下载远端文件到本地，返回本地路径。"""

    @abstractmethod
    async def delete(self, remote_path: str) -> None:
        """删除远端文件（目录递归删除由协议层自行处理）。"""

    @abstractmethod
    async def exists(self, remote_path: str) -> bool:
        """判断远端文件是否存在。"""

    @abstractmethod
    async def list(self, prefix: str = "") -> list[StorageObject]:
        """列出远端目录下的文件与目录（不含前缀）。"""

    async def get_url(self, remote_path: str, expire: int = 3600) -> str | None:
        """获取访问 URL（对象存储返回预签名 URL；FTP/SFTP 不支持返回 None）。"""
        return None

    async def close(self) -> None:  # noqa: B027 - 可选钩子，无连接池的协议实现为空操作
        """释放连接资源。"""
