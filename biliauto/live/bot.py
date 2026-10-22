"""直播互动主控。

数据流：
    LiveDanmaku 推送
        -> parse_event          （统一事件）
        -> TriggerPolicy        （值不值得回）
        -> 优先级队列           （花钱的、被@的插队）
        -> Brain / DeepSeek     （生成弹幕句 + 语音稿）
        -> DanmakuBudget 配额   （能不能发）
        -> DanmakuSender + Speaker
        -> Store                （留痕，供复盘和画像）

关键设计：配额检查放在调 LLM 之前，配额耗尽时连 token 都不烧。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import asdict

from ..config import AppConfig
from ..llm import Brain, build_system_prompt
from ..memory.store import Store
from .guard import DanmakuBudget, TriggerPolicy

log = logging.getLogger("biliauto.bot")


class LiveBot:
    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        brain: Brain | None = None,
        sender=None,
        speaker=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.brain = brain or Brain(cfg.llm)
        self.policy = TriggerPolicy(
            cfg.trigger, cfg.persona.aliases, cfg.bili.bot_uid
        )
        self.budget = DanmakuBudget(cfg.guard, store, dry_run=cfg.dry_run)

        # 延迟导入，避免离线回放时也要连 B 站
        if sender is None:
            from ..bili.actions import DanmakuSender

            room = None
            if not cfg.dry_run and cfg.bili.has_credential:
                from bilibili_api.live import LiveRoom

                from ..bili.credential import build_credential

                room = LiveRoom(
                    cfg.bili.room_id, credential=build_credential(cfg.bili)
                )
            sender = DanmakuSender(
                room, cfg.guard, self.budget, store, dry_run=cfg.dry_run or room is None
            )
        self.sender = sender

        if speaker is None:
            # 即使当前是关的也建一个，这样页面上那个开关能即时生效
            from ..bili.actions import Speaker

            speaker = Speaker(cfg.speech)
        # 语音是否生效做成运行时开关：关掉时也保留 Speaker，随时能再开
        self.speech_enabled = cfg.speech.enabled and speaker is not None
        self.speaker = speaker
        self._speech_tasks: set[asyncio.Task] = set()

        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=50)
        self._seq = itertools.count()
        self._context: deque = deque(maxlen=15)
        self._recent_replies: deque[str] = deque(maxlen=8)
        self._seen: dict[str, float] = {}
        self._stopping = asyncio.Event()
        self.replied = 0
        self.ignored = 0
        self.considered = 0

    # 超过这个年龄的弹幕不值得再回，晚回比不回更怪
    EVENT_MAX_AGE = 30.0
    # 单条消息最多为配额等这么久，再久就放弃
    MAX_BUDGET_WAIT = 20.0

    # ---------- 入口 ----------
    async def handle_event(self, event) -> None:
        """由 LiveClient 或回放脚本调用。必须轻，重活丢给 worker。"""
        decision = self.policy.classify(
            event.uid, event.uname, event.text, event.kind
        )
        self._context.append(event)

        if decision.action == "ignore":
            self.ignored += 1
            self.store.log_event(
                event.kind, event.uid, event.uname, event.text, "ignored", decision.reason
            )
            return

        # 同一人短时间连发同一句，只处理一次
        key = f"{event.uid}:{event.text}"
        now = time.time()
        if now - self._seen.get(key, 0) < 120:
            self.store.log_event(
                event.kind, event.uid, event.uname, event.text, "ignored", "duplicate"
            )
            return
        self._seen[key] = now
        if len(self._seen) > 500:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 600}

        self.considered += 1
        event_id = self.store.log_event(
            event.kind, event.uid, event.uname, event.text, "considered", decision.reason
        )
        try:
            self._queue.put_nowait((-decision.priority, next(self._seq), (event_id, event)))
        except asyncio.QueueFull:
            log.warning("处理队列已满，丢弃：%s", event.text[:20])

    # ---------- 工作协程 ----------
    async def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                _, _, (event_id, event) = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._process(event_id, event)
            except Exception:
                log.exception("处理消息时异常，已跳过该条")
            finally:
                self._queue.task_done()

    async def _await_budget(self, uid: int) -> tuple[bool, str]:
        """在可接受的时间内等配额，而不是一遇到冷却就丢消息。"""
        deadline = time.time() + self.MAX_BUDGET_WAIT
        while True:
            ok, wait, reason = self.budget.evaluate(uid)
            if ok:
                return True, "ok"
            if wait == self.budget.WAIT_FOREVER:
                return False, reason
            if time.time() + wait > deadline:
                return False, reason
            await asyncio.sleep(wait)

    async def _process(self, event_id: int, event) -> None:
        # 过期弹幕直接放弃：等半天再回，观众早走了
        if time.time() - event.ts > self.EVENT_MAX_AGE:
            self.store.set_event_decision(event_id, "skipped", "stale")
            return

        # 先等配额：发不出去就别烧 token
        ok, reason = await self._await_budget(event.uid)
        if not ok:
            log.debug("跳过（%s）：%s", reason, event.text[:16])
            self.store.set_event_decision(event_id, "skipped", reason)
            return

        # 没有 API Key 时 Brain 会进入离线模式并返回占位文本（"[离线]…"），
        # 那种东西绝不能真发出去。演练模式除外，因为演练本来就不发。
        if not self.brain.online and not self.cfg.dry_run:
            if not getattr(self, "_offline_warned", False):
                log.error("DeepSeek 不可用，已停止回复（不会发送离线占位文本）")
                self._offline_warned = True
            self.store.set_event_decision(event_id, "skipped", "llm_offline")
            return

        messages = self._build_messages(event)
        reply = await self.brain.decide(messages, self.cfg.guard.max_danmaku_len)
        self.store.incr_usage("llm_calls")

        if not reply.should_reply:
            self.store.set_event_decision(event_id, "ignored", "model_declined")
            return

        danmaku = self._polish(reply.danmaku)
        sent, why = await self.sender.send(danmaku, reply_mid=event.uid or None)
        self.store.log_reply(
            event_id, event.uid, danmaku, reply.speech, sent, "" if sent else why
        )
        self.store.set_event_decision(event_id, "replied" if sent else "failed", why)

        if not sent:
            log.info("本条不重试，避免触发风控（%s）", why)
            return

        self.replied += 1
        self._recent_replies.append(danmaku)
        self.store.mark_replied(event.uid)
        self.store.touch_viewer(event.uid, event.uname)

        if self.speech_enabled and self.speaker is not None and reply.speech:
            # 语音合成要 1.4 秒左右，不能堵住决策循环：
            # 丢到后台任务，合成完自然进播放队列
            task = asyncio.create_task(self._speak(reply.speech))
            self._speech_tasks.add(task)
            task.add_done_callback(self._speech_tasks.discard)

    async def _speak(self, text: str) -> None:
        try:
            path = await self.speaker.say(text)
            if path:
                self.store.incr_usage("speech_made")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("语音播报失败：%s", e)

    def set_speech(self, enabled: bool) -> None:
        """运行时开关语音，不需要重启机器人。"""
        self.speech_enabled = bool(enabled) and self.speaker is not None
        log.info("语音播报已%s", "开启" if self.speech_enabled else "关闭")

    def _polish(self, text: str) -> str:
        """提示词管不住的时候，最后一道确定性去重：连续重复的口头禅直接摘掉。"""
        cp = (self.cfg.persona.catchphrase or "").strip()
        if not cp or len(text) <= len(cp) + 2:
            return text
        recent = list(self._recent_replies)[-3:]
        if text.endswith(cp) and sum(1 for r in recent if r.endswith(cp)) >= 2:
            stripped = text[: -len(cp)].rstrip("，,。.!！~ ")
            if len(stripped) >= 4:
                log.debug("摘掉重复的口头禅 %r：%s -> %s", cp, text, stripped)
                return stripped
        return text

    def _build_messages(self, event) -> list[dict]:
        system = build_system_prompt(self.cfg.persona, self.cfg.guard.max_danmaku_len)

        recent = list(self._context)
        # 排除正在处理的这一条：context 是按到达顺序追加的，
        # 处理时它未必还在末尾（后面可能又来了新弹幕），按位置切会切错。
        recent = [e for e in recent if e is not event][-8:]
        ctx_lines = [
            f"{e.uname}{'（' + e.medal_name + str(e.medal_level) + '）' if e.medal_name else ''}: {e.text}"
            for e in recent
        ]
        ctx = "\n".join(ctx_lines) if ctx_lines else "（直播间刚开，还没人说话）"

        profile = self.store.viewer_profile(event.uid) or {}
        tag = ""
        if profile:
            tag = (
                f"这个观众在本房发过 {profile.get('msg_count', 0)} 条弹幕，"
                f"你回过 {profile.get('replied', 0)} 次。"
            )

        budget = self.budget.snapshot()
        duty = (
            f"你最近一小时已发 {budget['sent_last_hour']} 条弹幕，"
            f"今天共 {budget['sent_today']} 条。少而精。"
        )

        said = list(self._recent_replies)[-5:]
        said_block = ""
        if said:
            said_block = (
                "【你最近说过】（别重复这些结尾和句式）\n"
                + "\n".join(f"- {s}" for s in said)
                + "\n\n"
            )

        user = (
            f"【直播间最近】\n{ctx}\n\n"
            f"{said_block}"
            f"【现在】{event.uname}"
            f"{'（' + event.medal_name + str(event.medal_level) + '）' if event.medal_name else ''}"
            f" 说：{event.text}\n\n"
            f"【背景】{tag}{duty}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ---------- 生命周期 ----------
    async def run(self, client=None) -> None:
        worker = asyncio.create_task(self._worker(), name="bot-worker")
        tasks = [worker]
        if client is not None:
            tasks.append(asyncio.create_task(client.run(), name="live-client"))

        log.info(
            "机器人已启动 | 房间=%s | dry_run=%s | 配额=%s/%s/%s",
            self.cfg.bili.room_id,
            self.cfg.dry_run,
            self.cfg.guard.per_minute,
            self.cfg.guard.per_hour,
            self.cfg.guard.per_day,
        )

        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in done:
                if not t.cancelled() and t.exception():
                    log.error("任务异常退出：%s", t.exception())
        finally:
            self._stopping.set()
            if client is not None:
                await client.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in list(self._speech_tasks):
                t.cancel()
            if self._speech_tasks:
                await asyncio.gather(*self._speech_tasks, return_exceptions=True)
            if self.speaker is not None:
                await self.speaker.close()
            log.info(
                "已停止 | 回复 %s 条 | 忽略 %s 条", self.replied, self.ignored
            )
