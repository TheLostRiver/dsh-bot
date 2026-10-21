"""评论区采集。

实测要点（都是踩出来的）：
1. **读接口也必须带凭据**。不带 cookie 拉 UP 视频列表会直接吃 412 风控页。
2. 视频列表在 `data.list.vlist`，每条有 aid / bvid / title。
3. 评论按时间倒序（OrderType.TIME）拿第一页即可，新评论都在最前面。
4. `parent == 0 and root == 0` 才是顶层评论 —— 只回这种，不卷入楼中楼。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bilibili_api import comment, user
from bilibili_api import live as bili_live

log = logging.getLogger("biliauto.comments")


class CollectorError(RuntimeError):
    """采集失败。code=-412 表示被风控，调用方应该退避。"""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


@dataclass
class VideoRef:
    aid: int
    bvid: str
    title: str


@dataclass
class CommentEvent:
    rpid: int
    oid: int
    uid: int
    uname: str
    text: str
    ctime: float
    is_top_level: bool
    reply_count: int
    video_title: str = ""

    @property
    def age_hours(self) -> float:
        import time

        return (time.time() - self.ctime) / 3600


class Collector:
    def __init__(self, credential) -> None:
        self.credential = credential

    # ---------- 主播 ----------
    async def resolve_anchor(self, room_id: int) -> tuple[int, str]:
        """从直播间房号反查主播 UID 与昵称，省得再让用户填一次。"""
        try:
            info = await bili_live.LiveRoom(room_id).get_room_info()
        except Exception as e:
            raise CollectorError(f"取直播间信息失败：{e}")

        uid = ((info.get("room_info") or {}).get("uid")) or 0
        name = (
            ((info.get("anchor_info") or {}).get("base_info") or {}).get("uname") or ""
        )
        if not uid:
            raise CollectorError("直播间信息里没有主播 UID")
        return int(uid), str(name)

    # ---------- 视频 ----------
    async def videos(self, mid: int, limit: int = 5) -> list[VideoRef]:
        try:
            raw = await user.User(mid, credential=self.credential).get_videos(ps=limit)
        except Exception as e:
            raise self._wrap(e, "取 UP 主视频列表失败")

        vlist = ((raw.get("list") or {}).get("vlist")) or raw.get("vlist") or []
        out: list[VideoRef] = []
        for v in vlist[:limit]:
            aid = v.get("aid")
            if not aid:
                continue
            out.append(
                VideoRef(
                    aid=int(aid),
                    bvid=str(v.get("bvid") or ""),
                    title=str(v.get("title") or ""),
                )
            )
        return out

    # ---------- 评论 ----------
    async def comments(self, video: VideoRef, page: int = 1) -> list[CommentEvent]:
        try:
            raw = await comment.get_comments(
                video.aid,
                comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.TIME,
                credential=self.credential,
            )
        except Exception as e:
            raise self._wrap(e, f"取评论失败 aid={video.aid}")

        out: list[CommentEvent] = []
        for r in raw.get("replies") or []:
            try:
                rpid = int(r.get("rpid") or 0)
                if not rpid:
                    continue
                member = r.get("member") or {}
                content = r.get("content") or {}
                out.append(
                    CommentEvent(
                        rpid=rpid,
                        oid=video.aid,
                        uid=int(r.get("mid") or 0),
                        uname=str(member.get("uname") or "观众"),
                        text=str(content.get("message") or "").strip(),
                        ctime=float(r.get("ctime") or 0),
                        is_top_level=(
                            int(r.get("parent") or 0) == 0
                            and int(r.get("root") or 0) == 0
                        ),
                        reply_count=int(r.get("rcount") or 0),
                        video_title=video.title,
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    # ---------- 内部 ----------
    @staticmethod
    def _wrap(e: Exception, prefix: str) -> CollectorError:
        # NetworkException 带 code；412/-352 都是风控
        code = getattr(e, "code", 0) or 0
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        if code in (-412, 412, -352, 352):
            return CollectorError(f"{prefix}：触发风控（{code}），需要退避", code)
        return CollectorError(f"{prefix}：{type(e).__name__} {str(e)[:120]}", code)
