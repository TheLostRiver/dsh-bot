"""自动逛 B 站：找视频 → 读字幕 → 判断 → 互动 → 写报告。

和评论区值班的区别：那个是"守着自家评论区"，这个是"自己去逛"。
所以这里多了候选发现（分区/关键词）、动作矩阵（点赞/投币/收藏/关注）、以及每轮报告。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import AppConfig
from ..llm import Brain, build_browse_prompt
from .actions import ActionResult, InteractionPerformer
from .source import Candidate, Source

log = logging.getLogger("biliauto.browse.runner")

# 太短的（<60秒）和水视频（<1000 播放）不值得花 token 判断
MIN_DURATION = 60
MIN_PLAY = 1000
MAX_ACTIONS_PER_ROUND = 6


@dataclass
class RoundResult:
    found: int = 0
    fresh: int = 0
    judged: int = 0
    worth: int = 0
    actions: list[dict] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "fresh": self.fresh,
            "judged": self.judged,
            "worth": self.worth,
            "actions": self.actions,
            "error": self.error,
        }


class BrowseRunner:
    def __init__(self, cfg: AppConfig, store, credential=None, brain: Brain | None = None):
        self.cfg = cfg
        self.bc = cfg.browse
        self.store = store
        self.credential = credential
        self.brain = brain or Brain(cfg.llm)
        self.source = Source(credential)
        self.performer = InteractionPerformer(
            self.bc, store, credential, dry_run=cfg.dry_run
        )

        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.rounds = 0
        self.last_round: dict = {}
        self.last_round_at: float = 0.0
        self.report_dir = cfg.data_dir / "reports"

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="browse-runner")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "rounds": self.rounds,
            "last_round_at": self.last_round_at,
            "last_round": self.last_round,
            **self.store.browse_stats(),
            "today": self.store.actions_today(),
        }

    async def _loop(self) -> None:
        log.info("自动逛 B 站已启动（间隔 %ss）", self.bc.interval)
        while not self._stop.is_set():
            try:
                result = await self.round_once()
                self.rounds += 1
                self.last_round = result.as_dict()
                self.last_round_at = time.time()
                if result.error:
                    log.warning("本轮出错：%s", result.error)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("逛 B 站异常：%s", e)
                self.last_round = {"error": str(e)[:200]}
                self.last_round_at = time.time()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.bc.interval)
            except asyncio.TimeoutError:
                pass

    # ---------- 一轮 ----------
    async def round_once(self) -> RoundResult:
        r = RoundResult()
        if not self.bc.keywords and not self.bc.zones:
            r.error = "既没配关键词也没选分区，不知道该逛哪儿"
            return r

        try:
            cands = await self.source.discover(self.bc, self.bc.batch)
        except Exception as e:
            r.error = f"找视频失败：{e}"
            return r
        r.found = len(cands)

        fresh: list[Candidate] = []
        for c in cands:
            if self.store.browse_seen(c.bvid):
                continue
            if c.duration and c.duration < MIN_DURATION:
                self.store.record_browse(
                    c.bvid, c.aid, c.title, c.up_mid, c.up_name,
                    c.duration, c.play, c.pubdate, "skip", "too_short",
                )
                continue
            if c.play and c.play < MIN_PLAY:
                self.store.record_browse(
                    c.bvid, c.aid, c.title, c.up_mid, c.up_name,
                    c.duration, c.play, c.pubdate, "skip", "low_play",
                )
                continue
            fresh.append(c)
        r.fresh = len(fresh)

        acted = 0
        for c in fresh:
            if acted >= MAX_ACTIONS_PER_ROUND:
                break
            if r.judged >= self.bc.max_llm:
                break
            try:
                content = await self._content(c)
                plan = await self._decide(c, content)
            except Exception as e:
                log.warning("判断 %s 失败：%s", c.bvid, str(e)[:100])
                continue
            r.judged += 1

            if not plan or not plan.get("worth"):
                self.store.record_browse(
                    c.bvid, c.aid, c.title, c.up_mid, c.up_name,
                    c.duration, c.play, c.pubdate,
                    "skip", str((plan or {}).get("reason", "not_worth"))[:80],
                )
                continue

            r.worth += 1
            results = await self._act(c, plan)
            acted += len([x for x in results if x.ok])
            for res in results:
                self.store.log_action(c.bvid, res.action, res.ok, res.detail)
                r.actions.append({"bvid": c.bvid, **res.as_dict()})

            verdict = ",".join(x.action for x in results if x.ok) or "judged"
            self.store.record_browse(
                c.bvid, c.aid, c.title, c.up_mid, c.up_name,
                c.duration, c.play, c.pubdate, verdict,
                str(plan.get("reason", ""))[:120],
            )
            self._append_report(c, plan, results)

        return r

    # ---------- 内部 ----------
    async def _content(self, c: Candidate) -> str:
        """优先取字幕原文；没有字幕就退回简介。判断内容靠字幕，不看标题。"""
        from bilibili_api.video import Video

        v = Video(bvid=c.bvid, credential=self.credential)
        try:
            info = await v.get_info()
        except Exception as e:
            log.debug("取视频信息失败 %s：%s", c.bvid, e)
            return c.desc

        c.aid = c.aid or int(info.get("aid") or 0)
        c.duration = c.duration or int(info.get("duration") or 0)
        c.desc = str(info.get("desc") or c.desc)
        c.up_name = c.up_name or str((info.get("owner") or {}).get("name") or "")
        c.up_mid = c.up_mid or int((info.get("owner") or {}).get("mid") or 0)
        cid = info.get("cid")

        if not cid:
            return c.desc
        try:
            sub = await v.get_subtitle(int(cid))
        except Exception:
            return c.desc

        tracks = sub.get("subtitle") or sub.get("subtitles") or []
        if not tracks:
            return c.desc
        url = tracks[0].get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return c.desc
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                body = resp.json().get("body") or []
            text = " ".join(str(x.get("content") or "") for x in body)
        except Exception as e:
            log.debug("字幕下载失败 %s：%s", c.bvid, e)
            return c.desc
        return text or c.desc

    async def _decide(self, c: Candidate, content: str) -> dict | None:
        if not self.brain.online:
            return {"worth": False, "reason": "离线模式不判断"}
        text = (content or "")[: self.bc.max_subtitle_chars]
        system = build_browse_prompt(self.cfg.persona, 60)
        user = (
            f"【标题】{c.title}\n"
            f"【UP】{c.up_name}　【来源】{c.source}　"
            f"【播放】{c.play}　【时长】{c.duration}秒\n"
            f"【内容（字幕/简介）】\n{text or '（没有字幕也没有简介）'}"
        )
        try:
            reply, data = await self.brain.decide_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                60,
            )
        except Exception as e:
            log.warning("模型判断失败：%s", e)
            return None

        if not data:
            return {"worth": reply.should_reply, "reason": "", "comment": reply.danmaku}

        return {
            "worth": bool(data.get("worth", reply.should_reply)),
            "reason": str(data.get("reason") or "")[:120],
            "like": bool(data.get("like")),
            "coin": bool(data.get("coin")),
            "favorite": bool(data.get("favorite")),
            "follow": bool(data.get("follow")),
            "comment": str(data.get("comment") or "").strip(),
        }

    async def _act(self, c: Candidate, plan: dict) -> list[ActionResult]:
        out: list[ActionResult] = []
        if plan.get("like"):
            out.append(await self.performer.like(c))
        if plan.get("favorite"):
            out.append(await self.performer.favorite(c))
        if plan.get("coin"):
            out.append(await self.performer.coin(c))
        if plan.get("follow"):
            out.append(await self.performer.follow(c))
        text = str(plan.get("comment") or "").strip()
        if text:
            out.append(await self.performer.comment(c, text))
        return out

    def _append_report(self, c: Candidate, plan: dict, results: list[ActionResult]) -> None:
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            path = self.report_dir / f"browse-{time.strftime('%Y-%m-%d')}.md"
            marks = ", ".join(
                f"{x.action}{'✅' if x.ok else '❌'}" for x in results
            ) or "无动作"
            line = (
                f"- **{time.strftime('%H:%M')}** [{c.bvid}](https://www.bilibili.com/video/{c.bvid}) "
                f"{c.title[:40]}\n"
                f"  - 来源 {c.source} · 播放 {c.play} · 时长 {c.duration}s\n"
                f"  - 判断：{plan.get('reason', '')}\n"
                f"  - 动作：{marks}"
            )
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            log.debug("写报告失败：%s", e)
