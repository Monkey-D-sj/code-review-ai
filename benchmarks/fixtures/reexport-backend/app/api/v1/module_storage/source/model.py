from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import ModelMixin, UserMixin


class StorageSourceModel(ModelMixin, UserMixin):
    """存储源配置模型"""

    __tablename__: str = "storage_source"
    __table_args__: dict[str, str] = {"comment": "存储源配置表"}

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True, comment="存储源名称")
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, index=True, comment="协议(ftp/ftps/sftp/s3/obs/oss/cos/local)")
    host: Mapped[str] = mapped_column(String(255), nullable=False, comment="主机地址")
    port: Mapped[int] = mapped_column(Integer, nullable=False, comment="端口")
    username: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True, comment="用户名/AccessKey")
    password: Mapped[str | None] = mapped_column(Text, default=None, nullable=True, comment="密码/SecretKey(Fernet加密)")
    bucket: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True, comment="桶名/根目录")
    endpoint: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True, comment="接入点(对象存储)")
    region: Mapped[str | None] = mapped_column(String(64), default=None, nullable=True, comment="区域(对象存储)")
    path_prefix: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True, comment="统一路径前缀")
    is_secure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否启用TLS(FTPS)")
    implicit_tls: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="FTPS是否隐式TLS(默认显式)")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否默认存储源")
    status: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="状态(0:启用 1:停用)")
    description: Mapped[str | None] = mapped_column(Text, default=None, nullable=True, comment="备注")
