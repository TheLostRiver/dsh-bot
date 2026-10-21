"""配置层：从 .env 读取，集中校验，避免散落的 os.environ 读取。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from .auth.vault import load_credential, load_settings

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = _str(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


# 注意：正则里会有 `{1,3}` 这种逗号，不能按逗号盲切
DEFAULT_IGNORE_PATTERNS = (
    r"^[0-9]{1,3}$",
    r"^(哈+|6+|awsl|233+)$",
    r"^\s*$",
)


def _split_patterns(raw: str) -> tuple[str, ...]:
    """按逗号切分，但跳过 `{m,n}` 里的逗号；也支持换行分隔。"""
    parts: list[str] = []
    buf = ""
    depth = 0
    for ch in raw:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if ch in ",\n" and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return tuple(p.strip() for p in parts if p.strip())


def _ints(name: str, default: str = "") -> tuple[int, ...]:
    """解析 "4,36,188" 这种分区 tid 列表，顺手容忍中文逗号。"""
    raw = _str(name, default)
    out: list[int] = []
    for x in raw.replace("，", ",").split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return tuple(out)


def _patterns(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _str(name)
    return _split_patterns(raw) if raw else default


@dataclass(frozen=True)
class BiliConfig:
    room_id: int
    sessdata: str
    bili_jct: str
    buvid3: str
    bot_uid: int | None

    @property
    def has_credential(self) -> bool:
        return bool(self.sessdata and self.bili_jct)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    timeout: float

    @property
    def usable(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class PersonaConfig:
    name: str
    bio: str
    style: str
    catchphrase: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class TriggerConfig:
    reply_probability: float
    reply_on_question: bool
    ignore_patterns: tuple[str, ...]


@dataclass(frozen=True)
class GuardConfig:
    min_interval: float
    per_minute: int
    per_hour: int
    per_day: int
    user_cooldown: float
    max_danmaku_len: int


@dataclass(frozen=True)
class CommentConfig:
    enabled: bool
    up_mid: int          # 0 = 自动从 BILI_ROOM_ID 推导主播 UID
    videos: int          # 盯最近几个视频
    poll_interval: float  # 每轮扫描间隔（秒）
    lookback_hours: float  # 只回这么久以内的新评论
    max_per_day: int
    min_interval: float
    max_len: int
    reply_probability: float


@dataclass(frozen=True)
class ActionSpec:
    """一个可开关的互动动作：独立开关 + 日配额 + 命中概率。"""

    enabled: bool = False
    per_day: int = 10
    probability: float = 0.3


@dataclass(frozen=True)
class BrowseConfig:
    """自动逛 B 站：按分区/关键词找视频，看完再决定要不要互动。"""

    enabled: bool
    interval: float
    batch: int            # 每轮挑几条候选
    max_llm: int          # 每轮最多读几条字幕（烧 token 的大头）
    zones: tuple[str, ...]   # RankType 名字，如 Game / Knowledge / Technology
    keywords: tuple[str, ...]
    days: int
    order: str            # pubdate / click / dm
    max_subtitle_chars: int
    favorite_folder: str
    like: ActionSpec
    coin: ActionSpec
    favorite: ActionSpec
    follow: ActionSpec
    comment: ActionSpec


@dataclass(frozen=True)
class SpeechConfig:
    enabled: bool
    backend: str
    voice: str
    rate: str
    volume: str
    pitch: str
    device: int | None
    max_chars: int
    out_dir: Path


@dataclass(frozen=True)
class AppConfig:
    bili: BiliConfig
    llm: LLMConfig
    persona: PersonaConfig
    trigger: TriggerConfig
    guard: GuardConfig
    comments: CommentConfig
    browse: BrowseConfig
    speech: SpeechConfig
    dry_run: bool
    log_level: str
    data_dir: Path
    root: Path = field(default=ROOT)

    @classmethod
    def _from_env(cls) -> "AppConfig":
        data_dir = ROOT / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        device_raw = _str("SPEECH_DEVICE")
        try:
            device = int(device_raw) if device_raw else None
        except ValueError:
            device = None

        return cls(
            bili=BiliConfig(
                room_id=_int("BILI_ROOM_ID", 0),
                sessdata=_str("BILI_SESSDATA"),
                bili_jct=_str("BILI_BILI_JCT"),
                buvid3=_str("BILI_BUVID3"),
                bot_uid=(_int("BILI_BOT_UID", 0) or None),
            ),
            llm=LLMConfig(
                api_key=_str("DEEPSEEK_API_KEY"),
                base_url=_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                model=_str("DEEPSEEK_MODEL", "deepseek-chat"),
                temperature=_float("DEEPSEEK_TEMPERATURE", 1.1),
                max_tokens=_int("DEEPSEEK_MAX_TOKENS", 400),
                timeout=_float("DEEPSEEK_TIMEOUT", 30.0),
            ),
            persona=PersonaConfig(
                name=_str("PERSONA_NAME", "小深"),
                bio=_str("PERSONA_BIO", "B 站直播间里的 AI 粉丝。"),
                style=_str("PERSONA_STYLE", "口语化、像普通网友"),
                catchphrase=_str("PERSONA_CATCHPHRASE"),
                aliases=_csv("ALIASES", "小深,ai,机器人"),
            ),
            trigger=TriggerConfig(
                reply_probability=min(max(_float("REPLY_PROBABILITY", 0.12), 0.0), 1.0),
                reply_on_question=_bool("REPLY_ON_QUESTION", True),
                ignore_patterns=_patterns(
                    "IGNORE_PATTERNS", DEFAULT_IGNORE_PATTERNS
                ),
            ),
            guard=GuardConfig(
                min_interval=_float("SEND_MIN_INTERVAL", 12.0),
                per_minute=_int("SEND_PER_MINUTE", 3),
                per_hour=_int("SEND_PER_HOUR", 25),
                per_day=_int("SEND_PER_DAY", 180),
                user_cooldown=_float("USER_COOLDOWN", 240.0),
                max_danmaku_len=_int("MAX_DANMAKU_LEN", 20),
            ),
            comments=CommentConfig(
                enabled=_bool("COMMENT_ENABLED", False),
                up_mid=_int("COMMENT_UP_MID", 0),
                videos=_int("COMMENT_VIDEOS", 5),
                poll_interval=_float("COMMENT_POLL_INTERVAL", 300.0),
                lookback_hours=_float("COMMENT_LOOKBACK_HOURS", 72.0),
                max_per_day=_int("COMMENT_MAX_PER_DAY", 20),
                min_interval=_float("COMMENT_MIN_INTERVAL", 90.0),
                max_len=_int("COMMENT_MAX_LEN", 60),
                reply_probability=min(
                    max(_float("COMMENT_REPLY_PROBABILITY", 0.5), 0.0), 1.0
                ),
            ),
            browse=BrowseConfig(
                enabled=_bool("BROWSE_ENABLED", False),
                interval=_float("BROWSE_INTERVAL", 1800.0),
                batch=_int("BROWSE_BATCH", 6),
                max_llm=_int("BROWSE_MAX_LLM", 3),
                zones=_csv("BROWSE_ZONES", ""),
                keywords=_csv("BROWSE_KEYWORDS", ""),
                days=_int("BROWSE_DAYS", 7),
                order=_str("BROWSE_ORDER", "pubdate").lower(),
                max_subtitle_chars=_int("BROWSE_MAX_SUBTITLE_CHARS", 6000),
                favorite_folder=_str("BROWSE_FAVORITE_FOLDER", "AI 精选"),
                # 点赞低风险默认开；投币要花硬币、关注改关系，默认关
                like=ActionSpec(True, _int("BROWSE_LIKE_PER_DAY", 20), 0.7),
                coin=ActionSpec(False, 5, 0.2),
                favorite=ActionSpec(False, _int("BROWSE_FAV_PER_DAY", 10), 0.3),
                follow=ActionSpec(False, 2, 0.1),
                comment=ActionSpec(False, 5, 0.3),
            ),
            speech=SpeechConfig(
                enabled=_bool("SPEECH_ENABLED", True),
                backend=_str("SPEECH_BACKEND", "file").lower(),
                voice=_str("TTS_VOICE", "zh-CN-XiaoxiaoNeural"),
                rate=_str("TTS_RATE", "+8%"),
                volume=_str("TTS_VOLUME", "+0%"),
                pitch=_str("TTS_PITCH", "+0Hz"),
                device=device,
                max_chars=_int("SPEECH_MAX_CHARS", 80),
                out_dir=data_dir / "speech",
            ),
            dry_run=_bool("DRY_RUN", False),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            data_dir=data_dir,
        )

    @classmethod
    def load(cls) -> "AppConfig":
        """优先级：settings.json / credentials.json（后台 UI 改的） > .env。

        这样命令行和 Web 后台读的是同一份生效配置，不会出现
        "后台里改了人设，命令行跑的还是老的"。
        """
        cfg = cls._from_env()
        cfg = _apply_credential(cfg, load_credential(cfg.data_dir))
        cfg = _apply_settings(cfg, load_settings(cfg.data_dir))
        return cfg

    def validate(self) -> list[str]:
        """返回阻断性问题列表；空列表表示可以真实开播。"""
        problems: list[str] = []
        if self.bili.room_id <= 0:
            problems.append("BILI_ROOM_ID 未填写（目标直播间房号）")
        if self.dry_run:
            return problems
        if not self.bili.has_credential:
            problems.append("BILI_SESSDATA / BILI_BILI_JCT 未填写，AI 账号无法发言")
        if not self.bili.buvid3:
            problems.append("BILI_BUVID3 未填写，风控概率会明显升高（建议补上）")
        if not self.llm.usable:
            problems.append("DEEPSEEK_API_KEY 未填写，AI 无法思考")
        return problems


def _num(src: dict, key: str, default, kind=float):
    v = src.get(key)
    if v is None or v == "":
        return default
    try:
        return kind(v)
    except (TypeError, ValueError):
        return default


def _txt(src: dict, key: str, default: str) -> str:
    v = src.get(key)
    return default if v is None else str(v)


def _bol(src: dict, key: str, default: bool) -> bool:
    v = src.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _lst(src: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    v = src.get(key)
    if v is None:
        return default
    if isinstance(v, str):
        return tuple(x.strip() for x in v.split(",") if x.strip())
    try:
        return tuple(str(x).strip() for x in v if str(x).strip())
    except TypeError:
        return default


def _sub(src: dict, key: str) -> dict:
    v = src.get(key)
    return v if isinstance(v, dict) else {}


def _apply_credential(cfg: AppConfig, cred: dict) -> AppConfig:
    if not cred:
        return cfg
    sessdata = str(cred.get("sessdata") or "").strip()
    bili_jct = str(cred.get("bili_jct") or "").strip()
    if not sessdata or not bili_jct:
        return cfg
    buvid3 = str(cred.get("buvid3") or "").strip() or cfg.bili.buvid3
    mid_raw = str(cred.get("dedeuserid") or "").strip()
    bot_uid = int(mid_raw) if mid_raw.isdigit() else cfg.bili.bot_uid
    return replace(
        cfg,
        bili=replace(
            cfg.bili,
            sessdata=sessdata,
            bili_jct=bili_jct,
            buvid3=buvid3,
            bot_uid=bot_uid,
        ),
    )


def _action(src: dict, key: str, default: ActionSpec) -> ActionSpec:
    d = _sub(src, key)
    if not d:
        return default
    return ActionSpec(
        enabled=_bol(d, "enabled", default.enabled),
        per_day=max(0, _num(d, "per_day", default.per_day, int)),
        probability=min(max(_num(d, "probability", default.probability), 0.0), 1.0),
    )


def _apply_settings(cfg: AppConfig, s: dict) -> AppConfig:
    if not s:
        return cfg
    persona = _sub(s, "persona")
    trigger = _sub(s, "trigger")
    guard = _sub(s, "guard")
    speech = _sub(s, "speech")
    comments = _sub(s, "comments")
    browse = _sub(s, "browse")

    return replace(
        cfg,
        bili=replace(cfg.bili, room_id=_num(s, "room_id", cfg.bili.room_id, int)),
        persona=replace(
            cfg.persona,
            name=_txt(persona, "name", cfg.persona.name),
            bio=_txt(persona, "bio", cfg.persona.bio),
            style=_txt(persona, "style", cfg.persona.style),
            catchphrase=_txt(persona, "catchphrase", cfg.persona.catchphrase),
            aliases=_lst(persona, "aliases", cfg.persona.aliases),
        ),
        trigger=replace(
            cfg.trigger,
            reply_probability=min(
                max(
                    _num(trigger, "reply_probability", cfg.trigger.reply_probability),
                    0.0,
                ),
                1.0,
            ),
            reply_on_question=_bol(
                trigger, "reply_on_question", cfg.trigger.reply_on_question
            ),
            ignore_patterns=_lst(
                trigger, "ignore_patterns", cfg.trigger.ignore_patterns
            ),
        ),
        guard=replace(
            cfg.guard,
            min_interval=_num(guard, "min_interval", cfg.guard.min_interval),
            per_minute=_num(guard, "per_minute", cfg.guard.per_minute, int),
            per_hour=_num(guard, "per_hour", cfg.guard.per_hour, int),
            per_day=_num(guard, "per_day", cfg.guard.per_day, int),
            user_cooldown=_num(guard, "user_cooldown", cfg.guard.user_cooldown),
            max_danmaku_len=_num(
                guard, "max_danmaku_len", cfg.guard.max_danmaku_len, int
            ),
        ),
        comments=replace(
            cfg.comments,
            enabled=_bol(comments, "enabled", cfg.comments.enabled),
            up_mid=_num(comments, "up_mid", cfg.comments.up_mid, int),
            videos=_num(comments, "videos", cfg.comments.videos, int),
            poll_interval=_num(
                comments, "poll_interval", cfg.comments.poll_interval
            ),
            lookback_hours=_num(
                comments, "lookback_hours", cfg.comments.lookback_hours
            ),
            max_per_day=_num(comments, "max_per_day", cfg.comments.max_per_day, int),
            min_interval=_num(comments, "min_interval", cfg.comments.min_interval),
            max_len=_num(comments, "max_len", cfg.comments.max_len, int),
            reply_probability=min(
                max(
                    _num(
                        comments,
                        "reply_probability",
                        cfg.comments.reply_probability,
                    ),
                    0.0,
                ),
                1.0,
            ),
        ),
        browse=replace(
            cfg.browse,
            enabled=_bol(browse, "enabled", cfg.browse.enabled),
            interval=_num(browse, "interval", cfg.browse.interval),
            batch=_num(browse, "batch", cfg.browse.batch, int),
            max_llm=_num(browse, "max_llm", cfg.browse.max_llm, int),
            zones=(
                tuple(str(x).strip() for x in browse.get("zones", []) if str(x).strip())
                if "zones" in browse
                else cfg.browse.zones
            ),
            keywords=(
                tuple(str(x).strip() for x in browse.get("keywords", []) if str(x).strip())
                if "keywords" in browse
                else cfg.browse.keywords
            ),
            days=_num(browse, "days", cfg.browse.days, int),
            order=_txt(browse, "order", cfg.browse.order),
            max_subtitle_chars=_num(
                browse, "max_subtitle_chars", cfg.browse.max_subtitle_chars, int
            ),
            favorite_folder=_txt(
                browse, "favorite_folder", cfg.browse.favorite_folder
            ),
            like=_action(browse, "like", cfg.browse.like),
            coin=_action(browse, "coin", cfg.browse.coin),
            favorite=_action(browse, "favorite", cfg.browse.favorite),
            follow=_action(browse, "follow", cfg.browse.follow),
            comment=_action(browse, "comment", cfg.browse.comment),
        ),
        speech=replace(
            cfg.speech,
            enabled=_bol(speech, "enabled", cfg.speech.enabled),
            backend=_txt(speech, "backend", cfg.speech.backend),
            voice=_txt(speech, "voice", cfg.speech.voice),
            rate=_txt(speech, "rate", cfg.speech.rate),
            pitch=_txt(speech, "pitch", cfg.speech.pitch),
            max_chars=_num(speech, "max_chars", cfg.speech.max_chars, int),
        ),
        dry_run=_bol(s, "dry_run", cfg.dry_run),
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # bilibili_api 的心跳日志很吵，且 LiveDanmaku 会用自己的 logger
    # （名字带房间号，事先拿不到），所以这里用过滤器统一压掉
    for name in ("bilibili_api", "LiveDanmaku", "httpx", "openai", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)

    class _NoiseFilter(logging.Filter):
        NOISY = ("LiveDanmaku", "bilibili_api", "httpx", "openai", "websockets")

        def filter(self, record: logging.LogRecord) -> bool:
            return not record.name.startswith(self.NOISY)

    for handler in logging.getLogger().handlers:
        handler.addFilter(_NoiseFilter())
