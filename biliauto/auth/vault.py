"""本机凭据与设置的落盘。

两条铁律：
1. 凭据（SESSDATA 等）只落本机 data/credentials.json，永不通过 API 回显；
2. 设置（人设/配额）落 data/settings.json，可以随便看。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("biliauto.vault")

CRED_FILE = "credentials.json"
SETTINGS_FILE = "settings.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("读取 %s 失败：%s", path.name, e)
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_credential(data_dir: Path) -> dict:
    return _read_json(data_dir / CRED_FILE)


def save_credential(data_dir: Path, credential: dict) -> Path:
    path = data_dir / CRED_FILE
    _write_json(
        path,
        {
            "sessdata": credential.get("sessdata", ""),
            "bili_jct": credential.get("bili_jct", ""),
            "buvid3": credential.get("buvid3", ""),
            "dedeuserid": credential.get("dedeuserid", ""),
            "saved_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    log.info("凭据已保存到 %s", path)
    return path


def clear_credential(data_dir: Path) -> None:
    (data_dir / CRED_FILE).unlink(missing_ok=True)


def load_settings(data_dir: Path) -> dict:
    return _read_json(data_dir / SETTINGS_FILE)


def save_settings(data_dir: Path, settings: dict) -> None:
    _write_json(data_dir / SETTINGS_FILE, settings)
