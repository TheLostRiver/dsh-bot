"""候选来源：按关键词搜、按分区看排行榜、都没有就看热门。

踩过的坑：
- `search_by_type` 的 keyword **不能为空**，空串直接 -400。所以"只看分区"必须走排行榜，
  不能指望搜索接口。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from bilibili_api import hot, rank, search

log = logging.getLogger("biliauto.browse.source")

_EM_RE = re.compile(r"</?em[^>]*>")


@dataclass
class Candidate:
    bvid: str
    aid: int
    title: str
    up_mid: int
    up_name: str
    duration: int
    play: int
    pubdate: float
    desc: str = ""
    source: str = ""

    @property
    def age_hours(self) -> float:
        return (time.time() - self.pubdate) / 3600 if self.pubdate else 1e9


def _clean(text: str) -> str:
    return _EM_RE.sub("", str(text or "")).strip()


def _rank_type(name: str):
    """把配置里的分区名转成 RankType，名字不认识就返回 None。"""
    rt = getattr(rank.RankType, name, None)
    return rt if isinstance(rt, rank.RankType) else None


class Source:
    def __init__(self, credential=None) -> None:
        self.credential = credential

    async def discover(self, cfg, limit: int) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[str] = set()

        def add(c: Candidate) -> None:
            if c.bvid and c.bvid not in seen:
                seen.add(c.bvid)
                out.append(c)

        # 1) 关键词搜索
        for kw in cfg.keywords:
            if len(out) >= limit:
                break
            for c in await self._search(kw, cfg, limit):
                add(c)

        # 2) 分区排行榜
        for zone in cfg.zones:
            if len(out) >= limit:
                break
            for c in await self._zone(zone, limit):
                add(c)

        # 3) 都没有就热门
        if not cfg.keywords and not cfg.zones:
            for c in await self._hot(limit):
                add(c)

        return out[:limit]

    # ---------- 各来源 ----------
    async def _search(self, keyword: str, cfg, limit: int) -> list[Candidate]:
        order = {
            "pubdate": search.OrderVideo.PUBDATE,
            "click": search.OrderVideo.CLICK,
            "dm": search.OrderVideo.DM,
        }.get(cfg.order, search.OrderVideo.PUBDATE)
        try:
            raw = await search.search_by_type(
                keyword,
                search.SearchObjectType.VIDEO,
                order_type=order,
                time_range=max(1, cfg.days),
            )
        except Exception as e:
            log.warning("搜索 %r 失败：%s", keyword, str(e)[:120])
            return []

        out: list[Candidate] = []
        for it in (raw.get("result") or [])[: limit * 2]:
            bvid = it.get("bvid")
            if not bvid:
                continue
            out.append(
                Candidate(
                    bvid=str(bvid),
                    aid=int(it.get("aid") or 0),
                    title=_clean(it.get("title")),
                    up_mid=int(it.get("mid") or 0),
                    up_name=str(it.get("author") or ""),
                    duration=_duration_seconds(it.get("duration")),
                    play=int(it.get("play") or 0),
                    pubdate=float(it.get("pubdate") or 0),
                    desc=str(it.get("description") or ""),
                    source=f"search:{keyword}",
                )
            )
        return out

    async def _zone(self, zone_name: str, limit: int) -> list[Candidate]:
        rt = _rank_type(zone_name)
        if rt is None:
            log.warning("未知分区：%s", zone_name)
            return []
        try:
            raw = await rank.get_rank(type_=rt, day=rank.RankDayType.WEEK)
        except Exception as e:
            log.warning("取 %s 排行榜失败：%s", zone_name, str(e)[:120])
            return []

        out: list[Candidate] = []
        for it in (raw.get("list") or [])[: limit * 2]:
            bvid = it.get("bvid")
            if not bvid:
                continue
            owner = it.get("owner") or {}
            stat = it.get("stat") or {}
            out.append(
                Candidate(
                    bvid=str(bvid),
                    aid=int(it.get("aid") or 0),
                    title=_clean(it.get("title")),
                    up_mid=int(owner.get("mid") or 0),
                    up_name=str(owner.get("name") or ""),
                    duration=int(it.get("duration") or 0),
                    play=int(stat.get("view") or 0),
                    pubdate=float(it.get("pubdate") or 0),
                    desc=str(it.get("desc") or ""),
                    source=f"rank:{zone_name}",
                )
            )
        return out

    async def _hot(self, limit: int) -> list[Candidate]:
        try:
            raw = await hot.get_hot_videos(ps=limit * 2)
        except Exception as e:
            log.warning("取热门失败：%s", str(e)[:120])
            return []

        out: list[Candidate] = []
        for it in (raw.get("list") or [])[: limit * 2]:
            bvid = it.get("bvid")
            if not bvid:
                continue
            owner = it.get("owner") or {}
            stat = it.get("stat") or {}
            out.append(
                Candidate(
                    bvid=str(bvid),
                    aid=int(it.get("aid") or 0),
                    title=_clean(it.get("title")),
                    up_mid=int(owner.get("mid") or 0),
                    up_name=str(owner.get("name") or ""),
                    duration=int(it.get("duration") or 0),
                    play=int(stat.get("view") or 0),
                    pubdate=float(it.get("pubdate") or 0),
                    desc=str(it.get("desc") or ""),
                    source="hot",
                )
            )
        return out


def _duration_seconds(v) -> int:
    """搜索接口的 duration 有时是 "12:34"，有时候是秒数。"""
    if isinstance(v, int):
        return v
    s = str(v or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    except ValueError:
        return 0
