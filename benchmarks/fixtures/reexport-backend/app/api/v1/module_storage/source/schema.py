from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.v1.module_storage.core.constants import DEFAULT_PORTS, StorageProtocol
from app.core.base_schema import BaseQueryParam, BaseSchema, UserByQueryParam, UserBySchema


class StorageSourceConfigSchema(BaseModel):
    """存储源连接配置模型（创建/测试共用）"""

    protocol: StorageProtocol = Field(..., description="协议(ftp/ftps/sftp/s3/obs/oss/cos/local)")
    host: str = Field(..., min_length=1, max_length=255, description="主机地址/根目录")
    port: int | None = Field(default=None, ge=0, le=65535, description="端口(local 协议为0；不传则使用协议默认端口)")
    username: str | None = Field(default=None, max_length=255, description="用户名/AccessKey")
    password: str | None = Field(default=None, max_length=512, description="密码/SecretKey")
    bucket: str | None = Field(default=None, max_length=255, description="桶名/根目录")
    endpoint: str | None = Field(default=None, max_length=255, description="接入点(对象存储)")
    region: str | None = Field(default=None, max_length=64, description="区域(对象存储)")
    path_prefix: str | None = Field(default=None, max_length=255, description="统一路径前缀")
    is_secure: bool = Field(default=False, description="是否启用TLS(FTPS)")
    implicit_tls: bool = Field(default=False, description="FTPS是否隐式TLS(默认显式)")

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: str | None) -> str | None:
        if value:
            value = value.strip().strip("/")
            if ".." in value or "\x00" in value:
                raise ValueError("路径前缀包含非法字符")
        return value

    @model_validator(mode="after")
    def validate_protocol_fields(self):
        """按协议校验必填字段并填充默认端口。"""
        if self.port is None:
            self.port = DEFAULT_PORTS[self.protocol]
        # 对象存储类协议必须配置桶/空间名
        obj_store_protocols = (
            StorageProtocol.S3,
            StorageProtocol.OBS,
            StorageProtocol.OSS,
            StorageProtocol.COS,
        )
        if self.protocol in obj_store_protocols and not self.bucket:
            raise ValueError(f"{self.protocol.value} 协议必须配置 bucket")
        # endpoint 必须配置的协议
        if self.protocol in (StorageProtocol.S3, StorageProtocol.OBS, StorageProtocol.OSS) and not self.endpoint:
            raise ValueError(f"{self.protocol.value} 协议必须配置 endpoint")
        if self.protocol == StorageProtocol.COS and not self.region:
            raise ValueError("cos 协议必须配置 region")
        if self.protocol == StorageProtocol.FTPS and self.is_secure is False:
            # FTPS 默认开启 TLS
            self.is_secure = True
        return self


class StorageSourceCreateSchema(StorageSourceConfigSchema):
    """存储源创建模型"""

    name: str = Field(..., min_length=1, max_length=64, description="存储源名称")
    is_default: bool = Field(default=False, description="是否默认存储源")
    status: int = Field(default=0, ge=0, le=1, description="状态(0:启用 1:停用)")
    description: str | None = Field(default=None, max_length=255, description="备注")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("存储源名称不能为空")
        return value


class StorageSourceUpdateSchema(StorageSourceCreateSchema):
    """存储源更新模型（password 为空表示不修改原密码）"""


class StorageSourceTestSchema(StorageSourceConfigSchema):
    """存储源连接测试模型（仅校验连接配置，不落库；密码留空且传 source_id 时回退已保存密码）"""

    source_id: int | None = Field(default=None, ge=1, description="已保存的存储源ID(编辑态测试时使用)")


class StorageSourceOutSchema(StorageSourceCreateSchema, BaseSchema, UserBySchema):
    """存储源详情响应模型（密码永不明文返回）"""

    model_config = ConfigDict(from_attributes=True)

    password: None = Field(default=None, exclude=True, repr=False, description="密码(不返回)")
    has_password: bool = Field(default=False, description="是否已配置密码")

    @field_validator("password", mode="before")
    @classmethod
    def mask_password(cls, value) -> None:
        return None


class StorageSourceQueryParam(BaseQueryParam, UserByQueryParam):
    """存储源管理查询参数"""

    name: str | None = Field(None, description="存储源名称", json_schema_extra={"q": "like"})
    protocol: StorageProtocol | None = Field(None, description="协议", json_schema_extra={"q": "eq"})
    status: int | None = Field(None, ge=0, le=1, description="状态(0:启用 1:停用)", json_schema_extra={"q": "eq"})
