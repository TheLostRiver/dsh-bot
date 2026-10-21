"""离线回放：不连 B 站、不用真账号，验证整条决策链路。

它会假装有观众在说话，走完 策略 → 队列 → DeepSeek → 配额 → 发送 的全流程。
没有 DEEPSEEK_API_KEY 时会用离线占位脑子，链路依旧可验证。

    python scripts/replay.py
    python scripts/replay.py --fast
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biliauto.bili.danmaku import LiveEvent  # noqa: E402
from biliauto.config import AppConfig, setup_logging  # noqa: E402

SCRIPT = [
    (10001, "路人甲", "主播今天播到几点啊？", "DANMU_MSG"),
    (10002, "老粉头", "2333333", "DANMU_MSG"),
    (10003, "小海豹", "小深你在不在", "DANMU_MSG"),
    (10004, "氪金大佬", "这个画质有点糊", "SUPER_CHAT_MESSAGE"),
    (10005, "路人乙", "666", "DANMU_MSG"),
    (10006, "话痨", "你们觉得这游戏难吗？", "DANMU_MSG"),
    (10003, "小海豹", "小深你在不在", "DANMU_MSG"),  # 重复，应被去重
    (10007, "新舰长", "开通了舰长", "GUARD_BUY"),
]


async def main(args: argparse.Namespace) -> int:
    cfg = AppConfig.load()
    cfg = replace(
        cfg,
        dry_run=True,
        speech=replace(cfg.speech, enabled=args.speech),
        log_level="INFO",
        # 回放时把配额调松，否则一条完整演示要跑一分多钟；
        # 真实开播用的是 .env 里那套严格值。
        guard=replace(
            cfg.guard,
            min_interval=2.5,
            per_minute=10,
            per_hour=10,
            per_day=100,
            user_cooldown=20.0,
        ),
    )
    setup_logging(cfg.log_level)

    from biliauto.live.bot import LiveBot
    from biliauto.memory.store import Store

    db = cfg.data_dir / "replay.db"
    db.unlink(missing_ok=True)  # 每次回放都是干净状态
    store = Store(db)
    bot = LiveBot(cfg, store)

    print(f"离线脑子：{'关闭（使用 DeepSeek）' if bot.brain.online else '开启（无 API Key）'}")
    print("-" * 60)

    worker = asyncio.create_task(bot._worker())
    try:
        for uid, uname, text, kind in SCRIPT:
            print(f"  观众 {uname}: {text}")
            await bot.handle_event(
                LiveEvent(kind=kind, uid=uid, uname=uname, text=text)
            )
            if not args.fast:
                await asyncio.sleep(0.3)

        await asyncio.sleep(2.0)  # 等队列跑完
        await bot._queue.join()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if bot.speaker is not None:
            await bot.speaker.close()

    stats = store.reply_stats()
    print("-" * 60)
    print(f"收到 {len(SCRIPT)} 条 | 进入候选 {bot.considered} 条 | "
          f"实际回复 {bot.replied} 条 | 策略忽略 {bot.ignored} 条")
    print(f"数据库记录回复 {stats['total']} 条，其中成功发送 {stats['ok']} 条")
    print(f"今日用量：{store.today_usage()}")
    print("（回放模式配额已放宽，仅用于演示；真实开播见 .env）")
    store.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true", help="不模拟间隔")
    p.add_argument("--speech", action="store_true", help="同时验证 TTS（需要联网）")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main(p.parse_args())))
