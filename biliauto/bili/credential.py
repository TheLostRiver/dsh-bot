"""AI 账号凭据构建与自检。

凭据只从 .env 读，不落日志、不进数据库。

注意：B 站 get_self_info 返回的字段是 mid / name，不是 uid / uname。
早期版本按 uid/uname 取会得到 None，这里统一归一化成 AccountInfo，
避免每个调用点各写一套兼容逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bilibili_api.utils.network import Credential

from ..config import BiliConfig

log = logging.getLogger("biliauto.cred")

# B 站节操值低于这个数会被限制发言
MORAL_FLOOR = 60


@dataclass
class AccountInfo:
    mid: int
    name: str
    level: int = 0
    moral: int = 100
    silence: int = 0
    is_tourist: bool = False

    @property
    def can_speak(self) -> bool:
        return not self.is_tourist and self.silence == 0 and self.moral >= MORAL_FLOOR

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.is_tourist:
            out.append("这是游客态，cookie 没真正登录")
        if self.silence:
            out.append("账号处于禁言状态，发弹幕会失败")
        if self.moral < MORAL_FLOOR:
            out.append(f"节操值 {self.moral} 偏低，可能被限制发言")
        if self.level <= 1:
            out.append("账号等级 1，属于新号，发弹幕被限流的概率很高（建议先养号）")
        return out

    def describe(self) -> str:
        return f"{self.name} (uid={self.mid}) Lv{self.level} 节操 {self.moral}"


def build_credential(cfg: BiliConfig) -> Credential | None:
    if not cfg.has_credential:
        return None
    return Credential(
        sessdata=cfg.sessdata or None,
        bili_jct=cfg.bili_jct or None,
        buvid3=cfg.buvid3 or None,
        dedeuserid=str(cfg.bot_uid) if cfg.bot_uid else None,
    )


async def verify_credential(credential: Credential | None) -> AccountInfo | None:
    """调一次自身信息接口，确认 cookie 没过期。返回归一化账号信息或 None。"""
    if credential is None:
        return None
    try:
        from bilibili_api import user

        raw = await user.get_self_info(credential)
    except Exception as e:
        log.warning("凭据校验失败（可能已过期）：%s", e)
        return None

    if not isinstance(raw, dict) or not raw.get("mid"):
        log.warning("凭据校验返回异常结构：%s", str(raw)[:200])
        return None

    return AccountInfo(
        mid=int(raw.get("mid")),
        name=str(raw.get("name") or raw.get("uname") or "未知"),
        level=int(raw.get("level") or 0),
        moral=int(raw.get("moral") or 100),
        silence=int(raw.get("silence") or 0),
        is_tourist=bool(raw.get("is_tourist")),
    )
