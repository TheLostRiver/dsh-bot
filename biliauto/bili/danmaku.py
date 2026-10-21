"""直播间事件流：把 B 站推送的原始 JSON 解析成统一事件。

B 站推送的 DANMU_MSG 结构是出了名的不稳定（字段位置会变），
所以这里全部用"取不到就给默认值"的写法，绝不让一条脏数据打崩整场直播。
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bilibili_api.live import LiveDanmaku

log = logging.getLogger("biliauto.danmaku")

Handler = Callable[["LiveEvent"], Awaitable[None]]


@dataclass
class LiveEvent:
    kind: str
    uid: int
    uname: str
    text: str
    ts: float = field(default_factory=time.time)
    medal_name: str = ""
    medal_level: int = 0
    user_level: int = 0
    is_admin: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.kind.startswith("SUPER_CHAT") or self.kind == "GUARD_BUY"


def _dig(obj: Any, *path: Any, default: Any = None) -> Any:
    cur = obj
    for key in path:
        try:
            if isinstance(cur, (list, tuple)):
                cur = cur[key]
            elif isinstance(cur, dict):
                cur = cur[key]
            else:
                return default
        except (KeyError, IndexError, TypeError):
            return default
    return default if cur is None else cur


def parse_event(kind: str, data: dict) -> LiveEvent | None:
    if kind == "DANMU_MSG":
        info = data.get("info") or []
        text = str(_dig(info, 1, default="") or "").strip()
        if not text:
            return None
        uid_raw = _dig(info, 2, 0, default=0)
        uname = str(_dig(info, 2, 1, default="观众") or "观众")
        medal = data.get("fans_medal") or {}
        medal_name = str(medal.get("medal_name") or _dig(info, 3, 3, default="") or "")
        medal_level = int(medal.get("medal_level") or _dig(info, 3, 0, default=0) or 0)
        try:
            uid = int(uid_raw)
        except (TypeError, ValueError):
            uid = 0
        return LiveEvent(
            kind=kind,
            uid=uid,
            uname=uname,
            text=text,
            medal_name=medal_name,
            medal_level=medal_level,
            user_level=0,
            is_admin=bool(_dig(info, 2, 2, default=0)),
            raw=data,
        )

    if kind.startswith("SUPER_CHAT"):
        payload = data.get("data") or {}
        user_info = payload.get("user_info") or {}
        text = str(payload.get("message") or "").strip()
        if not text:
            return None
        return LiveEvent(
            kind="SUPER_CHAT_MESSAGE",
            uid=int(payload.get("uid") or 0),
            uname=str(user_info.get("uname") or "老板"),
            text=text,
            raw=data,
        )

    if kind == "GUARD_BUY":
        payload = data.get("data") or {}
        gift = str(payload.get("gift_name") or "舰长")
        uname = str(payload.get("username") or "观众")
        return LiveEvent(
            kind=kind,
            uid=int(payload.get("uid") or 0),
            uname=uname,
            text=f"开通了{gift}",
            raw=data,
        )

    return None


class LiveClient:
    """包一层 LiveDanmaku：负责订阅、解析、断线重连后的重新挂载。"""

    # 只订阅真正会影响"要不要聊"的事件，减少无谓处理
    EVENTS = (
        "DANMU_MSG",
        "SUPER_CHAT_MESSAGE",
        "SUPER_CHAT_MESSAGE_JPN",
        "GUARD_BUY",
    )

    def __init__(self, room_id: int, credential=None, handler: Handler | None = None):
        self.room_id = room_id
        self.handler = handler
        self._danmaku = LiveDanmaku(room_id, credential=credential)
        for name in self.EVENTS:
            self._danmaku.add_event_listener(name, self._make_handler(name))

    def _make_handler(self, kind: str):
        async def _on(callback_info: dict) -> None:
            try:
                event = parse_event(kind, callback_info.get("data") or {})
            except Exception as e:  # 脏数据不能让推送协程崩
                log.debug("解析 %s 失败：%s", kind, e)
                return
            if event is not None and self.handler is not None:
                await self.handler(event)

        return _on

    async def run(self) -> None:
        log.info("正在连接直播间 %s …", self.room_id)
        await self._danmaku.connect()

    async def stop(self) -> None:
        try:
            result = self._danmaku.disconnect()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log.debug("断开连接时出错（可忽略）：%s", e)
