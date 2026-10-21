"""核心逻辑自测（不联网、不需要账号、不需要 pytest）。

    python tests/test_core.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biliauto.bili.danmaku import LiveEvent, parse_event  # noqa: E402
from biliauto.config import GuardConfig, TriggerConfig  # noqa: E402
from biliauto.llm import Brain  # noqa: E402
from biliauto.live.guard import DanmakuBudget, TriggerPolicy  # noqa: E402
from biliauto.memory.store import Store  # noqa: E402

PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise AssertionError(name)


def make_policy(prob: float = 0.0) -> TriggerPolicy:
    cfg = TriggerConfig(
        reply_probability=prob,
        reply_on_question=True,
        ignore_patterns=(r"^[0-9]{1,3}$", r"^(哈+|6+|233+)3*$"),
    )
    return TriggerPolicy(cfg, ("小深", "ai"), bot_uid=999)


def test_policy() -> None:
    print("\n[触发策略]")
    p = make_policy()

    d = p.classify(1, "甲", "主播今天播到几点？")
    check("提问句必回且高优", d.action == "consider" and d.priority == 1, str(d))

    d = p.classify(2, "乙", "小深你在吗")
    check("被叫名字必回", d.action == "consider" and d.reason == "called", str(d))

    d = p.classify(999, "自己", "我在说话")
    check("不回复自己", d.action == "ignore" and d.reason == "self", str(d))

    d = p.classify(3, "丙", "666")
    check("刷屏数字被过滤", d.action == "ignore" and d.reason == "noise", str(d))

    d = p.classify(4, "丁", "哈哈哈")
    check("无意义叠字被过滤", d.action == "ignore", str(d))

    d = p.classify(5, "戊", "今天天气不错")
    check("低概率下普通闲聊不打扰", d.action == "ignore", str(d))

    d = p.classify(6, "富哥", "打赏了", "SUPER_CHAT_MESSAGE")
    check("SC 最高优先", d.action == "consider" and d.priority == 2, str(d))

    d = p.classify(7, "新人", "开通了舰长", "GUARD_BUY")
    check("上舰最高优先", d.action == "consider" and d.priority == 2, str(d))


def test_budget() -> None:
    print("\n[发送配额]")
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(tmp)
    cfg = GuardConfig(
        min_interval=10, per_minute=2, per_hour=5, per_day=3,
        user_cooldown=60, max_danmaku_len=20,
    )
    b = DanmakuBudget(cfg, store, dry_run=False)
    now = time.time()

    ok, wait, _ = b.evaluate(1, now)
    check("初始可发", ok and wait == 0)

    b.note_sent(1, now)
    ok, wait, reason = b.evaluate(2, now + 1)
    check("冷却期内不发并给出等待时间",
          not ok and reason == "cooldown" and 8 < wait <= 10, f"{ok} {wait} {reason}")

    ok, _, _ = b.evaluate(2, now + 11)
    check("冷却结束后可发", ok)

    ok, _, reason = b.evaluate(1, now + 11)
    check("同一观众冷却生效", not ok and reason == "user_cooldown", reason)

    # 每日上限是硬墙，等待也没用
    for i in range(3):
        store.incr_usage("danmaku_sent")
    b2 = DanmakuBudget(cfg, store, dry_run=False)
    ok, wait, reason = b2.evaluate(3, now)
    check("日上限硬拦截", not ok and reason == "per_day" and wait == float("inf"), reason)

    # dry-run 不消耗真实配额
    b3 = DanmakuBudget(cfg, store, dry_run=True)
    ok, _, _ = b3.evaluate(3, now)
    check("dry-run 不受日上限限制", ok)
    store.close()


def test_llm_coerce() -> None:
    print("\n[模型输出规整]")
    r = Brain._coerce(
        {"should_reply": True, "danmaku": "这句话故意写得很长很长很长超过二十个字了吧", "speech": "完整稿"},
        max_danmaku_len=20,
    )
    check("超长弹幕被截断到 20 字", len(r.danmaku) == 20, f"len={len(r.danmaku)}")
    check("语音稿保留完整", r.speech == "完整稿")

    r = Brain._coerce({"should_reply": False, "danmaku": "x", "speech": "y"}, 20)
    check("模型选择沉默", r.should_reply is False)

    r = Brain._coerce({"should_reply": True, "danmaku": "", "speech": ""}, 20)
    check("空回复视为沉默，避免发空弹幕", r.should_reply is False)

    r = Brain._coerce({"should_reply": True, "danmaku": "", "speech": "只有语音"}, 20)
    check("只有语音时用语音头兜底弹幕", r.danmaku == "只有语音")

    from biliauto.llm import _extract_json

    check("能解析被 ``` 包裹的 JSON",
          _extract_json('```json\n{"a":1}\n```') == {"a": 1})
    check("能解析带前言的 JSON",
          (_extract_json('好的，结果如下：{"a": 2}') or {}).get("a") == 2)
    check("脏输出返回 None", _extract_json("完全不是 json") is None)


def test_parse() -> None:
    print("\n[弹幕解析]")
    raw = {
        "info": [
            [0, 1, 25, 16777215],
            "好家伙这也太强了",
            [12345, "测试用户", 0, 0, 0, 10000, 1, ""],
            [12, "主播名", 888, "粉丝牌", 0],
            [10, 0, 0, 0, 0],
        ]
    }
    e = parse_event("DANMU_MSG", raw)
    check("解析出内容", e is not None and e.text == "好家伙这也太强了")
    check("解析出 UID", e.uid == 12345, str(e.uid))
    check("解析出用户名", e.uname == "测试用户")
    check("解析出粉丝牌等级", e.medal_level == 12, str(e.medal_level))

    check("空消息返回 None", parse_event("DANMU_MSG", {"info": []}) is None)
    check("残缺结构不崩溃", parse_event("DANMU_MSG", {"info": ["x", "有内容"]}) is not None)

    sc = parse_event("SUPER_CHAT_MESSAGE", {
        "data": {"uid": 1, "message": "加油", "user_info": {"uname": "老板"}}
    })
    check("SC 被识别为付费事件", sc is not None and sc.is_paid, str(sc))
    check("SC 文本正确", sc.text == "加油")


def test_store() -> None:
    print("\n[持久化]")
    tmp = Path(tempfile.mkdtemp()) / "s.db"
    s = Store(tmp)
    eid = s.log_event("DANMU_MSG", 1, "甲", "你好")
    check("事件落库", eid > 0)

    s.touch_viewer(1, "甲")
    s.touch_viewer(1, "甲")
    prof = s.viewer_profile(1)
    check("观众发言计数", prof and prof["msg_count"] == 2, str(prof))

    s.log_reply(eid, 1, "回一句", "回一句语音", True)
    stats = s.reply_stats()
    check("回复统计", stats["total"] == 1 and stats["ok"] == 1, str(stats))

    s.incr_usage("danmaku_sent")
    check("用量累加", s.today_usage()["danmaku_sent"] == 1)

    s.kv_set("k", "v")
    check("KV 读写", s.kv_get("k") == "v")
    s.close()


def test_patterns() -> None:
    print("\n[忽略规则解析]")
    from biliauto.config import DEFAULT_IGNORE_PATTERNS, _patterns, _split_patterns

    # 这是真踩过的坑：{1,3} 里的逗号不能当分隔符
    got = _split_patterns(r"^[0-9]{1,3}$,^abc$")
    check("保留 {1,3} 里的逗号", got == (r"^[0-9]{1,3}$", "^abc$"), str(got))

    got = _split_patterns("^a$\n^b$")
    check("支持换行分隔", got == ("^a$", "^b$"), str(got))

    for pat in DEFAULT_IGNORE_PATTERNS:
        import re

        re.compile(pat)
    check("内置规则全部是合法正则", True)

    pats = _patterns("IGNORE_PATTERNS__NOT_SET", DEFAULT_IGNORE_PATTERNS)
    check("未配置时回落到默认规则", pats == DEFAULT_IGNORE_PATTERNS, str(pats))


def test_comments() -> None:
    print("\n[评论区值班]")
    from dataclasses import replace

    from biliauto.comments.collector import CommentEvent
    from biliauto.comments.duty import CommentBudget, CommentPolicy
    from biliauto.config import AppConfig

    cfg = AppConfig.load()
    cc = replace(cfg.comments, lookback_hours=24, reply_probability=0.0)
    pol = CommentPolicy(cc, cfg.persona, bot_uid=999)
    pol.up_name = "主播名"

    def ev(text, uid=1, top=True, age_h=1.0):
        return CommentEvent(
            rpid=1, oid=1, uid=uid, uname="甲", text=text,
            ctime=time.time() - age_h * 3600, is_top_level=top, reply_count=0,
        )

    a, r = pol.classify(ev("你好"))
    check("纯表情被跳过", pol.classify(ev("[打call]"))[0] == "ignore")
    check("广告被跳过", pol.classify(ev("加群私信我，代练优惠"))[1] == "ad")
    check("楼中楼不参与", pol.classify(ev("同意", top=False))[1] == "nested")
    check("自己的评论不回", pol.classify(ev("我说一句", uid=999))[1] == "self")
    check("过老的评论不回", pol.classify(ev("这视频真好", age_h=100))[1] == "too_old")
    check("太短的不回", pol.classify(ev("好"))[1] == "too_short")
    check("提问必回", pol.classify(ev("这个配装怎么配啊？"))[0] == "reply")
    check("提到主播必回", pol.classify(ev("主播名这波操作太秀了"))[0] == "reply")

    # 配额
    tmp = Path(tempfile.mkdtemp()) / "c.db"
    s = Store(tmp)
    cc2 = replace(cfg.comments, min_interval=60, max_per_day=1)
    b = CommentBudget(cc2, s, dry_run=False)
    ok, _, _ = b.can_send(time.time())
    check("初始可发", ok)
    b.note_sent(time.time())
    ok, wait, why = b.can_send(time.time() + 1)
    check("冷却拦截", not ok and why == "cooldown" and wait > 50, f"{ok} {wait} {why}")

    s.incr_usage("comment_sent")
    b2 = CommentBudget(cc2, s, dry_run=False)
    ok, wait, why = b2.can_send(time.time() + 120)
    check("每日上限硬拦截", not ok and why == "per_day", f"{ok} {why}")

    # 去重与记录
    check("未见过", s.seen_comment(12345) is False)
    s.record_comment(12345, 1, 2, "乙", "好看", time.time(), "considered", "question")
    check("记录后算见过", s.seen_comment(12345) is True)
    s.log_comment_reply(12345, 1, 2, "好看", "确实好看", True)
    stats = s.comment_stats()
    check("评论回复统计", stats["replied"] == 1 and stats["seen"] == 1, str(stats))
    check("评论回复可回读", len(s.recent_comment_replies(5)) == 1)
    s.close()


def test_browse() -> None:
    print("\n[自动逛 B 站]")
    from dataclasses import replace

    from biliauto.browse.actions import InteractionPerformer
    from biliauto.browse.source import Candidate, _duration_seconds
    from biliauto.config import ActionSpec, AppConfig

    # duration 有时是 "12:34"，有时是秒数
    check("解析 mm:ss", _duration_seconds("12:34") == 754)
    check("解析 hh:mm:ss", _duration_seconds("1:02:03") == 3723)
    check("解析纯秒", _duration_seconds("90") == 90)
    check("脏数据归零", _duration_seconds("abc") == 0)

    cfg = AppConfig.load()
    bc = replace(
        cfg.browse,
        like=ActionSpec(True, 2, 1.0),
        coin=ActionSpec(False, 5, 1.0),
        favorite=ActionSpec(True, 2, 0.0),
    )
    tmp = Path(tempfile.mkdtemp()) / "b.db"
    store = Store(tmp)
    perf = InteractionPerformer(bc, store, None, dry_run=True)

    ok, why = perf.gate("like", bc.like)
    check("开启且未超限可执行", ok and why == "ok", why)

    ok, why = perf.gate("coin", bc.coin)
    check("关闭的动作直接跳过", not ok and why == "disabled", why)

    ok, why = perf.gate("favorite", bc.favorite)
    check("概率 0 时跳过", not ok and why == "miss", why)

    store.incr_action("like")
    store.incr_action("like")
    ok, why = perf.gate("like", bc.like)
    check("达到日上限后拦截", not ok and why == "per_day", why)

    check("按动作分别计量", store.action_used("like") == 2 and store.action_used("coin") == 0)

    # 记录与统计
    store.record_browse("BV1xx", 1, "标题", 2, "UP", 300, 5000, time.time(),
                        "like", "值得一看")
    check("看过的视频被记住", store.browse_seen("BV1xx") is True)
    store.log_action("BV1xx", "like", True, "ok")
    stats = store.browse_stats()
    check("动作统计", stats["seen"] == 1 and stats["actions_ok"] == 1, str(stats))

    # 候选对象的年龄
    old = Candidate("BV1old", 1, "老视频", 2, "UP", 300, 5000, time.time() - 3600 * 48)
    fresh = Candidate("BV1new", 1, "新视频", 2, "UP", 300, 5000, time.time() - 3600)
    check("年龄计算正确", 47 < old.age_hours < 49 and fresh.age_hours < 2)
    store.close()

    # 四种来源都不填时必须回落到热门，而不是罢工
    import asyncio as _asyncio

    from biliauto.browse.source import Source

    empty = replace(cfg.browse, keywords=(), zones=(), ups=(), topics=())
    src = Source(None)
    calls = {"hot": 0}

    async def fake_hot(limit):
        calls["hot"] += 1
        return [Candidate("BVhot", 1, "热门", 2, "UP", 300, 9000, time.time(), source="hot")]

    src._hot = fake_hot
    got = _asyncio.run(src.discover(empty, 5))
    check("空配置回落热门", len(got) == 1 and got[0].source == "hot", str(len(got)))
    check("确实调用了热门来源", calls["hot"] == 1)


    # 定向来源不能被全局播放门槛砍掉（搜索出来的常常是个位数播放）
    from biliauto.browse.runner import filter_candidate

    low = Candidate("BVlo", 1, "小众", 2, "UP", 300, 5, time.time())
    for src in ("search:大肥鱼", "topic:怪物猎人", "up:123"):
        c = replace(low, source=src)
        check(f"{src} 不被播放门槛拦", filter_candidate(c, cfg.browse) is None)
    check("分区低播放会被拦",
          filter_candidate(replace(low, source="rank:Game"), cfg.browse) == "low_play")
    check("热门低播放会被拦",
          filter_candidate(replace(low, source="hot"), cfg.browse) == "low_play")
    check("太短一律跳过",
          filter_candidate(replace(low, source="search:x", duration=10), cfg.browse)
          == "too_short")


def test_browse_rebuild() -> None:
    """手动逛一轮必须用最新配置重建 runner。

    这是踩过的坑：复用旧 runner 会带着旧的 dry_run，
    表现为「改了关键词没生效」，严重时会把演练当成真发。
    """
    print("\n[逛站配置刷新]")
    import asyncio as _asyncio
    from types import SimpleNamespace

    from biliauto.web.app import BrowseManager

    class FakeRunner:
        running = False
        rounds = 0
        last_round: dict = {}
        last_round_at = 0.0

        async def round_once(self):
            return SimpleNamespace(as_dict=lambda: {"ok": True})

    mgr = BrowseManager()
    old = FakeRunner()
    mgr.runner = old
    built: list[int] = []

    def fake_build(cfg):
        built.append(1)
        return FakeRunner()

    mgr._build = fake_build
    _asyncio.run(mgr.round_now(SimpleNamespace()))

    check("未运行时会重建 runner", built == [1], str(built))
    check("旧 runner 被替换", mgr.runner is not old)


def test_browse_fixes() -> None:
    """自动逛 B 站四个已修复 bug 的回归。"""
    print("\n[逛站修复回归]")
    import asyncio as _asyncio

    from dataclasses import replace

    from biliauto.browse.actions import ActionResult, InteractionPerformer, _ok
    from biliauto.browse.runner import BrowseRunner
    from biliauto.browse.source import Candidate
    from biliauto.config import ActionSpec, AppConfig

    cfg = AppConfig.load()

    # ① 返回值判断：SDK 成功时返回 data（不含 code），不能因此判失败
    check("空 dict 视为成功", _ok({})[0] is True)
    check("code=0 视为成功", _ok({"code": 0})[0] is True)
    check("code!=0 视为失败", _ok({"code": -101})[0] is False)
    check("None 视为成功", _ok(None)[0] is True)

    # ② 成功要消耗配额，失败不能消耗
    bc = replace(cfg.browse, like=ActionSpec(True, 20, 1.0))
    store = Store(Path(tempfile.mkdtemp()) / "fx.db")
    perf = InteractionPerformer(bc, store, None, dry_run=False)
    cand = Candidate("BVFX", 1, "t", 2, "UP", 300, 5000, time.time(), source="search:x")

    class OkVideo:
        async def has_liked(self):
            return False

        async def like(self, status=True):
            return {"like": 8}  # 真实返回形状

    class BoomVideo:
        async def has_liked(self):
            return False

        async def like(self, status=True):
            raise RuntimeError("boom")

    perf._video = lambda c: OkVideo()
    r = _asyncio.run(perf.like(cand))
    check("点赞成功被正确识别", r.ok and r.detail == "ok", f"{r.ok} {r.detail}")
    check("成功后消耗配额", store.action_used("like") == 1)

    perf._video = lambda c: BoomVideo()
    r = _asyncio.run(perf.like(cand))
    check("异常不算成功", not r.ok)
    check("失败不消耗配额", store.action_used("like") == 1)

    # ③ dry_run 的假动作不能计入「动作总数」
    store.log_action("BVFX", "like", True, "dry_run")
    check("dry_run 不计入成功数", store.browse_stats()["actions_ok"] == 0,
          str(store.browse_stats()))
    store.close()

    # ④ 并发两轮必须互斥，同一视频只能被处理一次
    store2 = Store(Path(tempfile.mkdtemp()) / "race.db")
    cfg2 = replace(
        cfg, dry_run=True,
        browse=replace(cfg.browse, keywords=("x",), max_llm=5, batch=5),
    )
    runner = BrowseRunner(cfg2, store2, credential=None)
    one = Candidate("BVRACE", 1, "竞态", 2, "UP", 300, 5000, time.time(), source="search:x")
    calls: list[str] = []

    async def fake_discover(_bc, _limit):
        return [one]

    async def fake_content(_c):
        return "字幕"

    async def fake_decide(_c, _t):
        return {"worth": True, "reason": "t", "like": True}

    async def fake_act(c, _plan):
        calls.append(c.bvid)
        await _asyncio.sleep(0.2)
        return [ActionResult("like", True, "dry_run")]

    runner.source.discover = fake_discover
    runner._content = fake_content
    runner._decide = fake_decide
    runner._act = fake_act

    async def run_two():
        return await _asyncio.gather(runner.round_once(), runner.round_once())

    a, b = _asyncio.run(run_two())
    check("并发只处理一次", len(calls) == 1, str(calls))
    check("第二轮被拒绝", "还没跑完" in b.error or "还没跑完" in a.error, f"{a.error}|{b.error}")
    store2.close()


def test_sender_clamp() -> None:
    print("\n[发送前的保护]")
    from biliauto.bili.actions import DanmakuSender

    tmp = Path(tempfile.mkdtemp()) / "c.db"
    store = Store(tmp)
    cfg = GuardConfig(
        min_interval=0, per_minute=99, per_hour=99, per_day=99,
        user_cooldown=0, max_danmaku_len=6,
    )
    budget = DanmakuBudget(cfg, store, dry_run=True)
    sender = DanmakuSender(None, cfg, budget, store, dry_run=True)

    check("超长文本被截断", sender.clamp("一二三四五六七八九十") == "一二三四五六")
    check("换行被压成空格", sender.clamp("a\n\nb") == "a b")
    check("全空白返回空串", sender.clamp("   \n ") == "")
    store.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for fn in (test_policy, test_budget, test_llm_coerce, test_parse,
               test_store, test_patterns, test_comments, test_browse,
               test_browse_rebuild, test_browse_fixes, test_sender_clamp):
        fn()
    print(f"\n全部通过：{PASSED} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
