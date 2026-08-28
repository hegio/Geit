#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把采集到的原始订阅配置 (data/latest.json) 转换成 TVBox / 影视仓 可直接订阅的干净 JSON。

处理策略：
- 保留客户端真正需要的字段：spider / wallpaper / logo / danmaku / sites
- 丢弃提供商的推广字段 notice（含 TG 群号等，对客户端无意义且会随源站变化导致无意义 diff）
- 校验 sites 中每条至少含 key / name / api / type，过滤掉残缺项并告警
- 输出可读性更好的 UTF-8 JSON（indent=2）

自托管（可选）：
- 若设置环境变量 RAW_BASE（如 https://raw.githubusercontent.com/<用户>/<仓库>/main/data），
  且 data/mirror/manifest.json 存在，则额外生成 tvbox_subscribe_local.json，
  把 spider / wallpaper / logo 以及各站点 api / ext / download 改写成 GitHub 上的本地文件地址，
  实现不依赖源站 lytvs.top 的完全自托管订阅。
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LATEST = DATA_DIR / "latest.json"
OUT = DATA_DIR / "tvbox_subscribe.json"
OUT_LOCAL = DATA_DIR / "tvbox_subscribe_local.json"
MANIFEST = DATA_DIR / "mirror" / "manifest.json"

# 允许透传给客户端的根级字段白名单（不在列表里的字段一律丢弃）
KEEP_ROOT_KEYS = ("spider", "wallpaper", "logo", "danmaku", "sites", "lives", "rules")
# 可能承载可下载 URL 的字段（根级 + 站点级）
URL_FIELDS = ("spider", "wallpaper", "logo", "api", "ext", "download")


def build_url_map() -> dict:
    """原始 URL -> GitHub 上的本地文件地址（需 RAW_BASE 与 manifest 同时存在）。"""
    raw_base = os.environ.get("RAW_BASE", "").rstrip("/")
    if not raw_base or not MANIFEST.exists():
        return {}
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}
    url_map = {}
    for original, info in manifest.items():
        rel = info.get("file")
        if rel:
            url_map[original] = f"{raw_base}/{rel}"
    return url_map


def main() -> int:
    if not LATEST.exists():
        print(f"ERROR: 未找到 {LATEST}，请先运行 lingdie.py 采集", file=sys.stderr)
        return 1

    try:
        raw = json.loads(LATEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: latest.json 不是合法 JSON: {e}", file=sys.stderr)
        return 1

    out: dict = {}
    for k in KEEP_ROOT_KEYS:
        if k in raw and raw[k] not in (None, "", [], {}):
            out[k] = raw[k]

    sites = raw.get("sites")
    if not isinstance(sites, list):
        print("WARN: 原始配置缺少 sites 数组，订阅将不含任何站点", file=sys.stderr)
        out["sites"] = []
    else:
        cleaned = []
        skipped = 0
        for idx, s in enumerate(sites):
            if not isinstance(s, dict):
                skipped += 1
                continue
            if not all(s.get(f) for f in ("key", "name", "api")):
                skipped += 1
                print(f"WARN: 跳过第 {idx} 条残缺站点: {s.get('key', '?')}", file=sys.stderr)
                continue
            if "type" not in s:
                s = dict(s)
                s["type"] = 3  # TVBox 默认爬虫类型
            cleaned.append(s)
        out["sites"] = cleaned
        print(f"站点校验：保留 {len(cleaned)} 条，跳过 {skipped} 条")

    OUT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    size = OUT.stat().st_size
    print(f"已生成 TVBox 订阅文件：{OUT.name}（{size} 字节，站点数 {len(out.get('sites', []))}）")

    # 可选：生成完全自托管版（把资源改写到 GitHub 本地文件）
    url_map = build_url_map()
    if url_map:
        local = rewrite_urls(out, url_map)
        OUT_LOCAL.write_text(
            json.dumps(local, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        print(f"已生成自托管订阅文件：{OUT_LOCAL.name}（{len(url_map)} 个资源已改为本地地址）")
    elif os.environ.get("RAW_BASE"):
        print("WARN: 设置了 RAW_BASE 但未找到 manifest，未生成自托管订阅（请先跑 lingdie.py 镜像）", file=sys.stderr)
    return 0


def rewrite_urls(out: dict, url_map: dict) -> dict:
    local = {k: v for k, v in out.items()}
    for f in ("spider", "wallpaper", "logo"):
        if isinstance(local.get(f), str) and local[f] in url_map:
            local[f] = url_map[local[f]]
    new_sites = []
    for s in local.get("sites", []):
        s = dict(s)
        for f in ("api", "ext", "download"):
            if isinstance(s.get(f), str) and s[f] in url_map:
                s[f] = url_map[s[f]]
        new_sites.append(s)
    local["sites"] = new_sites
    return local


if __name__ == "__main__":
    raise SystemExit(main())
