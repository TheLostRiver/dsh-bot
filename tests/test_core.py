"""核心逻辑自测（不联网、不需要账号、不需要 pytest）。

    python tests/test_core.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biliauto.bili.danmaku import parse_event  # noqa: E402
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
    check("待定状态不算见过（还能再处理）", s.seen_comment(12345) is False)
    s.set_comment_action(12345, "deferred", "cooldown")
    check("顺延状态也不算见过", s.seen_comment(12345) is False)
    s.set_comment_action(12345, "ignored", "model_declined")
    check("终态才算见过", s.seen_comment(12345) is True)
    s.record_comment(12345, 1, 2, "乙", "好看", time.time(), "replied", "ok")
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


def test_mixer() -> None:
    print("\n[混音]")
    import numpy as np

    from biliauto.audio.mixer import AudioMixer, mix_frames, resample, to_float

    # int16 -> float
    f = to_float(np.array([0, 16384, -32768], dtype=np.int16))
    check("int16 归一化", abs(f[1] - 0.5) < 1e-3 and abs(f[2] + 1.0) < 1e-3, str(f))

    # 重采样
    src = np.sin(np.linspace(0, 6.28, 240)).astype(np.float32)
    up = resample(src, 24000, 48000)
    down = resample(src, 48000, 24000)
    check("24k->48k 长度翻倍", abs(len(up) - 480) <= 1, str(len(up)))
    check("48k->24k 长度减半", abs(len(down) - 120) <= 1, str(len(down)))
    check("同采样率原样返回", len(resample(src, 24000, 24000)) == 240)

    # 混音：无 TTS 时等于麦克风乘增益
    mic = np.full(100, 0.4, dtype=np.float32)
    out = mix_frames(mic, None, mic_gain=0.5)
    check("无 TTS 只过麦克风", np.allclose(out, 0.2), str(out[0]))

    # 有 TTS 时叠加，且不越界
    tts = np.full(100, 0.5, dtype=np.float32)
    out = mix_frames(mic, tts, mic_gain=1.0, tts_gain=1.0)
    check("叠加后限幅在 1 以内", out.max() <= 1.0 and out.min() >= -1.0, str(out.max()))

    # 闪避：有 TTS 时麦克风被压低
    out = mix_frames(mic, tts, mic_gain=1.0, tts_gain=0.0, duck=0.5)
    check("闪避压低麦克风", abs(out[0] - 0.2) < 1e-6, str(out[0]))
    out2 = mix_frames(mic, None, mic_gain=1.0, tts_gain=0.0, duck=0.5)
    check("没有 TTS 时不闪避", abs(out2[0] - 0.4) < 1e-6, str(out2[0]))

    # TTS 比帧短时补零对齐
    out = mix_frames(mic, np.full(10, 0.1, dtype=np.float32), 1.0, 1.0, 0.0)
    check("短 TTS 自动补零", len(out) == 100 and abs(out[0] - 0.5) < 1e-6, str(out[0]))

    # 未启动时 feed 是安全的空操作
    m = AudioMixer(None, None)
    m.feed(np.zeros(100, dtype=np.float32), 24000)
    check("未启动时 feed 不报错", m.queued_seconds == 0.0)
    check("未启动 running=False", m.running is False)

    # 队列取帧：不够一帧就不播半截
    class StubStream:
        samplerate = 48000

    m._stream = StubStream()
    m._tts.append(np.ones(2000, dtype=np.float32))
    got = m._take(1024)
    check("取满一帧返回整块", got is not None and len(got) == 1024)
    got2 = m._take(2048)
    check("剩余不足一帧时返回 None", got2 is None)
    m._stream = None


def test_diagnose() -> None:
    print("\n[路由自检]")
    from dataclasses import replace

    from biliauto.audio.check import diagnose
    from biliauto.config import AppConfig

    base = AppConfig.load().speech

    outs = [
        {"index": 4, "name": "耳机 (Realtek High Definition Audio)"},
        {"index": 9, "name": "CABLE Input (VB-Audio Virtual Cable)"},
    ]
    ins = [
        {"index": 1, "name": "麦克风 (Realtek High Definition Audio)"},
        {"index": 8, "name": "CABLE Output (VB-Audio Virtual Cable)"},
    ]

    def codes(speech, default="耳机 (Realtek High Definition Audio)"):
        return [f.code for f in diagnose(speech, default, outs, ins).findings]

    # 语音关 / file 模式
    d = diagnose(replace(base, enabled=False), "耳机", outs, ins)
    check("语音关闭时给提示", "speech_off" in [f.code for f in d.findings] and not d.reachable)

    d = diagnose(replace(base, enabled=True, backend="file", mix_enabled=False), "耳机", outs, ins)
    check("file 模式说明不主动播放",
          "file_mode" in [f.code for f in d.findings] and not d.reachable)

    # play 到物理设备 = 观众听不到
    d = diagnose(replace(base, enabled=True, backend="play", mix_enabled=False, device=None),
                 "耳机 (Realtek)", outs, ins)
    check("默认播到物理设备时警告", "play_default_physical" in codes(
        replace(base, enabled=True, backend="play", mix_enabled=False, device=None)))
    check("物理设备判为不可达", d.reachable is False)

    # play 到虚拟声卡 = 可达
    d = diagnose(replace(base, enabled=True, backend="play", mix_enabled=False, device=9),
                 "耳机", outs, ins)
    check("播到虚拟声卡判为可达", d.reachable is True and "play_out_virtual" in [
        f.code for f in d.findings])

    # 设备序号不存在
    d = diagnose(replace(base, enabled=True, backend="play", mix_enabled=False, device=999),
                 "耳机", outs, ins)
    check("设备不存在报错", "play_out_missing" in [f.code for f in d.findings])

    # 混音：输入真麦 + 输出虚拟声卡 = 可达
    d = diagnose(
        replace(base, enabled=True, backend="play", mix_enabled=True,
                mix_input=1, mix_output=9), "耳机", outs, ins)
    check("混音正确配置判为可达", d.reachable is True, str([f.code for f in d.findings]))
    check("混音可达时给结论", "mix_ok" in [f.code for f in d.findings])

    # 混音：输出物理设备
    d = diagnose(
        replace(base, enabled=True, backend="play", mix_enabled=True,
                mix_input=1, mix_output=4), "耳机", outs, ins)
    check("混音输出到物理设备时警告", "mix_out_physical" in [f.code for f in d.findings])
    check("混音误配判为不可达", d.reachable is False)

    # 混音：输入输出同一个设备 = 回环
    d = diagnose(
        replace(base, enabled=True, backend="play", mix_enabled=True,
                mix_input=9, mix_output=9), "耳机", outs, ins)
    check("输入输出相同报回环", "mix_loop" in [f.code for f in d.findings])

    # 混音：麦克风选成虚拟声卡
    d = diagnose(
        replace(base, enabled=True, backend="play", mix_enabled=True,
                mix_input=8, mix_output=9), "耳机", outs, ins)
    check("麦克风选虚拟声卡时警告", "mix_in_virtual" in [f.code for f in d.findings])

    # 混音：输出设备不存在
    d = diagnose(
        replace(base, enabled=True, backend="play", mix_enabled=True,
                mix_input=1, mix_output=999), "耳机", outs, ins)
    check("混音输出不存在报错", "mix_out_missing" in [f.code for f in d.findings])


def test_live_fixes() -> None:
    """直播互动三个已修复 bug 的回归。"""
    print("\n[直播互动修复回归]")
    import asyncio as _asyncio
    from dataclasses import replace

    from biliauto.bili.actions import DanmakuSender
    from biliauto.bili.danmaku import LiveClient, LiveEvent
    from biliauto.bili.response import ok_result
    from biliauto.config import AppConfig
    from biliauto.live.bot import LiveBot
    from biliauto.live.guard import DanmakuBudget

    cfg = AppConfig.load()

    # ① 发弹幕的成功判定：SDK 返回 data，不含 code
    check("data 形状判成功", ok_result({"mode_info": {}})[0] is True)
    check("带 code 才核对", ok_result({"code": -101})[0] is False)

    store = Store(Path(tempfile.mkdtemp()) / "live.db")
    budget = DanmakuBudget(
        replace(cfg.guard, min_interval=30, per_minute=3, per_day=100),
        store, dry_run=False,
    )

    class FakeRoom:
        async def send_danmaku(self, danmaku, reply_mid=None):
            return {"mode_info": {"mode": 1}, "rnd": 1}

    sender = DanmakuSender(FakeRoom(), cfg.guard, budget, store, dry_run=False)
    ok, why = _asyncio.run(sender.send("回归测试弹幕"))
    check("成功发送被判为成功", ok and why == "ok", f"{ok} {why}")
    check("成功后才计消耗", store.today_usage()["danmaku_sent"] == 1,
          str(store.today_usage()))
    ok2, why2 = budget.check(1)
    check("成功后进入冷却", not ok2 and why2 == "cooldown", f"{ok2} {why2}")
    store.close()

    # ② 上下文不能把"正在回的这条"重复塞进去
    cfg2 = replace(cfg, dry_run=True, trigger=replace(cfg.trigger, reply_probability=1.0))
    store2 = Store(Path(tempfile.mkdtemp()) / "ctx.db")
    bot = LiveBot(cfg2, store2)

    def ev(uid, name, text):
        return LiveEvent(kind="DANMU_MSG", uid=uid, uname=name, text=text)

    async def feed():
        a = ev(1, "甲", "甲说的第一句？")
        b = ev(2, "乙", "乙说的第二句？")
        await bot.handle_event(a)
        await bot.handle_event(b)
        return a, bot._build_messages(a)[-1]["content"]

    _a, user = _asyncio.run(feed())
    check("当前弹幕在上下文里只出现一次", user.count("甲说的第一句？") == 1,
          str(user.count("甲说的第一句？")))
    check("更晚到的弹幕仍在上下文里", "乙说的第二句" in user)
    store2.close()

    # ③ 事件处理函数抛异常不能把连接带崩
    client = LiveClient(1, credential=None, handler=None)
    boom = {"called": 0}

    async def bad_handler(_ev):
        boom["called"] += 1
        raise RuntimeError("模拟下游故障")

    client.handler = bad_handler
    handler = client._make_handler("DANMU_MSG")
    payload = {"info": [[], "测试内容", [123, "某人", 0, 0, 0], [0], []]}
    _asyncio.run(handler({"data": payload}))
    check("处理函数异常被吞掉", boom["called"] == 1)

    # ③.5 打了钱没留言的 SC 不能丢
    from biliauto.bili.danmaku import parse_event
    from biliauto.live.guard import TriggerPolicy

    sc = parse_event("SUPER_CHAT_MESSAGE", {"data": {"price": 30, "uid": 5,
                                                     "user_info": {"uname": "老板"}}})
    check("空文本 SC 仍然会产生事件", sc is not None and "SC" in sc.text, str(sc))
    pol = TriggerPolicy(cfg.trigger, cfg.persona.aliases, 999)
    dec = pol.classify(sc.uid, sc.uname, sc.text, sc.kind)
    check("空文本 SC 仍然按付费处理", dec.action == "consider" and dec.priority == 2,
          f"{dec.action}/{dec.reason}")

    # ④ 没凭据时拒绝真开播（接口层的同一判断）
    no_cred = replace(cfg, dry_run=False, bili=replace(cfg.bili, sessdata="", bili_jct=""))
    check("无凭据不应被视为可发送", (not no_cred.dry_run and not no_cred.bili.has_credential))

    # ⑤ 口头禅去重依赖 _recent_replies 被正确填充（修复①后才恢复）
    store3 = Store(Path(tempfile.mkdtemp()) / "polish.db")
    bot2 = LiveBot(
        replace(cfg, dry_run=True, persona=replace(cfg.persona, catchphrase="喵喵")),
        store3,
    )
    bot2._recent_replies.extend(["甲甲甲甲喵喵", "乙乙乙乙喵喵"])
    check("连续重复的口头禅会被摘掉",
          bot2._polish("这句话够长了喵喵") == "这句话够长了",
          bot2._polish("这句话够长了喵喵"))
    store3.close()


def test_offline_and_comment_fixes() -> None:
    """离线兜底、评论并发、配置刷新三处修复的回归。"""
    print("\n[离线兜底与评论修复]")
    import asyncio as _asyncio
    from dataclasses import replace

    from biliauto.bili.danmaku import LiveEvent
    from biliauto.comments.duty import CommentDuty, ScanResult
    from biliauto.config import AppConfig
    from biliauto.llm import Brain
    from biliauto.live.bot import LiveBot

    cfg = AppConfig.load()
    offline = replace(cfg, dry_run=False, llm=replace(cfg.llm, api_key=""))

    # ① 没有 API Key 时不能把 "[离线]…" 发出去
    store = Store(Path(tempfile.mkdtemp()) / "off.db")
    bot = LiveBot(offline, store)
    bot.brain = Brain(offline.llm)
    sent: list[str] = []

    class FakeSender:
        dry_run = False

        async def send(self, text, reply_mid=None):
            sent.append(text)
            return True, "ok"

    bot.sender = FakeSender()
    ev = LiveEvent(kind="DANMU_MSG", uid=1, uname="甲", text="好看吗？")
    eid = store.log_event(ev.kind, ev.uid, ev.uname, ev.text)
    _asyncio.run(bot._process(eid, ev))
    check("离线时不发送占位文本", len(sent) == 0, str(sent))
    store.close()

    # ② 评论值班离线时直接放弃
    store2 = Store(Path(tempfile.mkdtemp()) / "off2.db")
    duty = CommentDuty(offline, store2)
    duty.brain = Brain(offline.llm)
    from biliauto.comments.collector import CommentEvent

    import time as _t

    c = CommentEvent(rpid=1, oid=1, uid=2, uname="乙", text="这视频好看吗",
                     ctime=_t.time(), is_top_level=True, reply_count=0)
    check("评论离线时不生成回复", _asyncio.run(duty._decide(c)) is None)
    store2.close()

    # ③ 评论扫描并发互斥
    store3 = Store(Path(tempfile.mkdtemp()) / "cc.db")
    d3 = CommentDuty(replace(cfg, dry_run=True), store3)
    calls: list[int] = []

    async def fake_do_scan():
        calls.append(1)
        await _asyncio.sleep(0.2)
        return ScanResult()

    d3._do_scan = fake_do_scan

    async def two():
        return await _asyncio.gather(d3.scan_once(), d3.scan_once())

    a, b = _asyncio.run(two())
    check("评论并发只扫一次", len(calls) == 1, str(calls))
    check("第二轮被拒绝", "还没扫完" in (a.error or b.error), f"{a.error}|{b.error}")
    store3.close()


def test_live_lifecycle() -> None:
    """队列溢出、停止收尾、连接断开后退出。"""
    print("\n[直播生命周期]")
    import asyncio as _asyncio
    from dataclasses import replace

    from biliauto.bili.danmaku import LiveEvent
    from biliauto.config import AppConfig
    from biliauto.llm import Reply
    from biliauto.live.bot import LiveBot

    cfg = AppConfig.load()
    always = replace(cfg, dry_run=True,
                     trigger=replace(cfg.trigger, reply_probability=1.0))

    def mk_ev(i):
        return LiveEvent(kind="DANMU_MSG", uid=i, uname=f"u{i}", text=f"消息{i}")

    # ① 队列满：被丢弃的事件要有结局，不能停在 considered
    store = Store(Path(tempfile.mkdtemp()) / "ov.db")
    bot = LiveBot(always, store)

    async def fill():
        for i in range(80):
            await bot.handle_event(mk_ev(i))

    _asyncio.run(fill())
    rows = {
        (r[0], r[1]): r[2]
        for r in store._conn.execute(
            "SELECT decision, IFNULL(reason,''), COUNT(*) FROM events GROUP BY 1,2"
        )
    }
    check("队列满的事件标记为 queue_full", rows.get(("skipped", "queue_full")) == 30,
          str(rows))
    check("没有事件停留在无结论状态", rows.get(("considered", "sampled")) == 50, str(rows))
    store.close()

    # ② 连接正常断开时 run() 必须返回（否则界面一直显示运行中）
    store2 = Store(Path(tempfile.mkdtemp()) / "life.db")
    bot2 = LiveBot(always, store2)

    class SlowBrain:
        online = True

        async def decide(self, m, n):
            await _asyncio.sleep(0.5)
            return Reply(True, "好", "好")

    bot2.brain = SlowBrain()

    class FakeClient:
        async def run(self):
            await _asyncio.sleep(0.05)
            return None

        async def stop(self):
            pass

    async def life():
        async def feeder():
            await _asyncio.sleep(0.01)
            for i in range(5):
                await bot2.handle_event(mk_ev(i))

        _asyncio.create_task(feeder())
        await _asyncio.wait_for(bot2.run(FakeClient()), timeout=8)

    try:
        _asyncio.run(life())
        returned = True
    except _asyncio.TimeoutError:
        returned = False
    check("连接断开后 run() 会返回", returned)

    rows2 = {
        r[0]: r[1]
        for r in store2._conn.execute(
            "SELECT IFNULL(reason,''), COUNT(*) FROM events GROUP BY 1"
        )
    }
    check("停止时队列里的事件被收尾", rows2.get("stopped", 0) >= 1
          and rows2.get("", 0) == 0, str(rows2))
    store2.close()


def test_comment_retry() -> None:
    """被配额挤掉的评论不能永久失效。"""
    print("\n[评论顺延]")
    import asyncio as _asyncio
    import time as _t
    from dataclasses import replace

    from biliauto.comments.collector import CommentEvent, VideoRef
    from biliauto.comments.duty import CommentDuty
    from biliauto.config import AppConfig
    from biliauto.llm import Reply

    cfg = AppConfig.load()
    cc = replace(cfg.comments, max_per_day=100, min_interval=3600,
                 lookback_hours=72, reply_probability=1.0)
    store = Store(Path(tempfile.mkdtemp()) / "retry.db")
    duty = CommentDuty(replace(cfg, dry_run=True, comments=cc), store, credential=object())

    class FakeCollector:
        async def resolve_anchor(self, r):
            return 111, "主播"

        async def videos(self, mid, limit=5):
            return [VideoRef(aid=1, bvid="BV1", title="测试视频")]

        async def comments(self, v, page=1):
            return [
                CommentEvent(rpid=100 + i, oid=1, uid=200 + i, uname=f"观众{i}",
                             text=f"这期讲得真好，第{i}个问题怎么看？",
                             ctime=_t.time() - 60, is_top_level=True, reply_count=0,
                             video_title="测试视频")
                for i in range(3)
            ]

    duty.collector = FakeCollector()

    class FakeBrain:
        online = True

        async def decide(self, m, n):
            return Reply(True, "谢谢支持", "谢谢支持")

    duty.brain = FakeBrain()

    r1 = _asyncio.run(duty._do_scan())
    check("一轮只能回一条", r1.replied == 1 and r1.candidates == 3, str(r1.as_dict()))

    actions = {r[0]: r[1] for r in store._conn.execute(
        "SELECT rpid, action FROM comments_seen")}
    check("没轮到的标记为 deferred",
          list(actions.values()).count("deferred") == 2, str(actions))

    duty.budget._last = 0
    r2 = _asyncio.run(duty._do_scan())
    check("顺延的评论下一轮会被重新捡起", r2.candidates == 2, str(r2.as_dict()))

    replied = [r[0] for r in store._conn.execute(
        "SELECT rpid FROM comment_replies WHERE ok=1")]
    check("同一条评论不会被回复两次", len(replied) == len(set(replied)) == 2,
          str(replied))
    store.close()


def test_pagination_bounds() -> None:
    """分页参数被喂负数时不能反过来返回更多。"""
    print("\n[分页边界]")
    import logging

    from biliauto.web.app import LogBuffer

    buf = LogBuffer()
    for i in range(10):
        buf.emit(logging.LogRecord("x", logging.INFO, __file__, 1, f"第{i}条", None, None))
    check("tail(3) 取 3 条", len(buf.tail(3)) == 3)
    check("tail(0) 不返回全部", buf.tail(0) == [])
    check("tail(-1) 不丢第一条", buf.tail(-1) == [])
    check("tail(100) 不超过总量", len(buf.tail(100)) == 10)

    store = Store(Path(tempfile.mkdtemp()) / "page.db")
    for i in range(20):
        store.record_browse(f"BV{i:03d}", 100 + i, f"标题{i}", 1, "UP", 300, 5000, 0, "judged")
    store._conn.commit()
    check("limit=2 只给 2 条", len(store.browse_history(limit=2)["items"]) == 2)
    check("limit=-1 不会拉全表",
          len(store.browse_history(limit=-1)["items"]) == 1,
          str(len(store.browse_history(limit=-1)["items"])))
    check("offset 负数被夹成 0",
          len(store.browse_history(limit=5, offset=-3)["items"]) == 5)
    store.close()


def test_tts_cleanup() -> None:
    """合成失败不能留下空文件。"""
    print("\n[TTS 清理]")
    import asyncio as _asyncio
    from dataclasses import replace

    import edge_tts

    from biliauto.bili.actions import Speaker
    from biliauto.config import AppConfig

    cfg = AppConfig.load()
    out = Path(tempfile.mkdtemp()) / "sp"
    sp = Speaker(replace(cfg.speech, out_dir=out))

    class FakeComm:
        def __init__(self, *a, **k):
            pass

        async def save(self, path):
            # 真实情况：先建文件再边下边写，中途断开就留下空文件
            Path(path).write_bytes(b"")
            raise RuntimeError("模拟连接中断")

    real = edge_tts.Communicate
    edge_tts.Communicate = FakeComm
    try:
        r = _asyncio.run(sp.say("模拟失败"))
    finally:
        edge_tts.Communicate = real

    check("合成失败返回 None", r is None)
    check("失败后不留空文件", not list(out.glob("*.mp3")),
          str([f.name for f in out.glob("*.mp3")]))


def test_instance_lock() -> None:
    """同一份数据目录只允许一个机器人。"""
    print("\n[单实例锁]")
    from biliauto.lock import InstanceLock

    d = Path(tempfile.mkdtemp())
    a = InstanceLock(d / ".bot.lock")
    b = InstanceLock(d / ".bot.lock")

    ok, _ = a.acquire("web")
    check("第一个能拿到锁", ok)
    ok2, holder = b.acquire("cli")
    check("第二个拿不到锁", not ok2)
    check("能报出占用者", "web" in holder, holder)

    ok3, _ = a.acquire("again")
    check("同进程可重入", ok3)
    a.release()
    a.release()
    check("释放后不再持有", not a.held)
    ok4, _ = b.acquire("cli")
    check("释放后另一个能拿到", ok4)
    b.release()


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
               test_browse_rebuild, test_browse_fixes, test_mixer,
               test_diagnose, test_live_fixes,
               test_offline_and_comment_fixes, test_live_lifecycle, test_instance_lock,
               test_comment_retry, test_pagination_bounds, test_tts_cleanup,
               test_sender_clamp):
        fn()
    print(f"\n全部通过：{PASSED} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
