"""聊天 WebSocket 连接管理（单实例部署）"""

from fastapi import WebSocket


class ChatWSManager:
    """维护 user_id -> 连接集合，支持多标签页连接"""

    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(user_id, set()).add(ws)

    def disconnect(self, user_id: int, ws: WebSocket) -> None:
        conns = self._connections.get(user_id)
        if conns is None:
            return
        conns.discard(ws)
        if not conns:
            self._connections.pop(user_id, None)

    def is_online(self, user_id: int) -> bool:
        return user_id in self._connections

    def online_count(self) -> int:
        return len(self._connections)

    async def send_to_user(self, user_id: int, data: dict) -> None:
        for ws in list(self._connections.get(user_id, ())):
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def send_to_users(self, user_ids: list[int], data: dict) -> None:
        for uid in set(user_ids):
            await self.send_to_user(uid, data)

    async def broadcast_presence(self, user_id: int, online: bool) -> None:
        """向所有在线用户广播某用户的上线/离线状态"""
        data = {"type": "presence", "user_id": user_id, "online": online}
        for ws in list(self._all_connections()):
            try:
                await ws.send_json(data)
            except Exception:
                pass

    def _all_connections(self):
        for conns in self._connections.values():
            yield from conns


chat_ws_manager = ChatWSManager()
