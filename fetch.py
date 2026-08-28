#!/usr/bin/env python3
"""
lytvs 定时采集脚本
从 sub.lytvs.top 接口采集配置数据，保存到本地文件。
仅使用 Python 标准库，无需安装任何依赖。
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

API_BASE = "https://sub.lytvs.top/get"
TOKEN_ENV = "LYTVS_TOKEN"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "latest.json")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "archive")

TIMEOUT = 30


def fetch(token: str) -> bytes:
    """请求接口，返回原始响应体。"""
    url = f"{API_BASE}?token={token}"
    req = Request(url, headers={
        "User-Agent": "lytvs-collector/1.0",
        "Accept": "application/json",
    })
    resp = urlopen(req, timeout=TIMEOUT)
    return resp.read()


def save(data: bytes) -> tuple:
    """保存数据到 latest.json 和带时间戳的归档文件。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    ts = now.strftime("%Y%m%d_%H%M%S")

    with open(OUTPUT_FILE, "wb") as f:
        f.write(data)

    archive_path = os.path.join(ARCHIVE_DIR, f"{ts}.json")
    with open(archive_path, "wb") as f:
        f.write(data)

    return OUTPUT_FILE, archive_path


def cleanup_archives(keep: int = 30):
    """只保留最近 N 个归档文件，防止仓库膨胀。"""
    if not os.path.isdir(ARCHIVE_DIR):
        return
    files = sorted(
        (os.path.join(ARCHIVE_DIR, f) for f in os.listdir(ARCHIVE_DIR)
         if f.endswith(".json")),
        key=os.path.getmtime,
        reverse=True,
    )
    for old in files[keep:]:
        os.remove(old)
        print(f"已清理旧归档: {os.path.basename(old)}")


def main() -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"ERROR: 请设置环境变量 {TOKEN_ENV}", file=sys.stderr)
        return 1

    try:
        now_str = datetime.now(timezone(timedelta(hours=8))).isoformat()
        print(f"[{now_str}] 开始采集...")
        data = fetch(token)
        print(f"采集成功，数据大小: {len(data)} bytes")

        latest, archive = save(data)
        print(f"已保存: {os.path.relpath(latest, BASE_DIR)}")
        print(f"已归档: {os.path.relpath(archive, BASE_DIR)}")

        try:
            obj = json.loads(data)
            sites = obj.get("sites", [])
            notice = obj.get("notice", "")[:60]
            print(f"摘要: {len(sites)} 个站点 | notice: {notice}...")
        except (json.JSONDecodeError, AttributeError):
            print("警告: 响应非标准 JSON")

        cleanup_archives(keep=30)
        return 0

    except HTTPError as e:
        print(f"HTTP 错误: {e.code} {e.reason}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"网络错误: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"未知错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
