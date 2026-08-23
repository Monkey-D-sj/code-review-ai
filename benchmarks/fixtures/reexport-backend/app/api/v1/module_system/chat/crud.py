from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_crud import CRUDBase
from app.core.base_schema import AuthSchema

from .model import ChatGroupMemberModel, ChatGroupModel, ChatGroupReadModel, ChatMessageModel
from .schema import ChatGroupCreateSchema, ChatGroupUpdateSchema, ChatMessageCreateSchema


class ChatMessageCRUD(CRUDBase[ChatMessageModel, ChatMessageCreateSchema, ChatMessageCreateSchema]):
    """聊天消息数据层"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        super().__init__(model=ChatMessageModel, auth=auth, db=db)


class ChatGroupCRUD(CRUDBase[ChatGroupModel, ChatGroupCreateSchema, ChatGroupUpdateSchema]):
    """聊天群组数据层"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        super().__init__(model=ChatGroupModel, auth=auth, db=db)


class ChatGroupMemberCRUD(CRUDBase[ChatGroupMemberModel, None, None]):
    """群成员数据层"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        super().__init__(model=ChatGroupMemberModel, auth=auth, db=db)


class ChatGroupReadCRUD(CRUDBase[ChatGroupReadModel, None, None]):
    """群已读位置数据层"""

    def __init__(self, auth: AuthSchema, db: AsyncSession) -> None:
        super().__init__(model=ChatGroupReadModel, auth=auth, db=db)
