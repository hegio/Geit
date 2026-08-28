#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把采集到的原始订阅配置 (data/latest.json) 转换成 TVBox / 影视仓 可直接订阅的干净 JSON。

处理策略：
- 保留客户端真正需要的字段：spider / wallpaper / logo / danmaku / sites
- 丢弃提供商的推广字段 notice（含 TG 群号等，对客户端无意义且会随源站变化导致无意义 diff）
- 校验 sites 中每条至少含 key / name / api / type，过滤掉残缺项并告警
- 输出可读性更好的 UTF-8 JSON（indent=2）
"""
import json
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
LATEST = ROOT / "data" / "latest.json"
OUT = ROOT / "data" / "tvbox_subscribe.json"

# 允许透传给客户端的根级字段白名单（不在列表里的字段一律丢弃）
KEEP_ROOT_KEYS = ("spider", "wallpaper", "logo", "danmaku", "sites", "lives", "rules")


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
                s = dict(s)  # 不污染原数据
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
