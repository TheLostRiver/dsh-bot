"""统一的「操作是否成功」判定。

踩过的坑：`bilibili_api` 的操作类接口（发弹幕/点赞/评论…）**内部已经检查过 code 并抛异常**，
成功时返回的是响应里的 `data` 字段（例如 `{"like": 8}`、`{"mode_info": {...}}`），
**根本不含 `code`**。

所以写 `resp["code"] == 0` 会把每一次成功都判成失败。这个 bug 在本项目里出现过三次
（点赞、评论、发弹幕），所以统一收在这里，只留一份实现。
"""

from __future__ import annotations


def ok_result(resp) -> tuple[bool, str]:
    """没抛异常就是成功；返回里恰好带 code 才再核对一次。"""
    if isinstance(resp, dict):
        code = resp.get("code")
        if code is not None:
            try:
                if int(code) != 0:
                    return False, f"code:{code}"
            except (TypeError, ValueError):
                pass
    return True, "ok"


def error_of(exc: Exception) -> tuple[int, str]:
    """从异常里抠出 B 站错误码和消息，方便判断是不是风控。"""
    code = getattr(exc, "code", 0) or 0
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    return code, str(exc)[:160]


# 被风控时会出现的错误码
RISK_CODES = (-412, 412, -352, 352)


def is_risk(code: int) -> bool:
    return code in RISK_CODES
