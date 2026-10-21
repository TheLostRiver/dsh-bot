"""启动 Web 管理后台。

    python scripts/run_web.py            # http://127.0.0.1:8787
    python scripts/run_web.py --port 9000
    python scripts/run_web.py --no-browser

只绑定 127.0.0.1，局域网访问不到 —— 凭据在本机，别往外开。
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="dsh-bot 管理后台")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = p.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n  dsh-bot 管理后台\n  {url}\n  Ctrl+C 退出\n")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "biliauto.web.app:app",
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
