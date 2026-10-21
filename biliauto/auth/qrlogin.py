"""B 站扫码登录。

走的是 passport 网页端的二维码流程，实测可行的三条要点：
  ① GET  /x/passport-login/web/qrcode/generate  → data.url + data.qrcode_key
  ② 轮询 /x/passport-login/web/qrcode/poll?qrcode_key=…
     data.code: 86101 未扫 / 86090 已扫待确认 / 0 成功 / 86038 过期
  ③ **登录成功时凭证在轮询响应的 Set-Cookie 头里**，不在 data.url 里
     （data.url 是别的域的游戏票，在里面找 SESSDATA 会永远找不到）
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

STATE_TEXT = {
    "waiting": "等待扫码",
    "scanned": "已扫码，请在手机上确认",
    "ok": "登录成功",
    "expired": "二维码已过期，请刷新",
    "error": "登录异常",
}

# 二维码有效期约 180 秒，提前 10 秒判过期，避免用户扫了一张已经失效的码
QR_TTL = 170


@dataclass
class QrSession:
    key: str
    url: str
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > QR_TTL


@dataclass
class LoginResult:
    state: str
    message: str = ""
    credential: dict | None = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=20.0,
        follow_redirects=False,
    )


def _pick_cookies(set_cookie_headers: list[str]) -> dict:
    """从若干 Set-Cookie 头里抠出登录需要的四个字段。"""
    want = {
        "SESSDATA": "sessdata",
        "bili_jct": "bili_jct",
        "DedeUserID": "dedeuserid",
        "buvid3": "buvid3",
    }
    jar: dict[str, str] = {}
    for raw in set_cookie_headers or []:
        first = str(raw).split(";")[0]
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        key = want.get(name.strip())
        if key and value.strip():
            jar[key] = value.strip()
    return jar


async def _ensure_buvid3(client: httpx.AsyncClient, jar: dict) -> dict:
    """拿不到 buvid3 时补一张设备指纹（缺了会让风控评分变高）。"""
    if jar.get("buvid3"):
        return jar
    try:
        r = await client.get(SPI_URL)
        jar["buvid3"] = (r.json().get("data") or {}).get("b_3") or ""
    except Exception:
        pass
    return jar


async def generate() -> QrSession:
    async with _client() as c:
        r = await c.get(GEN_URL)
        payload = r.json()
    data = payload.get("data") or {}
    if not data.get("qrcode_key") or not data.get("url"):
        raise RuntimeError(
            f"生成二维码失败：{payload.get('message') or '返回里没有 qrcode_key'}"
        )
    return QrSession(key=data["qrcode_key"], url=data["url"])


async def poll(key: str) -> LoginResult:
    async with _client() as c:
        r = await c.get(
            POLL_URL, params={"qrcode_key": key, "source": "main-fe-header"}
        )
        try:
            body = r.json()
        except Exception:
            body = {}
        data = body.get("data") or {}
        code = data.get("code")

        if code == 0:
            # 关键：凭证在 Set-Cookie 里
            jar = _pick_cookies(r.headers.get_list("set-cookie"))
            jar = await _ensure_buvid3(c, jar)
            if not jar.get("sessdata"):
                return LoginResult(
                    state="error",
                    message="登录成功但响应里没有 SESSDATA，请重试或改用 cookie 手填",
                )
            if not jar.get("bili_jct"):
                return LoginResult(state="error", message="响应里缺少 bili_jct，无法发弹幕")
            return LoginResult(state="ok", message="登录成功", credential=jar)

    mapping = {86101: "waiting", 86090: "scanned", 86038: "expired"}
    state = mapping.get(code, "error")
    msg = data.get("message") or body.get("message") or STATE_TEXT.get(state, "")
    return LoginResult(state=state, message=msg)


def render_qr_png_data_url(url: str) -> str:
    """把登录 URL 画成二维码，返回可直接塞进 <img src> 的 data URL。"""
    import base64

    import qrcode

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"
