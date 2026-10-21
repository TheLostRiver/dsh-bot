"""真实开播入口。

用法：
    python scripts/run_live.py                # 正式运行
    python scripts/run_live.py --dry-run      # 走完整流程但不发弹幕
    python scripts/run_live.py --no-speech    # 只发弹幕不念

跑之前先执行 python scripts/check_env.py 确认环境。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biliauto.config import AppConfig, setup_logging  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()

    if args.room:
        cfg = replace(cfg, bili=replace(cfg.bili, room_id=args.room))
    if args.dry_run:
        cfg = replace(cfg, dry_run=True)
    if args.no_speech:
        cfg = replace(cfg, speech=replace(cfg.speech, enabled=False))

    setup_logging(cfg.log_level)

    problems = cfg.validate()
    if problems and not args.force:
        print("\n配置不完整，已中止：")
        for p in problems:
            print(f"  - {p}")
        print("\n修好 .env 后重试，或用 --force 强行尝试。\n")
        return 2
    for p in problems:
        print(f"[警告] {p}")

    from biliauto.bili import build_credential, verify_credential
    from biliauto.bili.danmaku import LiveClient
    from biliauto.live.bot import LiveBot
    from biliauto.memory.store import Store

    credential = build_credential(cfg.bili)
    if credential is not None:
        account = await verify_credential(credential)
        if account:
            print(f"[凭据 OK] AI 账号：{account.describe()}")
            # 补上 dedeuserid 再重建凭据，少一个风控特征
            cfg = replace(cfg, bili=replace(cfg.bili, bot_uid=account.mid))
            credential = build_credential(cfg.bili)
            for w in account.warnings():
                print(f"[警告] {w}")
            if not account.can_speak:
                print("[中止] 该账号当前不具备发言条件，先解决上面的问题再开播。")
                return 3
        else:
            print("[警告] 凭据校验失败，cookie 可能已过期，发弹幕大概率失败")

    store = Store(cfg.data_dir / "biliauto.db")
    bot = LiveBot(cfg, store)
    client = LiveClient(cfg.bili.room_id, credential=credential, handler=bot.handle_event)

    try:
        await bot.run(client)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DeepSeek 自主运营 B 站 · 直播互动")
    p.add_argument("--dry-run", action="store_true", help="不真发弹幕，只走决策流程")
    p.add_argument("--no-speech", action="store_true", help="关闭语音播报")
    p.add_argument("--room", type=int, default=0, help="覆盖 .env 里的直播间号")
    p.add_argument("--force", action="store_true", help="配置不完整也强行启动")
    return p


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(asyncio.run(main(build_parser().parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)
