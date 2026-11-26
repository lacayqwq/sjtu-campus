from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Callable


def data_dir() -> Path:
    root = Path(os.environ.get("SJTU_DATA_DIR", Path.home() / ".kimi" / "sjtu-campus"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def run(main: Callable[[], None]) -> None:
    try:
        main()
    except Exception as error:
        emit({"ok": False, "error": str(error)})
        raise SystemExit(1) from error


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def request_json(url: str, token: str | None = None) -> tuple[Any, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Kimi-SJTU-Campus/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response), response.headers
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("授权已失效，请重新配置后再试") from error
        raise RuntimeError(f"学校接口返回 HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接学校接口：{error.reason}") from error


def paginated(url: str, token: str) -> list[Any]:
    from urllib.parse import urlparse

    origin = urlparse(url)
    rows = []
    visited = set()
    while url:
        target = urlparse(url)
        if target.scheme != "https" or target.netloc != origin.netloc or not target.path.startswith("/api/v1/"):
            raise RuntimeError("学校接口返回了无效分页地址")
        if url in visited:
            raise RuntimeError("学校接口返回了重复分页地址")
        visited.add(url)
        payload, headers = request_json(url, token)
        if not isinstance(payload, list):
            raise RuntimeError("学校接口返回了无法识别的列表")
        rows.extend(payload)
        links = headers.get("Link", "")
        url = ""
        for part in links.split(","):
            if 'rel="next"' in part:
                url = part.split(";", 1)[0].strip().strip("<>")
                break
    return rows


def keychain_get(account: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "kimi-sjtu-campus", "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def keychain_set(account: str, value: str) -> None:
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            "kimi-sjtu-campus",
            "-a",
            account,
            "-w",
            value,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def dialog(message: str, *, secret: bool = False) -> str:
    hidden = " with hidden answer" if secret else ""
    script = (
        'display dialog "'
        + message.replace("\\", "\\\\").replace('"', '\\"')
        + '" default answer ""'
        + hidden
        + ' buttons {"取消","继续"} default button "继续" cancel button "取消"'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("已取消配置")
    prefix = "button returned:继续, text returned:"
    if not result.stdout.startswith(prefix):
        raise RuntimeError("无法读取本机输入")
    value = result.stdout[len(prefix) :].strip()
    if not value:
        raise RuntimeError("输入不能为空")
    return value


def iso_date(value: str) -> str:
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
