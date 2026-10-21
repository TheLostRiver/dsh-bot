"""评论区值班：轮询 UP 主视频下的新评论，用 AI 账号接话。"""

from .collector import Collector, CollectorError, CommentEvent, VideoRef
from .duty import CommentDuty, ScanResult

__all__ = [
    "Collector",
    "CollectorError",
    "CommentEvent",
    "VideoRef",
    "CommentDuty",
    "ScanResult",
]
