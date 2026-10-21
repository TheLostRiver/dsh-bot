"""互动动作执行器：点赞 / 投币 / 收藏 / 关注 / 发评论。

每个动作三道闸：
1. 配置开关（enabled）
2. 当日配额（per_day，写库，重启不清零）
3. 命中概率（probability，压总量）

另外点赞和收藏前会先查 `has_liked` / `has_favoured` —— B 站对"重复点赞"很敏感，
而且重复操作本身也没意义。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from bilibili_api import comment as bili_comment
from bilibili_api import favorite_list, user
from bilibili_api.video import Video

from ..bili.response import ok_result as _ok
from ..config import ActionSpec, BrowseConfig
from .source import Candidate

log = logging.getLogger("biliauto.browse.actions")

# 评论正文上限（接口限制，超了发不出去）
COMMENT_MAX_LEN = 60


@dataclass
class ActionResult:
    action: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"action": self.action, "ok": self.ok, "detail": self.detail}


class InteractionPerformer:
    def __init__(self, cfg: BrowseConfig, store, credential, dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.credential = credential
        self.dry_run = dry_run
        self._rng = random.Random()
        self._folder_id: int | None = None

    # ---------- 闸门 ----------
    def gate(self, name: str, spec: ActionSpec) -> tuple[bool, str]:
        if not spec.enabled:
            return False, "disabled"
        if self.store.action_used(name) >= spec.per_day:
            return False, "per_day"
        if self._rng.random() > spec.probability:
            return False, "miss"
        return True, "ok"

    def _video(self, cand: Candidate) -> Video:
        return Video(bvid=cand.bvid, credential=self.credential)

    # ---------- 动作 ----------
    async def like(self, cand: Candidate) -> ActionResult:
        ok, why = self.gate("like", self.cfg.like)
        if not ok:
            return ActionResult("like", False, why)
        v = self._video(cand)
        try:
            if await v.has_liked():
                return ActionResult("like", False, "already")
        except Exception as e:
            log.debug("查点赞状态失败：%s", e)

        if self.dry_run:
            log.info("[DRY-RUN] 点赞 %s %s", cand.bvid, cand.title[:20])
            return ActionResult("like", True, "dry_run")
        try:
            resp = await v.like(True)
        except Exception as e:
            return ActionResult("like", False, f"exception:{str(e)[:60]}")
        ok2, detail = _ok(resp)
        if ok2:
            self.store.incr_action("like")
            log.info("已点赞 %s %s", cand.bvid, cand.title[:20])
            return ActionResult("like", True, "ok")
        return ActionResult("like", False, detail)

    async def favorite(self, cand: Candidate) -> ActionResult:
        ok, why = self.gate("favorite", self.cfg.favorite)
        if not ok:
            return ActionResult("favorite", False, why)
        v = self._video(cand)
        try:
            if await v.has_favoured():
                return ActionResult("favorite", False, "already")
        except Exception as e:
            log.debug("查收藏状态失败：%s", e)

        folder = await self._ensure_folder()
        if folder is None:
            return ActionResult("favorite", False, "no_folder")

        if self.dry_run:
            log.info("[DRY-RUN] 收藏 %s 到「%s」", cand.bvid, self.cfg.favorite_folder)
            return ActionResult("favorite", True, "dry_run")
        try:
            resp = await v.set_favorite(add_media_ids=[folder])
        except Exception as e:
            return ActionResult("favorite", False, f"exception:{str(e)[:60]}")
        ok2, detail = _ok(resp)
        if ok2:
            self.store.incr_action("favorite")
            log.info("已收藏 %s", cand.bvid)
            return ActionResult("favorite", True, "ok")
        return ActionResult("favorite", False, detail)

    async def coin(self, cand: Candidate) -> ActionResult:
        """投币会真花掉账号硬币，所以默认关。"""
        ok, why = self.gate("coin", self.cfg.coin)
        if not ok:
            return ActionResult("coin", False, why)
        v = self._video(cand)
        if self.dry_run:
            log.info("[DRY-RUN] 投币 %s", cand.bvid)
            return ActionResult("coin", True, "dry_run")
        try:
            resp = await v.pay_coin(num=1, like=False)
        except Exception as e:
            return ActionResult("coin", False, f"exception:{str(e)[:60]}")
        ok2, detail = _ok(resp)
        if ok2:
            self.store.incr_action("coin")
            log.info("已投币 %s", cand.bvid)
            return ActionResult("coin", True, "ok")
        return ActionResult("coin", False, detail)

    async def follow(self, cand: Candidate) -> ActionResult:
        ok, why = self.gate("follow", self.cfg.follow)
        if not ok:
            return ActionResult("follow", False, why)
        if not cand.up_mid:
            return ActionResult("follow", False, "no_up_mid")
        if self.dry_run:
            log.info("[DRY-RUN] 关注 %s(%s)", cand.up_name, cand.up_mid)
            return ActionResult("follow", True, "dry_run")
        try:
            u = user.User(cand.up_mid, credential=self.credential)
            resp = await u.modify_relation(user.RelationType.SUBSCRIBE)
        except Exception as e:
            return ActionResult("follow", False, f"exception:{str(e)[:60]}")
        ok2, detail = _ok(resp)
        if ok2:
            self.store.incr_action("follow")
            log.info("已关注 %s", cand.up_name)
            return ActionResult("follow", True, "ok")
        return ActionResult("follow", False, detail)

    async def comment(self, cand: Candidate, text: str) -> ActionResult:
        ok, why = self.gate("comment", self.cfg.comment)
        if not ok:
            return ActionResult("comment", False, why)
        text = " ".join((text or "").split())[:COMMENT_MAX_LEN]
        if not text:
            return ActionResult("comment", False, "empty")
        if self.dry_run:
            log.info("[DRY-RUN] 评论 %s：%s", cand.bvid, text)
            return ActionResult("comment", True, "dry_run")
        try:
            resp = await bili_comment.send_comment(
                text,
                oid=cand.aid,
                type_=bili_comment.CommentResourceType.VIDEO,
                credential=self.credential,
            )
        except Exception as e:
            return ActionResult("comment", False, f"exception:{str(e)[:60]}")
        ok2, detail = _ok(resp)
        if ok2:
            self.store.incr_action("comment")
            log.info("已评论 %s：%s", cand.bvid, text)
            return ActionResult("comment", True, "ok")
        return ActionResult("comment", False, detail)

    # ---------- 收藏夹 ----------
    async def _ensure_folder(self) -> int | None:
        """找到（或创建）目标收藏夹。"""
        if self._folder_id is not None:
            return self._folder_id
        if not self.credential:
            return None
        try:
            uid = int(self.credential.dedeuserid or 0)
            if not uid:
                return None
            raw = await favorite_list.get_video_favorite_list(
                uid, credential=self.credential
            )
            for f in raw.get("list") or []:
                if str(f.get("title")) == self.cfg.favorite_folder:
                    self._folder_id = int(f.get("id"))
                    return self._folder_id
            if self.dry_run:
                return -1
            created = await favorite_list.create_video_favorite_list(
                self.cfg.favorite_folder,
                introduction="AI 自动收藏的喜欢",
                credential=self.credential,
            )
            data = created.get("data") or {}
            fid = data.get("id")
            if fid:
                self._folder_id = int(fid)
                log.info("已创建收藏夹「%s」id=%s", self.cfg.favorite_folder, fid)
                return self._folder_id
        except Exception as e:
            log.warning("准备收藏夹失败：%s", str(e)[:100])
        return None

