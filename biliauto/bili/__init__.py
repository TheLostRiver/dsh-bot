"""上游 B 站适配层。把第三方 SDK 的细节关在这一层里面。"""

from .credential import AccountInfo, build_credential, verify_credential
from .danmaku import LiveClient, LiveEvent
from .actions import DanmakuSender, Speaker

__all__ = [
    "AccountInfo",
    "build_credential",
    "verify_credential",
    "LiveClient",
    "LiveEvent",
    "DanmakuSender",
    "Speaker",
]
