"""单实例锁。

踩过的坑：同时开两个机器人（比如命令行 `run_live.py --dry-run` 忘了关，
又在后台点了一次启动）会同时连进同一个直播间 —— 各自一套配额和语音队列，
于是同一条弹幕被回三次、语音重复播报，而且数据库里的记录会互相交错，
看起来像"代码有并发 bug"，实际是两个进程。

用操作系统的文件锁（进程退出、甚至被强杀时内核会自动释放），
比"检查 pid 文件"可靠得多。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("biliauto.lock")


class InstanceLock:
    """独占一个锁文件。同一个进程内可以重复获取（引用计数）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._depth = 0

    # ---------- 对外 ----------
    @property
    def held(self) -> bool:
        return self._depth > 0

    def acquire(self, note: str = "") -> tuple[bool, str]:
        """成功返回 (True, "")，失败返回 (False, 占用者信息)。"""
        if self._depth > 0:  # 同进程重复获取
            self._depth += 1
            return True, ""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            self._lock_fd(fh)
        except OSError:
            # 文件被别的进程锁着，连读都可能被拒 —— 关掉再从第 1 字节读占用者信息
            try:
                fh.close()
            except Exception:
                pass
            return False, self._read_holder()

        # 锁到手了，写入自己的标识方便排查。
        # 第 0 字节留空，因为那是被锁住的区域：别的进程只能从第 1 字节往后读。
        fh.seek(0)
        fh.truncate()
        fh.write("\n" + f"pid={os.getpid()} {note}".strip())
        fh.flush()

        self._fh = fh
        self._depth = 1
        log.info("已获取单实例锁（%s）", self.path.name)
        return True, ""

    def release(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            self._unlock_fd(fh)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        log.info("已释放单实例锁")

    def _read_holder(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(1)
                return f.read().strip() or "未知进程"
        except Exception:
            return "未知进程"

    # ---------- 平台相关 ----------
    @staticmethod
    def _lock_fd(fh) -> None:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_fd(fh) -> None:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_lock: InstanceLock | None = None


def bot_lock(data_dir: Path) -> InstanceLock:
    """机器人主进程锁：同一份数据目录只允许一个机器人实体。"""
    global _lock
    if _lock is None or _lock.path != data_dir / ".bot.lock":
        _lock = InstanceLock(data_dir / ".bot.lock")
    return _lock
