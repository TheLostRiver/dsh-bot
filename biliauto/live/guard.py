"""触发策略：决定"哪条弹幕值得 AI 开口"。

独立账号最容易死的方式不是被封 IP，而是被观众举报刷屏。
所以这里的原则是：宁可漏回，不可滥回。
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass

from ..config import GuardConfig, TriggerConfig

log = logging.getLogger("biliauto.guard")


@dataclass
class Decision:
    action: str  # "ignore" | "consider"
    priority: int  # 0 普通 / 1 高优（提问、被@）
    reason: str


class TriggerPolicy:
    def __init__(self, cfg: TriggerConfig, persona_aliases: tuple[str, ...], bot_uid: int | None):
        self.cfg = cfg
        self.aliases = tuple(a.lower() for a in persona_aliases if a)
        self.bot_uid = bot_uid
        self._ignore_res = []
        for pat in cfg.ignore_patterns:
            try:
                self._ignore_res.append(re.compile(pat))
            except re.error:
                log.warning("IGNORE_PATTERNS 里有非法正则，已跳过：%r", pat)
        self._rng = random.Random()

    def classify(self, uid: int, uname: str, text: str, kind: str = "DANMU_MSG") -> Decision:
        if self.bot_uid and uid == self.bot_uid:
            return Decision("ignore", 0, "self")
        text = (text or "").strip()
        if not text:
            return Decision("ignore", 0, "empty")

        # 最高优先：花钱的、开舰长的，必须给反应
        if kind in {"SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"}:
            return Decision("consider", 2, "super_chat")
        if kind in {"GUARD_BUY", "USER_TOAST_MSG"}:
            return Decision("consider", 2, "guard_buy")

        for rx in self._ignore_res:
            if rx.search(text):
                return Decision("ignore", 0, "noise")

        lowered = text.lower()
        if self.aliases and any(a in lowered for a in self.aliases):
            return Decision("consider", 1, "called")
        if self.cfg.reply_on_question and ("?" in text or "？" in text):
            return Decision("consider", 1, "question")

        # 剩下的按概率抽样，避免变成"有问必答机"被当成机器人
        if self._rng.random() < self.cfg.reply_probability:
            return Decision("consider", 0, "sampled")
        return Decision("ignore", 0, "not_selected")


class DanmakuBudget:
    """滑动窗口 + 每日硬上限。所有计数都以"真的发出去了"为准。

    evaluate() 除了判断能不能发，还会告诉调用方"还要等多久"。
    这一点很关键：队列里堆着 5 条值得回的消息时，直接丢掉 4 条是浪费；
    正确做法是等冷却、但丢弃已经过期的消息（回复 2 分钟前的弹幕很怪）。
    """

    # 每天上限是硬墙，等多久都没用
    WAIT_FOREVER = float("inf")

    def __init__(self, cfg: GuardConfig, store, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.dry_run = dry_run
        self._sent_ts: list[float] = []
        self._user_ts: dict[int, float] = {}
        self._last_sent = 0.0
        self._day_used = 0
        self._day_used_at = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        self._sent_ts = [t for t in self._sent_ts if t > cutoff]

    def _used_today(self, now: float) -> int:
        # 每次判断都查库太贵，5 秒缓存足够
        if now - self._day_used_at > 5:
            self._day_used = 0 if self.dry_run else self.store.today_usage()["danmaku_sent"]
            self._day_used_at = now
        return self._day_used

    def evaluate(self, uid: int, now: float | None = None) -> tuple[bool, float, str]:
        """返回 (能否立刻发, 需要等待的秒数, 原因)。"""
        now = now or time.time()
        self._prune(now)

        if self._used_today(now) >= self.cfg.per_day:
            return False, self.WAIT_FOREVER, "per_day"

        gap = now - self._last_sent
        if gap < self.cfg.min_interval:
            return False, self.cfg.min_interval - gap, "cooldown"

        last_user = self._user_ts.get(uid, 0.0)
        if now - last_user < self.cfg.user_cooldown:
            # 同一个人我们会等，但别等太久 —— 等半天再回更奇怪
            wait = self.cfg.user_cooldown - (now - last_user)
            if wait > self.cfg.min_interval * 3:
                return False, self.WAIT_FOREVER, "user_cooldown"
            return False, wait, "user_cooldown"

        in_minute = [t for t in self._sent_ts if t > now - 60]
        if len(in_minute) >= self.cfg.per_minute:
            wait = 60 - (now - in_minute[0]) + 0.5
            return False, wait, "per_minute"

        if len(self._sent_ts) >= self.cfg.per_hour:
            wait = 3600 - (now - self._sent_ts[0]) + 1
            return False, wait, "per_hour"

        return True, 0.0, "ok"

    def check(self, uid: int, now: float | None = None) -> tuple[bool, str]:
        ok, _, reason = self.evaluate(uid, now)
        return ok, reason

    def note_sent(self, uid: int, now: float | None = None) -> None:
        now = now or time.time()
        self._sent_ts.append(now)
        self._user_ts[uid] = now
        self._last_sent = now
        self._day_used += 1
        self._day_used_at = now

    def note_failed(self, now: float | None = None) -> None:
        """发送被风控拒绝时，拉长冷却，让账号缓一缓。"""
        self._last_sent = (now or time.time()) + self.cfg.min_interval * 3

    def snapshot(self) -> dict:
        now = time.time()
        self._prune(now)
        return {
            "sent_last_hour": len(self._sent_ts),
            "sent_today": self.store.today_usage()["danmaku_sent"],
            "since_last": round(now - self._last_sent, 1),
        }
