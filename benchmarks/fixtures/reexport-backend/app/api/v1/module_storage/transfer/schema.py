from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.base_schema import BaseQueryParam, BaseSchema

TransferStatus = Literal["pending", "running", "success", "failed", "canceled"]
TransferTaskType = Literal["parallel", "chain"]
TransferSourceType = Literal["local", "remote"]


class TransferTargetSchema(BaseModel):
    """传输目标配置"""

    target_id: int = Field(..., ge=1, description="目标存储源ID")
    target_path: str = Field(..., min_length=1, max_length=1024, description="目标路径")


class TransferTaskCreateSchema(BaseModel):
    """创建传输任务（远端源，JSON 提交）"""

    name: str = Field(..., min_length=1, max_length=128, description="任务名称")
    task_type: TransferTaskType = Field(..., description="任务类型(parallel:多目标 chain:链式)")
    source_type: TransferSourceType = Field("remote", description="源类型(local:本地 remote:远端)")
    source_id: int | None = Field(default=None, ge=1, description="源存储源ID(remote 必填)")
    source_path: str | None = Field(default=None, min_length=1, max_length=1024, description="源远端路径(remote 必填)")
    targets: list[TransferTargetSchema] = Field(..., min_length=1, description="目标列表(顺序即链式执行顺序)")

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_type == "remote" and not self.source_id:
            raise ValueError("远端源必须指定源存储源 source_id")
        if self.source_type == "remote" and not self.source_path:
            raise ValueError("远端源必须指定源路径 source_path")
        return self


class TransferStepOutSchema(BaseSchema):
    """传输步骤详情"""

    model_config = ConfigDict(from_attributes=True)

    task_id: int = Field(description="任务ID")
    step_order: int = Field(description="步骤序号")
    source_id: int | None = Field(default=None, description="源存储源ID")
    source_path: str | None = Field(default=None, description="源路径")
    target_id: int = Field(description="目标存储源ID")
    target_path: str = Field(description="目标路径")
    status: TransferStatus = Field(description="状态")
    progress: int = Field(default=0, description="进度(0-100)")
    speed: float = Field(default=0.0, description="速度(B/s)")
    total_size: int = Field(default=0, description="本步总字节")
    transferred_size: int = Field(default=0, description="本步已传输字节")
    error_msg: str | None = Field(default=None, description="错误信息")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")


class TransferTaskOutSchema(BaseSchema):
    """传输任务详情"""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(description="任务名称")
    task_type: TransferTaskType = Field(description="任务类型")
    source_type: TransferSourceType = Field(description="源类型")
    source_id: int | None = Field(default=None, description="源存储源ID")
    source_path: str | None = Field(default=None, description="源远端路径")
    source_name: str | None = Field(default=None, description="源文件名")
    source_size: int | None = Field(default=None, description="源文件大小")
    status: TransferStatus = Field(description="状态")
    total_size: int = Field(default=0, description="总字节")
    transferred_size: int = Field(default=0, description="已传输字节")
    progress: int = Field(default=0, description="进度(0-100)")
    speed: float = Field(default=0.0, description="实时速度(B/s)")
    error_msg: str | None = Field(default=None, description="错误信息")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")
    steps: list[TransferStepOutSchema] = Field(default_factory=list, description="传输步骤")


class TransferTaskQueryParam(BaseQueryParam):
    """传输任务查询参数"""

    name: str | None = Field(None, description="任务名称", json_schema_extra={"q": "like"})
    task_type: TransferTaskType | None = Field(None, description="任务类型", json_schema_extra={"q": "eq"})
    status: TransferStatus | None = Field(None, description="状态", json_schema_extra={"q": "eq"})
