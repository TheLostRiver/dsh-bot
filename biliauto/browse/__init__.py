"""自动逛 B 站：分区/关键词找视频 → 读字幕 → 判断 → 互动（点赞/投币/收藏/关注/评论）。"""

from .actions import ActionResult, InteractionPerformer
from .runner import BrowseRunner, RoundResult
from .source import Candidate, Source

__all__ = [
    "ActionResult",
    "InteractionPerformer",
    "BrowseRunner",
    "RoundResult",
    "Candidate",
    "Source",
]
