"""系统内部聊天 - 数据模型"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base_model import ModelMixin, UserMixin

if TYPE_CHECKING:
    from app.api.v1.module_system.user.model import UserModel


class ChatGroupModel(ModelMixin, UserMixin):
    """聊天群组"""

    __tablename__: str = "sys_chat_group"
    __table_args__: dict[str, str] = {"comment": "聊天群组表"}

    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="群名称")
    avatar: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="群头像URL")
    announcement: Mapped[str | None] = mapped_column(Text, nullable=True, comment="群公告")
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False, index=True, comment="群主ID"
    )
    owner: Mapped["UserModel"] = relationship(foreign_keys=[owner_id])


class ChatGroupMemberModel(ModelMixin):
    """聊天群组成员"""

    __tablename__: str = "sys_chat_group_member"
    __table_args__: tuple = (
        UniqueConstraint("group_id", "user_id", name="uq_chat_group_member"),
        {"comment": "聊天群组成员表"},
    )

    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_chat_group.id", ondelete="CASCADE"), nullable=False, index=True, comment="群ID"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False, index=True, comment="用户ID"
    )


class ChatMessageModel(ModelMixin):
    """聊天消息"""

    __tablename__: str = "sys_chat_message"
    __table_args__: tuple = (
        Index("ix_chat_message_conv_created", "conversation_type", "receiver_id", "created_time"),
        {"comment": "聊天消息表"},
    )

    conversation_type: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, index=True, comment="会话类型(1:私聊 2:群聊)"
    )
    sender_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False, index=True, comment="发送人ID"
    )
    receiver_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="接收人ID(私聊:对方用户,群聊:群ID)")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="消息状态(0:未读 1:已读,私聊使用)")

    sender: Mapped["UserModel"] = relationship(foreign_keys=[sender_id])


class ChatGroupReadModel(ModelMixin):
    """群聊已读位置"""

    __tablename__: str = "sys_chat_group_read"
    __table_args__: tuple = (
        UniqueConstraint("user_id", "group_id", name="uq_chat_group_read"),
        {"comment": "群聊已读位置表"},
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False, index=True, comment="用户ID"
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sys_chat_group.id", ondelete="CASCADE"), nullable=False, index=True, comment="群ID"
    )
    last_read_msg_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="最后已读消息ID")
