"""环境自检：在真开播前把能踩的坑先踩一遍。

检查项：
  1. 依赖是否装齐
  2. .env 是否存在、必填项是否填了
  3. 直播间是否存在、是否在直播中
  4. AI 账号 cookie 是否有效（会打出账号名，确认没登错号）
  5. DeepSeek 是否通
  6. ffmpeg 是否可用（语音播报需要）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, BAD, WARN = "[OK]  ", "[FAIL]", "[WARN]"


def line(mark: str, text: str) -> None:
    print(f"{mark} {text}")


async def main() -> int:
    failures = 0

    # 1. 依赖
    for mod in ("bilibili_api", "openai", "dotenv", "edge_tts"):
        try:
            __import__(mod)
            line(OK, f"依赖 {mod}")
        except ImportError:
            line(BAD, f"依赖 {mod} 未安装 → pip install -r requirements.txt")
            failures += 1

    # 2. 配置
    from biliauto.config import AppConfig

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        line(OK, ".env 已存在")
    else:
        line(BAD, ".env 不存在 → copy .env.example .env 后填写")
        failures += 1

    cfg = AppConfig.load()
    for problem in cfg.validate():
        line(BAD, problem)
        failures += 1
    if not cfg.validate():
        line(OK, "必填配置齐全")

    # 3. 直播间
    if cfg.bili.room_id > 0:
        try:
            from bilibili_api.live import LiveRoom

            room = LiveRoom(cfg.bili.room_id)
            info = await room.get_room_play_info()
            status = "直播中" if info.get("live_status") == 1 else "未开播"
            line(OK, f"直播间 {cfg.bili.room_id} 存在，当前状态：{status}")
            if info.get("live_status") != 1:
                line(WARN, "房间当前未开播，弹幕流可能为空，属于正常")
        except Exception as e:
            line(BAD, f"取直播间信息失败：{e}")
            failures += 1

    # 4. 账号
    from biliauto.bili import build_credential, verify_credential

    cred = build_credential(cfg.bili)
    if cred is None:
        line(WARN, "未配置 AI 账号凭据，只能 dry-run")
    else:
        info = await verify_credential(cred)
        if info:
            line(OK, f"AI 账号登录有效：{info.describe()}")
            for w in info.warnings():
                line(WARN, w)
            if not info.can_speak:
                failures += 1
        else:
            line(BAD, "AI 账号 cookie 无效或已过期")
            failures += 1

    # 5. DeepSeek
    if cfg.llm.usable:
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=cfg.llm.api_key, base_url=cfg.llm.base_url, timeout=20
            )
            resp = await client.chat.completions.create(
                model=cfg.llm.model,
                messages=[{"role": "user", "content": "回复一个字：好"}],
                max_tokens=5,
            )
            line(OK, f"DeepSeek 连通（{cfg.llm.model}）：{resp.choices[0].message.content!r}")
        except Exception as e:
            detail = str(e)
            line(BAD, f"DeepSeek 调用失败：{detail[:200]}")
            # 模型名不对是最常见的坑，直接把可选项列出来
            if "MODEL_NOT_AVAILABLE" in detail or "model" in detail.lower():
                try:
                    from openai import OpenAI

                    ids = [
                        m.id
                        for m in OpenAI(
                            api_key=cfg.llm.api_key,
                            base_url=cfg.llm.base_url,
                            timeout=20,
                        ).models.list().data
                    ]
                    line(WARN, f"当前端点 {cfg.llm.base_url} 可用模型：")
                    for mid in ids:
                        print(f"          - {mid}")
                    line(WARN, f"把 .env 里的 DEEPSEEK_MODEL 改成上面之一（现在是 {cfg.llm.model}）")
                except Exception as e2:
                    line(WARN, f"顺便查可用模型也失败了：{e2}")
            failures += 1
    else:
        line(BAD, "DEEPSEEK_API_KEY 未填写")
        failures += 1

    # 6. ffmpeg
    try:
        import imageio_ffmpeg

        line(OK, f"ffmpeg：{imageio_ffmpeg.get_ffmpeg_exe()}")
    except Exception:
        line(WARN, "ffmpeg 不可用，play 模式的语音播报会失败（file 模式不受影响）")

    print()
    if failures:
        print(f"共 {failures} 项需要处理，先别开播。")
    else:
        print("全部通过，可以 python scripts/run_live.py 开播了。")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
