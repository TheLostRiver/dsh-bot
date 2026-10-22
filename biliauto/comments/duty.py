"""评论区值班。

和直播那套的区别：
- 直播是长连接、要抢时效；评论是轮询、可以慢，反而不该抢第一时间。
- 评论区回复更容易被当成刷屏，所以配额更保守、还要去重。

三道闸门：
1. 见过的不再处理（rpid 落库），避免每轮重复回复
2. 只回顶层评论、只看最近 lookback 小时、广告链接直接丢
3. 每日上限 + 两条最小间隔；遇 -412 立刻退避
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass

from ..config import CommentConfig
from ..llm import Brain, build_comment_prompt
from .collector import Collector, CollectorError, CommentEvent

log = logging.getLogger("biliauto.comments.duty")

# 一轮最多做几次模型判断，防止一次扫描烧掉太多 token
MAX_DECISIONS_PER_SCAN = 5

_AD_RE = re.compile(
    r"(https?://|www\.|加群|进群|私信|vx|微信|qq|QQ|代练|刷单|返利|优惠|推广)", re.I
)
# 纯 B 站表情评论，比如 "[打call]" "[大笑][大笑]"
_EMOJI_ONLY_RE = re.compile(r"^(\[[^\[\]]{1,12}\])+$")


@dataclass
class ScanResult:
    videos: int = 0
    comments: int = 0
    fresh: int = 0
    candidates: int = 0
    replied: int = 0
    skipped: int = 0
    error: str = ""
    backoff: float = 0.0

    def as_dict(self) -> dict:
        return {
            "videos": self.videos,
            "comments": self.comments,
            "fresh": self.fresh,
            "candidates": self.candidates,
            "replied": self.replied,
            "skipped": self.skipped,
            "error": self.error,
        }


class CommentBudget:
    """评论回复的每日上限与最小间隔。"""

    def __init__(self, cfg: CommentConfig, store, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.dry_run = dry_run
        self._last = 0.0
        self._used = 0

    def can_send(self, now: float | None = None) -> tuple[bool, float, str]:
        now = now or time.time()
        if not self.dry_run and self.store.today_usage()["comment_sent"] >= self.cfg.max_per_day:
            return False, float("inf"), "per_day"
        gap = now - self._last
        if gap < self.cfg.min_interval:
            return False, self.cfg.min_interval - gap, "cooldown"
        return True, 0.0, "ok"

    def note_sent(self, now: float | None = None) -> None:
        self._last = now or time.time()
        self._used += 1

    def note_blocked(self, seconds: float = 600.0) -> None:
        """被风控时把冷却拉长，别头铁。"""
        self._last = time.time() + seconds


class CommentPolicy:
    def __init__(self, cfg: CommentConfig, persona, bot_uid: int | None):
        self.cfg = cfg
        self.bot_uid = bot_uid
        self.aliases = tuple(a.lower() for a in (persona.aliases or ()) if a)
        self.up_name = ""
        self._rng = random.Random()

    def classify(self, c: CommentEvent) -> tuple[str, str]:
        """返回 (action, reason)。action: reply / ignore"""
        if self.bot_uid and c.uid == self.bot_uid:
            return "ignore", "self"
        if not c.is_top_level:
            return "ignore", "nested"
        text = c.text.strip()
        if len(text) < 3:
            return "ignore", "too_short"
        if _EMOJI_ONLY_RE.match(text):
            return "ignore", "emoji_only"
        if _AD_RE.search(text):
            return "ignore", "ad"
        if c.age_hours > self.cfg.lookback_hours:
            return "ignore", "too_old"

        lowered = text.lower()
        if "?" in text or "？" in text:
            return "reply", "question"
        if self.aliases and any(a in lowered for a in self.aliases):
            return "reply", "called"
        if self.up_name and self.up_name.lower() in lowered:
            return "reply", "about_up"
        if self._rng.random() < self.cfg.reply_probability:
            return "reply", "sampled"
        return "ignore", "not_selected"


class CommentDuty:
    def __init__(
        self,
        cfg,
        store,
        credential=None,
        brain: Brain | None = None,
    ) -> None:
        self.cfg = cfg
        self.cc = cfg.comments
        self.store = store
        self.credential = credential
        self.brain = brain or Brain(cfg.llm)
        self.collector = Collector(credential)
        self.policy = CommentPolicy(self.cc, cfg.persona, cfg.bili.bot_uid)
        self.budget = CommentBudget(self.cc, store, dry_run=cfg.dry_run)

        self._stop = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_scan: dict = {}
        self.last_scan_at: float = 0.0
        self.up_mid: int = self.cc.up_mid
        self.up_name: str = ""
        self.scans: int = 0

    # ---------- 对外 ----------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="comment-duty")

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
            "scans": self.scans,
            "up_mid": self.up_mid,
            "up_name": self.up_name,
            "last_scan_at": self.last_scan_at,
            "last_scan": self.last_scan,
            **self.store.comment_stats(),
        }

    # ---------- 主循环 ----------
    async def _loop(self) -> None:
        log.info("评论区值班已启动（间隔 %ss）", self.cc.poll_interval)
        while not self._stop.is_set():
            delay = self.cc.poll_interval
            try:
                result = await self.scan_once()
                self.scans += 1
                self.last_scan = result.as_dict()
                self.last_scan_at = time.time()
                if result.error:
                    log.warning("扫描出错：%s", result.error)
                if result.backoff:
                    delay = max(delay, result.backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("评论区扫描异常：%s", e)
                self.last_scan = {"error": str(e)[:200]}
                self.last_scan_at = time.time()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ---------- 单轮扫描 ----------
    async def scan_once(self) -> ScanResult:
        """带互斥：长驻循环和手动扫描不能同时跑，否则会重复回复同一批评论。"""
        if self._scan_lock.locked():
            r = ScanResult()
            r.error = "上一轮还没扫完，稍等"
            return r
        async with self._scan_lock:
            return await self._do_scan()

    async def _do_scan(self) -> ScanResult:
        r = ScanResult()

        if self.credential is None:
            r.error = "没有凭据，评论区读取会触发 412"
            return r

        # 1. 定位主播
        try:
            if not self.up_mid:
                self.up_mid, self.up_name = await self.collector.resolve_anchor(
                    self.cfg.bili.room_id
                )
                log.info("从直播间推导出主播：%s (uid=%s)", self.up_name, self.up_mid)
            elif not self.up_name:
                _, self.up_name = await self.collector.resolve_anchor(
                    self.cfg.bili.room_id
                )
            self.policy.up_name = self.up_name
        except CollectorError as e:
            r.error = str(e)
            return r

        # 2. 拉最近视频
        try:
            videos = await self.collector.videos(self.up_mid, self.cc.videos)
        except CollectorError as e:
            r.error = str(e)
            r.backoff = 900 if e.code in (-412, 412, -352, 352) else 0
            return r
        r.videos = len(videos)

        # 3. 逐视频读评论
        queued: list[CommentEvent] = []
        for v in videos:
            try:
                items = await self.collector.comments(v)
            except CollectorError as e:
                log.warning("%s", e)
                if e.code in (-412, 412, -352, 352):
                    r.error = str(e)
                    r.backoff = 900
                    return r
                continue
            r.comments += len(items)

            for c in items:
                if self.store.seen_comment(c.rpid):
                    continue
                r.fresh += 1
                action, reason = self.policy.classify(c)
                if action == "ignore":
                    self.store.record_comment(
                        c.rpid, c.oid, c.uid, c.uname, c.text, c.ctime, "ignored", reason
                    )
                    r.skipped += 1
                    continue
                self.store.record_comment(
                    c.rpid, c.oid, c.uid, c.uname, c.text, c.ctime, "considered", reason
                )
                queued.append(c)

        # 4. 回复（受配额和每轮上限约束）
        r.candidates = len(queued)
        decisions = 0
        for c in queued:
            if decisions >= MAX_DECISIONS_PER_SCAN:
                break
            ok, wait, why = self.budget.can_send()
            if not ok:
                # 不是不回了，是这会儿不能回：留成待定，下一轮还能捡回来
                log.debug("评论区顺延（%s）", why)
                self.store.set_comment_action(c.rpid, "deferred", why)
                r.skipped += 1
                continue

            decisions += 1
            reply = await self._decide(c)
            if not reply or not reply.should_reply:
                self.store.set_comment_action(c.rpid, "ignored", "model_declined")
                continue

            text = self._clamp(reply.danmaku)
            if not text:
                self.store.set_comment_action(c.rpid, "ignored", "empty")
                continue

            sent, err = await self._send(c, text)
            self.store.log_comment_reply(
                c.rpid, c.oid, c.uid, c.text, text, sent, err
            )
            self.store.set_comment_action(
                c.rpid, "replied" if sent else "failed", err
            )
            if sent:
                r.replied += 1
                self.store.incr_usage("comment_sent")
                self.budget.note_sent()

        return r

    # ---------- 内部 ----------
    def _clamp(self, text: str) -> str:
        text = " ".join((text or "").split())
        if len(text) > self.cc.max_len:
            text = text[: self.cc.max_len]
        return text

    async def _decide(self, c: CommentEvent):
        if not self.brain.online and not self.cfg.dry_run:
            if not getattr(self, "_offline_warned", False):
                log.error("DeepSeek 不可用，评论区不再回复（不会发送离线占位文本）")
                self._offline_warned = True
            return None
        system = build_comment_prompt(
            self.cfg.persona, self.cc.max_len, self.up_name, c.video_title
        )
        user = (
            f"【视频】{c.video_title}\n\n"
            f"【评论】{c.uname}：{c.text}\n\n"
            f"（这条评论 {c.age_hours:.1f} 小时前发的，已有 {c.reply_count} 条回复）"
        )
        try:
            return await self.brain.decide(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                self.cc.max_len,
            )
        except Exception as e:
            log.warning("评论区决策失败：%s", e)
            return None

    async def _send(self, c: CommentEvent, text: str) -> tuple[bool, str]:
        if self.cfg.dry_run:
            log.info("[DRY-RUN] 本应回复评论 %s：%s", c.uname, text)
            return True, "dry_run"

        from bilibili_api import comment as bili_comment

        try:
            resp = await bili_comment.send_comment(
                text,
                oid=c.oid,
                type_=bili_comment.CommentResourceType.VIDEO,
                root=c.rpid,
                credential=self.credential,
            )
        except Exception as e:
            code = getattr(e, "code", 0)
            if code in (-412, 412, -352, 352):
                self.budget.note_blocked(900)
            log.warning("回复评论异常：%s", e)
            return False, f"exception:{str(e)[:80]}"

        # send_comment 成功时返回的是 data 字段（含 rpid 等），不含 code——没抛异常即成功
        code = resp.get("code") if isinstance(resp, dict) else None
        if code is None or (isinstance(code, int) and code == 0):
            log.info("已回复评论（%s）：%s", c.uname, text)
            return True, "ok"
        if code in (-412, 412, -352, 352):
            self.budget.note_blocked(900)
        log.warning("回复评论被拒 code=%s", code)
        return False, f"code:{code}"
