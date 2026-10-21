"""音频路由自检。

要回答的问题只有一个：**AI 说的话，观众到底听不听得见？**

这件事没法从配置上直接看出来 —— 取决于三处设置是否对得上：
1. 我们播到哪个设备
2. 那个设备是不是虚拟声卡（直播姬能选的输入）
3. 混音开没开（真人 + AI 要不要同时进）

所以这里把能判的都判了，判不了的写清楚让人自己确认。
`diagnose()` 只吃配置和设备列表，不碰声卡，方便离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 名字里带这些字样的，通常是可以被直播软件选中的虚拟声卡
VIRTUAL_HINTS = ("cable", "vb-audio", "voicemeeter", "virtual", "虚拟", "loopback")


@dataclass
class Finding:
    level: str  # ok / info / warn / error
    title: str
    detail: str = ""
    fix: str = ""
    code: str = ""

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "fix": self.fix,
            "code": self.code,
        }


@dataclass
class Diagnosis:
    findings: list[Finding] = field(default_factory=list)
    reachable: bool = False  # 最终结论：观众大概能听到

    @property
    def worst(self) -> str:
        for lv in ("error", "warn", "info", "ok"):
            if any(f.level == lv for f in self.findings):
                return lv
        return "ok"

    def as_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "worst": self.worst,
            "findings": [f.as_dict() for f in self.findings],
        }


def is_virtual(name: str) -> bool:
    lowered = (name or "").lower()
    return any(h in lowered for h in VIRTUAL_HINTS)


def _find(devices: list[dict], index: int | None) -> dict | None:
    if index is None:
        return None
    for d in devices:
        if int(d.get("index", -1)) == int(index):
            return d
    return None


def diagnose(
    speech,
    default_output: str = "",
    outputs: list[dict] | None = None,
    inputs: list[dict] | None = None,
) -> Diagnosis:
    """speech 是 SpeechConfig；outputs/inputs 是 [{index,name}]。"""
    outputs = outputs or []
    inputs = inputs or []
    d = Diagnosis()

    # ---------- 1. 语音总开关 ----------
    if not speech.enabled:
        d.findings.append(
            Finding(
                "info",
                "语音播报是关闭的",
                "AI 只发弹幕，不会出声。",
                "要出声就在「直播互动」页顶部打开语音开关。",
                "speech_off",
            )
        )
        return d

    # ---------- 2. 混音优先判断 ----------
    # 混音模式下 Speaker 会直接走混音，跟 backend 无关，所以要先判
    if not speech.mix_enabled:
        if speech.backend != "play":
            d.findings.append(
                Finding(
                    "info",
                    "当前是 file 模式：只生成 mp3，不主动播放",
                    f"音频落在 {speech.out_dir}。需要你自己在 OBS/直播姬里播。",
                    "想让它自动出声，把输出方式改成 play。",
                    "file_mode",
                )
            )
            d.reachable = False
            return d

    # ---------- 3. 混音 ----------
    if speech.mix_enabled:
        out_idx = speech.mix_output if speech.mix_output is not None else speech.device
        out_dev = _find(outputs, out_idx)
        in_dev = _find(inputs, speech.mix_input)

        if out_dev is None:
            d.findings.append(
                Finding(
                    "error",
                    "混音的输出设备找不到",
                    f"配置里的设备序号 {out_idx} 不存在。",
                    "重新在「混音」里选一次输出设备，建议选虚拟声卡的 Input。",
                    "mix_out_missing",
                )
            )
        elif not is_virtual(out_dev.get("name", "")):
            d.findings.append(
                Finding(
                    "warn",
                    "混音输出到了物理设备",
                    f"当前输出：{out_dev.get('name')}。直播姬收不到物理声卡里的声音。",
                    "把混音输出改成虚拟声卡（CABLE Input / VoiceMeeter Input）。",
                    "mix_out_physical",
                )
            )
        else:
            d.findings.append(
                Finding(
                    "ok",
                    "混音输出接在虚拟声卡上",
                    f"当前输出：{out_dev.get('name')}。",
                    "直播姬里把麦克风选成对应的虚拟声卡输出端。",
                    "mix_out_virtual",
                )
            )

        if speech.mix_input is None:
            d.findings.append(
                Finding(
                    "warn",
                    "混音没指定麦克风，用的是系统默认",
                    "系统默认输入可能不是你的麦，观众会听到奇怪的声音。",
                    "在「混音 → 麦克风设备」里明确选一下。",
                    "mix_in_default",
                )
            )
        elif in_dev is None:
            d.findings.append(
                Finding(
                    "error",
                    "混音的麦克风设备找不到",
                    f"配置里的设备序号 {speech.mix_input} 不存在。",
                    "重新选一次麦克风。",
                    "mix_in_missing",
                )
            )
        elif is_virtual(in_dev.get("name", "")):
            d.findings.append(
                Finding(
                    "warn",
                    "混音的麦克风选成了虚拟声卡",
                    f"当前输入：{in_dev.get('name')}。这会把虚拟线里的声音再混回去，容易自激。",
                    "麦克风应该选你的真实麦克风。",
                    "mix_in_virtual",
                )
            )

        if (
            speech.mix_input is not None
            and out_idx is not None
            and int(speech.mix_input) == int(out_idx)
        ):
            d.findings.append(
                Finding(
                    "error",
                    "混音的输入和输出是同一个设备",
                    "会形成回环，直播里会听到尖锐啸叫。",
                    "输入选真实麦克风，输出选虚拟声卡。",
                    "mix_loop",
                )
            )

        d.reachable = any(f.code == "mix_out_virtual" for f in d.findings) and not any(
            f.level == "error" for f in d.findings
        )
        if d.reachable:
            d.findings.append(
                Finding(
                    "ok",
                    "结论：真人 + AI 会一起进直播",
                    "直播姬只需选虚拟声卡的输出端作为麦克风。",
                    "",
                    "mix_ok",
                )
            )
        return d

    # ---------- 4. 非混音 + play ----------
    out_dev = _find(outputs, speech.device)
    if speech.device is not None and out_dev is None:
        d.findings.append(
            Finding(
                "error",
                "播放设备不存在",
                f"配置里的设备序号 {speech.device} 找不到。",
                "重新选一次播放设备。",
                "play_out_missing",
            )
        )
        return d

    if out_dev is None:
        # 播到系统默认
        if is_virtual(default_output):
            d.findings.append(
                Finding(
                    "ok",
                    "播放到系统默认设备（虚拟声卡）",
                    f"默认输出：{default_output}。直播姬可以收到。",
                    "",
                    "play_default_virtual",
                )
            )
            d.reachable = True
        else:
            d.findings.append(
                Finding(
                    "warn",
                    "AI 的声音播在你自己的设备上",
                    f"默认输出是「{default_output}」，直播姬收不到它。"
                    "除非你在直播姬里开了「系统声音」采集。",
                    "要么开系统声音采集，要么在「播放设备」里选虚拟声卡。",
                    "play_default_physical",
                )
            )
            d.reachable = False
    elif is_virtual(out_dev.get("name", "")):
        d.findings.append(
            Finding(
                "ok",
                "播放到虚拟声卡",
                f"当前输出：{out_dev.get('name')}。",
                "直播姬里把麦克风选成这个虚拟声卡的输出端。",
                "play_out_virtual",
            )
        )
        d.reachable = True
    else:
        d.findings.append(
            Finding(
                "warn",
                "播放到物理设备",
                f"当前输出：{out_dev.get('name')}。直播姬收不到。",
                "改成虚拟声卡，或者开直播姬的系统声音采集。",
                "play_out_physical",
            )
        )
        d.reachable = False

    if not speech.mix_enabled:
        d.findings.append(
            Finding(
                "info",
                "没有开启混音",
                "AI 语音和你的麦克风是两路，直播姬只能选一路。",
                "想同时进直播就开「混音」。",
                "no_mix",
            )
        )

    return d
