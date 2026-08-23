"""传输任务 WebSocket 连接管理（单实例部署，按用户推送任务进度）"""

from fastapi import WebSocket


class TransferWSManager:
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

    async def send_to_user(self, user_id: int | None, data: dict) -> None:
        if user_id is None:
            return
        for ws in list(self._connections.get(user_id, ())):
            try:
                await ws.send_json(data)
            except Exception:
                pass


transfer_ws_manager = TransferWSManager()
