#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集 lytvs.top 私有订阅配置（蝴蝶影视专属 Token 接口）

安全约定：
- Token 只允许通过环境变量 SUB_URL 传入（来自 GitHub Secrets），绝不硬编码。
- 返回体不含 Token，可安全落盘 / 提交。
- 多个 IP 同时用同一 Token 会触发封禁，请勿在公开 Runner 上并发跑多份。
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys
import urllib.request

SUB_URL = os.environ.get("SUB_URL")
if not SUB_URL:
    print("ERROR: 缺少环境变量 SUB_URL（应在 GitHub Secrets 中配置完整订阅 URL）", file=sys.stderr)
    sys.exit(1)

DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def main() -> int:
    raw, ctype = fetch(SUB_URL)

    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%SZ")
    day = now.strftime("%Y-%m-%d")

    is_json = "application/json" in ctype or raw.lstrip()[:1] == b"{"
    ext = "json" if is_json else "txt"

    # 1) 始终保留最新一份
    latest = DATA_DIR / f"latest.{ext}"
    latest.write_bytes(raw)

    # 2) 按 UTC 天归档（同一天覆盖，跨天新增）
    hist = HISTORY_DIR / f"{day}.{ext}"
    hist.write_bytes(raw)

    # 3) 采集日志（追加）
    sha = hashlib.sha256(raw).hexdigest()[:12]
    site_count = 0
    if is_json:
        try:
            site_count = len(json.loads(raw).get("sites", []))
        except Exception:
            site_count = -1
    log = DATA_DIR / "fetch.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} size={len(raw)} sha={sha} type={ctype} sites={site_count}\n")

    # 4) 清理过老的快照，避免仓库无限膨胀（默认保留 60 天）
    keep_days = int(os.environ.get("KEEP_DAYS", "60"))
    olds = sorted(HISTORY_DIR.glob(f"*.{ext}"))
    for old in olds[:-keep_days]:
        old.unlink()

    print(f"OK size={len(raw)} sha={sha} sites={site_count} -> {latest.name} / history/{day}.{ext}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
