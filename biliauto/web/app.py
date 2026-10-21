"""Web 管理后台。

只监听 127.0.0.1：凭据、cookie 全在本机，不该暴露到局域网。
后端只做四件事：读状态、改设置、启停机器人、扫码登录。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..auth import qrlogin, vault
from ..config import AppConfig, setup_logging
from ..memory.store import Store

log = logging.getLogger("biliauto.web")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------- 日志缓冲
class LogBuffer(logging.Handler):
    """把日志留在内存里供前端轮询，同时不阻止控制台输出。"""

    NOISY = ("uvicorn", "LiveDanmaku", "bilibili_api", "httpx", "openai", "websockets")

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._items: deque[dict] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(self.NOISY):
            return
        try:
            self._seq += 1
            self._items.append(
                {
                    "seq": self._seq,
                    "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name.replace("biliauto.", ""),
                    "msg": record.getMessage(),
                }
            )
        except Exception:  # 日志系统自己不能抛
            pass

    def since(self, seq: int) -> list[dict]:
        return [x for x in self._items if x["seq"] > seq]

    def tail(self, n: int) -> list[dict]:
        return list(self._items)[-n:]

    @property
    def last_seq(self) -> int:
        return self._seq


# ---------------------------------------------------------------- 机器人管理
class BotManager:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.bot = None
        self.client = None
        self.store: Store | None = None
        self.started_at: float = 0.0
        self.error: str = ""
        self.dry_run: bool = False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, cfg: AppConfig) -> None:
        if self.running:
            raise RuntimeError("机器人已在运行")

        from ..bili import build_credential
        from ..bili.danmaku import LiveClient
        from ..live.bot import LiveBot

        if cfg.bili.room_id <= 0:
            raise RuntimeError("还没填直播间房号")

        store = Store(cfg.data_dir / "biliauto.db")
        bot = LiveBot(cfg, store)
        credential = build_credential(cfg.bili)
        client = LiveClient(cfg.bili.room_id, credential=credential, handler=bot.handle_event)

        self.store, self.bot, self.client = store, bot, client
        self.dry_run = cfg.dry_run
        self.started_at = time.time()
        self.error = ""
        self.task = asyncio.create_task(bot.run(client), name="web-bot")

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self.error = f"{type(exc).__name__}: {exc}"
                log.error("机器人异常退出：%s", self.error)
            else:
                log.info("机器人已退出")

        self.task.add_done_callback(_on_done)

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.store is not None:
            self.store.close()
            self.store = None
        self.bot = None
        self.client = None

    def snapshot(self) -> dict:
        bot = self.bot
        return {
            "running": self.running,
            "dry_run": self.dry_run,
            "uptime": round(time.time() - self.started_at, 0) if self.started_at else 0,
            "replied": getattr(bot, "replied", 0),
            "ignored": getattr(bot, "ignored", 0),
            "considered": getattr(bot, "considered", 0),
            "queued": bot._queue.qsize() if bot is not None else 0,
            "error": self.error,
        }


# ---------------------------------------------------------------- 缓存
class _Cache:
    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self.at = 0.0
        self.data: dict = {}

    def fresh(self) -> bool:
        return time.time() - self.at < self.ttl

    def set(self, data: dict) -> None:
        self.at, self.data = time.time(), data


_account_cache = _Cache(60)
_room_cache = _Cache(30)
_voice_cache = _Cache(3600)
_mix_probe = None  # 混音自检用的临时 Speaker

# 试听音频很小（十几 KB），内存里留十几条足够，别写盘
_tts_cache: dict[tuple, bytes] = {}


async def account_info(cfg: AppConfig, force: bool = False) -> dict:
    """账号状态。凭据本身绝不回显，只回显身份和体检结论。"""
    if not force and _account_cache.fresh():
        return _account_cache.data

    from ..bili import build_credential, verify_credential

    cred = build_credential(cfg.bili)
    if cred is None:
        data = {"configured": False, "name": "", "uid": 0, "warnings": ["尚未登录"]}
        _account_cache.set(data)
        return data

    info = await verify_credential(cred)
    if info is None:
        data = {
            "configured": True,
            "valid": False,
            "name": "",
            "uid": cfg.bili.bot_uid or 0,
            "warnings": ["cookie 已失效，需要重新扫码"],
        }
    else:
        data = {
            "configured": True,
            "valid": True,
            "name": info.name,
            "uid": info.mid,
            "level": info.level,
            "moral": info.moral,
            "can_speak": info.can_speak,
            "warnings": info.warnings(),
        }
    _account_cache.set(data)
    return data


async def room_info(cfg: AppConfig) -> dict:
    if cfg.bili.room_id <= 0:
        return {"id": 0, "live": False, "error": "未配置房号"}
    if _room_cache.fresh():
        return _room_cache.data
    try:
        from bilibili_api.live import LiveRoom

        info = await LiveRoom(cfg.bili.room_id).get_room_play_info()
        data = {
            "id": cfg.bili.room_id,
            "live": info.get("live_status") == 1,
            "title": info.get("title") or "",
            "online": info.get("online") or 0,
        }
    except Exception as e:
        data = {"id": cfg.bili.room_id, "live": False, "error": str(e)[:120]}
    _room_cache.set(data)
    return data


# ---------------------------------------------------------------- 评论区值班
class CommentManager:
    def __init__(self) -> None:
        self.duty = None
        self.store: Store | None = None

    @property
    def running(self) -> bool:
        return self.duty is not None and self.duty.running

    def _build(self, cfg: AppConfig):
        from ..bili import build_credential
        from ..comments import CommentDuty

        if self.store is None:
            self.store = Store(cfg.data_dir / "biliauto.db")
        credential = build_credential(cfg.bili)
        return CommentDuty(cfg, self.store, credential=credential)

    async def start(self, cfg: AppConfig) -> None:
        if self.running:
            raise RuntimeError("评论区值班已在运行")
        if not cfg.bili.has_credential:
            raise RuntimeError("没有登录凭据，评论区读取会触发 412")
        self.duty = self._build(cfg)
        self.duty.start()

    async def stop(self) -> None:
        if self.duty is not None:
            await self.duty.stop()
        self.duty = None

    async def scan_now(self, cfg: AppConfig) -> dict:
        """手动扫一轮。没在跑就临时建一个。"""
        if self.duty is None:
            self.duty = self._build(cfg)
        result = await self.duty.scan_once()
        self.duty.scans += 1
        self.duty.last_scan = result.as_dict()
        self.duty.last_scan_at = time.time()
        return result.as_dict()

    def snapshot(self) -> dict:
        if self.duty is None:
            return {"running": False, "replied": 0, "attempts": 0, "seen": 0}
        snap = self.duty.snapshot()
        snap["running"] = self.running
        return snap


# ---------------------------------------------------------------- 自动逛 B 站
class BrowseManager:
    def __init__(self) -> None:
        self.runner = None
        self.store: Store | None = None

    @property
    def running(self) -> bool:
        return self.runner is not None and self.runner.running

    def _build(self, cfg: AppConfig):
        from ..bili import build_credential
        from ..browse import BrowseRunner

        if self.store is None:
            self.store = Store(cfg.data_dir / "biliauto.db")
        return BrowseRunner(cfg, self.store, credential=build_credential(cfg.bili))

    async def start(self, cfg: AppConfig) -> None:
        if self.running:
            raise RuntimeError("自动逛 B 站已在运行")
        self.runner = self._build(cfg)
        self.runner.start()

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.stop()
        self.runner = None

    async def round_now(self, cfg: AppConfig) -> dict:
        # 手动一轮必须用「当前设置」重建 runner：
        # 复用旧 runner 会带着旧的 dry_run / 关键词，甚至真的发出动作。
        if self.runner is None or not self.runner.running:
            self.runner = self._build(cfg)
        result = await self.runner.round_once()
        self.runner.rounds += 1
        self.runner.last_round = result.as_dict()
        self.runner.last_round_at = time.time()
        return result.as_dict()

    def snapshot(self) -> dict:
        if self.runner is None:
            cfg = AppConfig.load()
            store = Store(cfg.data_dir / "biliauto.db")
            try:
                return {
                    "running": False,
                    **store.browse_stats(),
                    "today": store.actions_today(),
                }
            finally:
                store.close()
        snap = self.runner.snapshot()
        snap["running"] = self.running
        return snap


# ---------------------------------------------------------------- 应用
def _settings_payload(cfg: AppConfig) -> dict:
    return {
        "room_id": cfg.bili.room_id,
        "dry_run": cfg.dry_run,
        "persona": {
            "name": cfg.persona.name,
            "bio": cfg.persona.bio,
            "style": cfg.persona.style,
            "catchphrase": cfg.persona.catchphrase,
            "aliases": list(cfg.persona.aliases),
        },
        "trigger": {
            "reply_probability": cfg.trigger.reply_probability,
            "reply_on_question": cfg.trigger.reply_on_question,
            "ignore_patterns": list(cfg.trigger.ignore_patterns),
        },
        "guard": {
            "min_interval": cfg.guard.min_interval,
            "per_minute": cfg.guard.per_minute,
            "per_hour": cfg.guard.per_hour,
            "per_day": cfg.guard.per_day,
            "user_cooldown": cfg.guard.user_cooldown,
            "max_danmaku_len": cfg.guard.max_danmaku_len,
        },
        "speech": {
            "enabled": cfg.speech.enabled,
            "backend": cfg.speech.backend,
            "voice": cfg.speech.voice,
            "rate": cfg.speech.rate,
            "pitch": cfg.speech.pitch,
            "max_chars": cfg.speech.max_chars,
            "device": cfg.speech.device,
        },
        "comments": {
            "enabled": cfg.comments.enabled,
            "up_mid": cfg.comments.up_mid,
            "videos": cfg.comments.videos,
            "poll_interval": cfg.comments.poll_interval,
            "lookback_hours": cfg.comments.lookback_hours,
            "max_per_day": cfg.comments.max_per_day,
            "min_interval": cfg.comments.min_interval,
            "max_len": cfg.comments.max_len,
            "reply_probability": cfg.comments.reply_probability,
        },
        "browse": {
            "enabled": cfg.browse.enabled,
            "interval": cfg.browse.interval,
            "batch": cfg.browse.batch,
            "max_llm": cfg.browse.max_llm,
            "zones": list(cfg.browse.zones),
            "ups": list(cfg.browse.ups),
            "up_names": list(cfg.browse.up_names),
            "topics": list(cfg.browse.topics),
            "keywords": list(cfg.browse.keywords),
            "days": cfg.browse.days,
            "order": cfg.browse.order,
            "min_play": cfg.browse.min_play,
            "min_duration": cfg.browse.min_duration,
            "max_subtitle_chars": cfg.browse.max_subtitle_chars,
            "favorite_folder": cfg.browse.favorite_folder,
            "like": _action_dict(cfg.browse.like),
            "coin": _action_dict(cfg.browse.coin),
            "favorite": _action_dict(cfg.browse.favorite),
            "follow": _action_dict(cfg.browse.follow),
            "comment": _action_dict(cfg.browse.comment),
        },
    }


def _action_dict(spec) -> dict:
    return {
        "enabled": spec.enabled,
        "per_day": spec.per_day,
        "probability": spec.probability,
    }


def create_app() -> FastAPI:
    setup_logging(AppConfig.load().log_level)

    app = FastAPI(title="dsh-bot 管理后台", docs_url=None, redoc_url=None)
    buffer = LogBuffer()
    logging.getLogger().addHandler(buffer)
    manager = BotManager()
    comment_mgr = CommentManager()
    browse_mgr = BrowseManager()

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status() -> dict:
        cfg = AppConfig.load()
        account = await account_info(cfg)
        room = await room_info(cfg)
        usage = Store(cfg.data_dir / "biliauto.db").today_usage()
        return {
            "account": account,
            "room": room,
            "bot": manager.snapshot(),
            "comments": comment_mgr.snapshot(),
            "browse": browse_mgr.snapshot(),
            "usage": usage,
            "llm": {"model": cfg.llm.model, "ok": cfg.llm.usable},
            "data_dir": str(cfg.data_dir),
        }

    # ---------------- 登录 ----------------
    @app.post("/api/login/start")
    async def login_start() -> dict:
        try:
            session = await qrlogin.generate()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"生成二维码失败：{e}")
        return {
            "key": session.key,
            "qr": qrlogin.render_qr_png_data_url(session.url),
            "expires_in": qrlogin.QR_TTL,
        }

    @app.get("/api/login/poll")
    async def login_poll(key: str) -> dict:
        try:
            result = await qrlogin.poll(key)
        except Exception as e:
            return {"state": "error", "message": f"轮询失败：{e}"}

        if result.state == "ok" and result.credential:
            cfg = AppConfig.load()
            vault.save_credential(cfg.data_dir, result.credential)
            account = await account_info(cfg, force=True)
            return {"state": "ok", "message": "登录成功", "account": account}
        return {"state": result.state, "message": result.message}

    @app.post("/api/logout")
    async def logout() -> dict:
        cfg = AppConfig.load()
        if manager.running:
            await manager.stop()
        vault.clear_credential(cfg.data_dir)
        await account_info(cfg, force=True)
        return {"ok": True}

    # ---------------- 设置 ----------------
    @app.get("/api/settings")
    async def get_settings() -> dict:
        return _settings_payload(AppConfig.load())

    @app.post("/api/settings")
    async def put_settings(payload: dict) -> dict:
        cfg = AppConfig.load()
        vault.save_settings(cfg.data_dir, payload)
        return {"ok": True, "restart_required": manager.running}

    # ---------------- 机器人 ----------------
    @app.post("/api/bot/start")
    async def bot_start(payload: dict | None = None) -> dict:
        cfg = AppConfig.load()
        if payload and "dry_run" in payload:
            cfg = replace(cfg, dry_run=bool(payload["dry_run"]))
        try:
            await manager.start(cfg)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            log.exception("启动失败")
            raise HTTPException(status_code=500, detail=f"启动失败：{e}")
        return {"ok": True, "bot": manager.snapshot()}

    @app.post("/api/bot/stop")
    async def bot_stop() -> dict:
        await manager.stop()
        return {"ok": True, "bot": manager.snapshot()}

    @app.post("/api/bot/dry-run")
    async def bot_dry_run(payload: dict) -> dict:
        """改 dry_run 需要重启才生效，这里只做提示。"""
        return {"ok": True, "restart_required": manager.running}

    # ---------------- 人设预览 ----------------
    @app.post("/api/persona/preview")
    async def persona_preview(payload: dict) -> dict:
        """用前端表单里（可能还没保存）的人设，真跑一次决策看效果。"""
        cfg = AppConfig.load()
        if not cfg.llm.usable:
            raise HTTPException(status_code=400, detail="未配置 DEEPSEEK_API_KEY，无法预览")

        text = str(payload.get("text") or "").strip() or "主播今天播到几点啊？"
        text = text[:120]

        p = payload.get("persona") or {}
        aliases = p.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [x.strip() for x in aliases.split(",") if x.strip()]

        persona = replace(
            cfg.persona,
            name=str(p.get("name") or cfg.persona.name),
            bio=str(p.get("bio") or cfg.persona.bio),
            style=str(p.get("style") or cfg.persona.style),
            catchphrase=str(p.get("catchphrase") or ""),
            aliases=tuple(str(a) for a in aliases),
        )

        from ..llm import Brain, build_system_prompt

        system = build_system_prompt(persona, cfg.guard.max_danmaku_len)
        user = (
            "【直播间最近】\n（刚开播，人不多）\n\n"
            f"【现在】观众「小明」说：{text}\n\n"
            "【背景】这是人设试说，正常接话即可。"
        )

        started = time.time()
        reply = await Brain(cfg.llm).decide(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            cfg.guard.max_danmaku_len,
        )
        return {
            "input": text,
            "should_reply": reply.should_reply,
            "danmaku": reply.danmaku,
            "speech": reply.speech,
            "mood": reply.mood,
            "elapsed_ms": int((time.time() - started) * 1000),
            "model": cfg.llm.model,
        }

    # ---------------- 语音 ----------------
    @app.get("/api/voices")
    async def voices() -> dict:
        if _voice_cache.fresh():
            return _voice_cache.data
        try:
            import edge_tts

            raw = await edge_tts.list_voices()
        except Exception as e:
            return {"items": [], "total": 0, "error": f"获取音色列表失败：{e}"}

        items = [
            {
                "id": v.get("ShortName", ""),
                "name": (v.get("FriendlyName") or "").split(" - ")[0],
                "gender": "女" if v.get("Gender") == "Female" else "男",
                "locale": v.get("Locale", ""),
                "zh": str(v.get("Locale", "")).startswith("zh"),
            }
            for v in raw
        ]
        # 中文排前面，其余按地区排；下拉框才好用
        items.sort(key=lambda x: (not x["zh"], x["locale"], x["id"]))
        data = {"items": items, "total": len(items)}
        _voice_cache.set(data)
        return data

    @app.get("/api/tts/preview")
    async def tts_preview(
        voice: str,
        text: str = "你好呀，我是直播间里那只 AI。",
        rate: str = "+0%",
        pitch: str = "+0Hz",
    ) -> Response:
        text = (text or "").strip()[:60]
        if not text:
            raise HTTPException(status_code=400, detail="试听文本不能为空")

        key = (voice, text, rate, pitch)
        if key not in _tts_cache:
            try:
                import edge_tts

                comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                buf = bytearray()
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio":
                        buf.extend(chunk["data"])
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"合成失败：{e}")
            if not buf:
                raise HTTPException(status_code=502, detail="合成结果为空，音色名可能不对")
            if len(_tts_cache) > 16:
                _tts_cache.clear()
            _tts_cache[key] = bytes(buf)

        return Response(
            content=_tts_cache[key],
            media_type="audio/mpeg",
            headers={"Cache-Control": "max-age=600"},
        )

    # ---------------- 音频设备 ----------------
    @app.get("/api/audio/devices")
    async def audio_devices() -> dict:
        """列出输入/输出设备。混音要选一路麦克风 + 一路虚拟声卡。"""
        try:
            import sounddevice as sd
        except Exception as e:
            return {"items": [], "inputs": [], "error": f"未安装 sounddevice：{e}"}

        try:
            outs, ins = [], []
            for idx, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) > 0:
                    outs.append(
                        {
                            "index": idx,
                            "name": str(d.get("name") or ""),
                            "channels": d["max_output_channels"],
                        }
                    )
                if d.get("max_input_channels", 0) > 0:
                    ins.append(
                        {
                            "index": idx,
                            "name": str(d.get("name") or ""),
                            "channels": d["max_input_channels"],
                        }
                    )
            try:
                default_out = str(sd.query_devices(kind="output").get("name") or "")
            except Exception:
                default_out = ""
            try:
                default_in = str(sd.query_devices(kind="input").get("name") or "")
            except Exception:
                default_in = ""
            return {
                "items": outs,
                "inputs": ins,
                "default": default_out,
                "default_input": default_in,
                "error": "",
            }
        except Exception as e:
            return {"items": [], "inputs": [], "error": str(e)[:160]}

    # ---------------- 混音 ----------------
    @app.get("/api/audio/mix")
    async def mix_status() -> dict:
        cfg = AppConfig.load()
        bot = manager.bot
        if bot is not None and bot.speaker is not None:
            return bot.speaker.mixer_snapshot()
        return {
            "running": False,
            "enabled": cfg.speech.mix_enabled,
            "input": cfg.speech.mix_input,
            "output": cfg.speech.mix_output or cfg.speech.device,
            "mic_gain": cfg.speech.mix_mic_gain,
            "tts_gain": cfg.speech.mix_tts_gain,
            "duck": cfg.speech.mix_duck,
        }

    @app.post("/api/audio/mix/test")
    async def mix_test() -> dict:
        """用当前设置起一次混音，并送一段测试语音，方便确认路由对不对。"""
        global _mix_probe
        cfg = AppConfig.load()
        if not cfg.speech.mix_enabled:
            raise HTTPException(status_code=409, detail="先在设置里打开混音")
        from dataclasses import replace as _replace

        from ..bili.actions import Speaker

        if _mix_probe is None:
            _mix_probe = Speaker(_replace(cfg.speech, mix_enabled=True))
        ok, msg = _mix_probe.ensure_mixer()
        if not ok:
            raise HTTPException(status_code=409, detail=msg)
        path = await _mix_probe.say("混音测试，一二三，能听到就说明路由是对的")
        return {
            "ok": True,
            "message": "已送出一段测试语音，检查直播姬里能不能听到",
            "generated": bool(path),
            "mixer": _mix_probe.mixer_snapshot(),
        }

    @app.post("/api/audio/mix/stop")
    async def mix_stop() -> dict:
        global _mix_probe
        if _mix_probe is not None:
            _mix_probe.stop_mixer()
        bot = manager.bot
        if bot is not None and bot.speaker is not None:
            bot.speaker.stop_mixer()
        return {"ok": True}

    # ---------------- 语音播报开关 ----------------
    @app.post("/api/speech/toggle")
    async def speech_toggle(payload: dict | None = None) -> dict:
        """一个开关：立刻生效 + 落盘，不用重启机器人。"""
        cfg = AppConfig.load()
        enabled = None
        if payload and "enabled" in payload:
            enabled = bool(payload["enabled"])
        else:
            enabled = not cfg.speech.enabled

        settings = vault.load_settings(cfg.data_dir)
        speech = settings.get("speech")
        if not isinstance(speech, dict):
            speech = {}
            settings["speech"] = speech
        speech["enabled"] = enabled
        vault.save_settings(cfg.data_dir, settings)

        applied = False
        bot = manager.bot
        if bot is not None:
            bot.set_speech(enabled)
            applied = True
        return {"ok": True, "enabled": enabled, "applied": applied}

    @app.get("/api/speech/state")
    async def speech_state() -> dict:
        cfg = AppConfig.load()
        bot = manager.bot
        return {
            "enabled": bool(bot.speech_enabled) if bot is not None else cfg.speech.enabled,
            "running": manager.running,
            "backend": cfg.speech.backend,
            "voice": cfg.speech.voice,
        }

    # ---------------- 评论区值班 ----------------
    @app.get("/api/comments/status")
    async def comments_status() -> dict:
        return comment_mgr.snapshot()

    @app.post("/api/comments/start")
    async def comments_start() -> dict:
        cfg = AppConfig.load()
        try:
            await comment_mgr.start(cfg)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            log.exception("评论区启动失败")
            raise HTTPException(status_code=500, detail=f"启动失败：{e}")
        return {"ok": True, "comments": comment_mgr.snapshot()}

    @app.post("/api/comments/stop")
    async def comments_stop() -> dict:
        await comment_mgr.stop()
        return {"ok": True, "comments": comment_mgr.snapshot()}

    @app.post("/api/comments/scan")
    async def comments_scan() -> dict:
        cfg = AppConfig.load()
        if not cfg.bili.has_credential:
            raise HTTPException(status_code=409, detail="没有登录凭据，读评论会触发 412")
        try:
            result = await comment_mgr.scan_now(cfg)
        except Exception as e:
            log.exception("手动扫描失败")
            raise HTTPException(status_code=500, detail=f"扫描失败：{e}")
        return {"ok": True, "result": result, "comments": comment_mgr.snapshot()}

    @app.get("/api/comments/replies")
    async def comments_replies(limit: int = 20) -> dict:
        cfg = AppConfig.load()
        store = Store(cfg.data_dir / "biliauto.db")
        try:
            return {"items": store.recent_comment_replies(limit)}
        finally:
            store.close()

    # ---------------- 自动逛 B 站 ----------------
    @app.get("/api/browse/status")
    async def browse_status() -> dict:
        return browse_mgr.snapshot()

    @app.post("/api/browse/start")
    async def browse_start() -> dict:
        cfg = AppConfig.load()
        try:
            await browse_mgr.start(cfg)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            log.exception("自动逛启动失败")
            raise HTTPException(status_code=500, detail=f"启动失败：{e}")
        return {"ok": True, "browse": browse_mgr.snapshot()}

    @app.post("/api/browse/stop")
    async def browse_stop() -> dict:
        await browse_mgr.stop()
        return {"ok": True, "browse": browse_mgr.snapshot()}

    @app.post("/api/browse/round")
    async def browse_round() -> dict:
        cfg = AppConfig.load()
        try:
            result = await browse_mgr.round_now(cfg)
        except Exception as e:
            log.exception("逛一轮失败")
            raise HTTPException(status_code=500, detail=f"失败：{e}")
        return {"ok": True, "result": result, "browse": browse_mgr.snapshot()}

    @app.get("/api/browse/resolve")
    async def browse_resolve(name: str) -> dict:
        """按昵称搜 UP 主，方便后台加人时不用手抄 mid。"""
        name = (name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="请输入 UP 主昵称")
        from ..bili import build_credential
        from ..browse.source import Source

        cfg = AppConfig.load()
        items = await Source(build_credential(cfg.bili)).resolve_up(name)
        return {"items": items}

    @app.get("/api/browse/history")
    async def browse_history(
        verdict: str = "",
        action: str = "",
        q: str = "",
        limit: int = 40,
        offset: int = 0,
    ) -> dict:
        cfg = AppConfig.load()
        store = Store(cfg.data_dir / "biliauto.db")
        try:
            data = store.browse_history(
                verdict=verdict,
                action=action,
                query=(q or "").strip(),
                limit=max(1, min(limit, 200)),
                offset=max(0, offset),
            )
            data["stats"] = store.browse_stats()
            data["today"] = store.actions_today()
            data["dry_run"] = cfg.dry_run
            return data
        finally:
            store.close()

    @app.get("/api/browse/actions")
    async def browse_actions(limit: int = 30) -> dict:
        cfg = AppConfig.load()
        store = Store(cfg.data_dir / "biliauto.db")
        try:
            return {"items": store.recent_actions(limit)}
        finally:
            store.close()

    @app.get("/api/browse/report")
    async def browse_report() -> dict:
        """今日逛站报告：结构化的给界面渲染，Markdown 原文留给导出。"""
        cfg = AppConfig.load()
        store = Store(cfg.data_dir / "biliauto.db")
        try:
            items = store.browse_report(40)
        finally:
            store.close()

        path = cfg.data_dir / "reports" / f"browse-{time.strftime('%Y-%m-%d')}.md"
        markdown = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"items": items, "markdown": markdown, "exists": bool(items)}

    # ---------------- 观测 ----------------
    @app.get("/api/logs")
    async def logs(after: int = 0, limit: int = 200) -> dict:
        items = buffer.since(after) if after else buffer.tail(limit)
        return {"items": items[:limit], "last": buffer.last_seq}

    @app.get("/api/replies")
    async def replies(limit: int = 30) -> dict:
        cfg = AppConfig.load()
        store = Store(cfg.data_dir / "biliauto.db")
        try:
            return {"items": store.recent_replies(limit)}
        finally:
            store.close()

    @app.on_event("startup")
    async def _startup() -> None:
        # 勾了"自动开始值班"才自动跑，默认不偷偷动
        cfg = AppConfig.load()
        if cfg.comments.enabled and cfg.bili.has_credential:
            try:
                await comment_mgr.start(cfg)
                log.info("已按配置自动启动评论区值班")
            except Exception as e:
                log.warning("自动启动评论区值守失败：%s", e)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await manager.stop()
        await comment_mgr.stop()
        await browse_mgr.stop()

    return app


app = create_app()
