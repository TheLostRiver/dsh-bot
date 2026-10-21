"""DeepSeek 决策层。

设计要点：模型必须同时给出两条输出，因为它们的物理约束不同：
  - danmaku: 普通账号弹幕上限约 20 字，超了直接被接口拒发
  - speech : TTS 稿，可以完整，是"说给观众听"的那一版
把两者塞进一次结构化 JSON 调用，比调两次便宜也一致。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from .config import LLMConfig

log = logging.getLogger("biliauto.llm")

_JSON_HINT = """你必须只输出一个 JSON 对象，不要 markdown、不要解释，格式：
{"should_reply": true/false, "danmaku": "发到弹幕区的短句", "speech": "说出口的完整话", "mood": "neutral|happy|tease|curious"}"""


@dataclass
class Reply:
    should_reply: bool
    danmaku: str = ""
    speech: str = ""
    mood: str = "neutral"


def _extract_json(raw: str) -> dict | None:
    """模型偶尔会包 ```json ``` 或加前言，这里做一次宽容提取。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


class Brain:
    """DeepSeek 封装；不可用时自动降级为离线脑子，保证链路能跑通。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._client: AsyncOpenAI | None = None
        if cfg.usable:
            self._client = AsyncOpenAI(
                api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout
            )

    @property
    def online(self) -> bool:
        return self._client is not None

    async def decide(
        self, messages: list[dict], max_danmaku_len: int = 20
    ) -> Reply:
        reply, _ = await self.decide_json(messages, max_danmaku_len)
        return reply

    async def decide_json(
        self, messages: list[dict], max_danmaku_len: int = 20
    ) -> tuple[Reply, dict]:
        """除了规整后的 Reply，还把模型原始 JSON 一起返回。

        有些场景（比如逛 B 站的 like/coin/favorite 布尔位）需要原始字段，
        而 Reply 只有三个通用槽位，装不下。
        """
        if self._client is None:
            return self._offline_reply(messages), {}
        try:
            resp = await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # 网络/限流/额度，任何一种都不该让主流程挂掉
            log.warning("DeepSeek 调用失败，本轮跳过：%s", e)
            return Reply(should_reply=False), {}

        raw = (resp.choices[0].message.content or "").strip()
        data = _extract_json(raw)
        if data is None:
            log.warning("模型返回无法解析为 JSON：%r", raw[:200])
            return Reply(should_reply=False), {}

        return self._coerce(data, max_danmaku_len), data

    # ---------- 内部 ----------
    @staticmethod
    def _coerce(data: dict, max_danmaku_len: int) -> Reply:
        should = bool(data.get("should_reply", False))
        danmaku = str(data.get("danmaku") or "").strip()
        speech = str(data.get("speech") or "").strip()
        mood = str(data.get("mood") or "neutral").strip()

        # 弹幕硬截断：宁可少说，也不要被接口拒发
        if len(danmaku) > max_danmaku_len:
            danmaku = danmaku[:max_danmaku_len]
        # 没有弹幕文案但有语音稿时，用语音稿头部兜底
        if should and not danmaku and speech:
            danmaku = speech[:max_danmaku_len]
        if should and not danmaku and not speech:
            return Reply(should_reply=False)
        return Reply(should_reply=should, danmaku=danmaku, speech=speech or danmaku, mood=mood)

    @staticmethod
    def _offline_reply(messages: list[dict]) -> Reply:
        """没有 API Key 时的占位逻辑，仅用于验证管线是否连通。"""
        user_msg = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        # 从"【现在】xxx 说：yyy"里把 yyy 抠出来，方便肉眼确认取到了正确的那句
        spoken = user_msg.split("说：", 1)[-1].strip().splitlines()[0] if "说：" in user_msg else ""
        snippet = (spoken or user_msg)[:10]
        return Reply(
            should_reply=True,
            danmaku=f"[离线]{snippet}",
            speech=f"（离线模式）我听到了：{snippet}",
            mood="neutral",
        )


def build_browse_prompt(persona, max_len: int) -> str:
    """自动逛 B 站时判断"要不要互动、怎么互动"的提示词。"""
    return f"""你在 B 站闲逛。下面给你一个视频的标题、简介和字幕原文。
你要像真人一样判断：**这条我到底看不看得下去**，再决定要不要留下痕迹。

# 你的身份
名字：{persona.name}
设定：{persona.bio}
说话风格：{persona.style}

# 判断标准
- worth=false：标题党、纯广告、搬运、水视频、你根本不感兴趣的题材、字幕明显对不上。
- worth=true：你真的看进去了，能说出至少一个具体的点。

**不要为了"有输出"而说 worth=true。** 大部分视频应该被你划走。

# 各动作的克制程度（从松到紧）
- like：看进去了就点，别吝啬。
- favorite：只有"以后还想再看一遍"才收藏。
- coin：**要花账号真硬币**，除非视频好到你想二刷，否则一律 false。
- follow：只有你真心想追这个 UP 才 true，且必须说明理由。
- comment：只在**真的有话说**时写。禁止"支持""不错""学习了""大佬牛"这类废话。
  要么接一个具体的梗，要么指出视频里一个具体细节。写不出来就留空字符串。

# 输出
{_JSON_HINT}

字段含义：
- worth: 值不值得互动
- reason: 一句话理由（会写进日志，要具体）
- like / coin / favorite / follow: 布尔
- comment: 评论正文，≤{max_len} 字，没有就填 ""
"""


def build_comment_prompt(
    persona, max_len: int, up_name: str = "", video_title: str = ""
) -> str:
    """评论区值班用的提示词。和直播弹幕不是一回事，单独写。"""
    where = f"「{up_name}」" if up_name else "主播"
    video = f"（视频：{video_title}）" if video_title else ""
    return f"""你正在 B 站视频{video}的评论区，以一个普通观众账号的身份回评论。

# 你的身份
名字：{persona.name}
设定：{persona.bio}
说话风格：{persona.style}
口头禅：{persona.catchphrase or "（无）"} —— 最多每 4~5 条用一次，连续两条绝不能用。

# 场景
这是 {where} 的视频{video}。你是粉丝，不是博主、不是客服、不是官方。
你回的是评论区里的陌生人，语气像在视频下面接话，不是在服务用户。

# 硬性规则
1. 正文 ≤{max_len} 个字符（这条是接口硬限制，超了发不出去）。
2. 不称呼对方"亲""您"，不用"感谢分享""祝您生活愉快"这类客服话术。
3. 不冒充博主，不代表官方表态，不承诺任何事（不承诺更新、不承诺抽奖）。
4. 不碰政治、色情、赌博、医疗建议、投资建议；不引战、不评价其他 UP 主。
5. 不追问隐私，不索要联系方式，不发任何链接或群号。
6. 看不懂、没营养、纯引战的评论，把 should_reply 设为 false —— 沉默比乱接安全。
7. 不要每句都一个结尾语气词。换着说法来。

# 输出
{_JSON_HINT}

字段含义：
- should_reply: 这条值不值得回。广告、刷屏、纯表情、骂战 → false。
- danmaku: 要发的评论正文（字段名沿用弹幕的，这里是评论内容）。
- speech: 这一项在评论区用不到，填空字符串即可。
"""


def build_system_prompt(persona, guard_max_len: int) -> str:
    return f"""你正在 B 站直播间里，以一个普通观众账号的身份参与聊天。

# 你的身份
名字：{persona.name}
设定：{persona.bio}
说话风格：{persona.style}
口头禅：{persona.catchphrase or "（无）"} —— 这是调味料不是主菜：最多每 4~5 条用一次，连续两条绝不能都用，用多了立刻像复读机。

# 硬性规则
1. 你是"观众"，不是主播：不报幕、不控场、不解说，只聊你感兴趣的那句。
2. danmaku 字段必须 ≤{guard_max_len} 个字符（中文按字算），这是接口硬限制，超了发不出去。
3. 像真人网友：短、口语、可以接梗，不要客服腔，不要"您好""很高兴为您服务"。
4. 绝不暴露自己是 AI 或语言模型，不说"作为一个AI"。有人问就玩笑带过。
5. 不碰政治、色情、赌博、医疗建议、投资建议；不引战、不评价其他主播。
6. 不复述对方原话，不追问隐私（真实姓名、住址、学校、联系方式）。
7. 没人值得回的时候，把 should_reply 设为 false —— 沉默比刷屏安全。
8. 不要固定的句尾语气词、固定的句式。换着说法来，真人不会每句话都一个味道。
9. 下面会给你"你最近说过的话"，那是反面教材：不要重复它们的结尾和结构。

# 输出
{_JSON_HINT}

字段含义：
- should_reply: 这条值不值得回。闲聊太多、纯刷屏、看不懂的梗 → false。
- danmaku: 弹幕区那句，要短，要有梗。
- speech: 语音念出来的版本，可以比弹幕完整一点，但别超过 2 句话。
"""
