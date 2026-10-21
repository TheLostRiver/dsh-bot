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

from fastapi import FastAPI, HTTPException
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
            "max_chars": cfg.speech.max_chars,
        },
    }


def create_app() -> FastAPI:
    setup_logging(AppConfig.load().log_level)

    app = FastAPI(title="dsh-bot 管理后台", docs_url=None, redoc_url=None)
    buffer = LogBuffer()
    logging.getLogger().addHandler(buffer)
    manager = BotManager()

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

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await manager.stop()

    return app


app = create_app()
