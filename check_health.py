#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check generated TVBox links and record playback-oriented health states.

This is intentionally conservative:

* HTTP 200 does not mean a channel is playable.
* HLS manifests and media segments are probed when ffprobe is available.
* A failure is recorded, not used to permanently delete a source.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_security import find_sensitive_credentials, redact_sensitive_text


HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "docs"
HEALTH_JSON = DOCS_DIR / "health.json"
HEALTH_CSV = DOCS_DIR / "health.csv"
REMOVABLE_JSON = DOCS_DIR / "removable.json"
USER_AGENT = "Mozilla/5.0 (compatible; GeitHealth/2.0)"
TIMEOUT = 15
MAX_PROBE_BYTES = 2 * 1024 * 1024
# 连续失败达到这个次数就自动删除对应直播源。
FAIL_STREAK_LIMIT = 5
# 超时不计入删除判定：境外探测对国内源超时很常见，属于误判高发区。
SOFT_FAILURE_MARKERS = ("timed out", "read operation timed out", "超时")
PROXY_FALLBACKS = (
    "https://gh-proxy.com/",
    "https://gh-proxy.org/",
    "https://ghfast.top/",
)


@dataclass
class Item:
    kind: str
    name: str
    url: str
    status: str = "unverified"
    http_status: int | None = None
    detail: str = ""
    checked_at: str = ""
    source: str = ""


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))


def is_decisive_failure(item: "Item") -> bool:
    """True when a failure proves the live source is gone, not merely slow.

    Timeouts are deliberately excluded: the health workflow runs on GitHub
    hosted runners, and domestic Chinese live sources routinely time out from
    outside the country. Counting those would delete working sources.
    """
    if item.kind != "live":
        return False
    if item.status not in {"http_error", "probe_failed", "invalid_config", "invalid_placeholder"}:
        return False
    detail = str(item.detail or "")
    if any(marker in detail for marker in SOFT_FAILURE_MARKERS):
        return False
    return True


def load_streaks() -> dict[str, dict[str, Any]]:
    """Read the previous run's failure streaks keyed by live URL."""
    if not REMOVABLE_JSON.is_file():
        return {}
    try:
        data = json.loads(REMOVABLE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("streaks", []) if isinstance(data, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("url", "")):
            result[str(entry["url"])] = entry
    return result


def update_streaks(items: list["Item"]) -> dict[str, dict[str, Any]]:
    """Fold this run's decisive live failures into the persisted streaks.

    Only live sources participate: they are fully checked every run, so a
    streak of N really means N consecutive failures. Channels are sampled and
    rotated, so a streak there would mostly record absence from the sample.
    """
    previous = load_streaks()
    current: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.kind != "live":
            continue
        url = str(item.url)
        prior = previous.get(url, {})
        history = list(prior.get("history", []))
        if is_decisive_failure(item):
            count = int(prior.get("count", 0)) + 1
            history.append({
                "checked_at": item.checked_at,
                "status": item.status,
                "detail": str(item.detail or "")[:300],
            })
            current[url] = {
                "url": url,
                "name": item.name,
                "source": str(getattr(item, "source", "") or ""),
                "count": count,
                "history": history[-FAIL_STREAK_LIMIT:],
                "removable": count >= FAIL_STREAK_LIMIT,
            }
        else:
            # A healthy or merely slow result clears the streak.
            current[url] = {
                "url": url,
                "name": item.name,
                "source": str(getattr(item, "source", "") or ""),
                "count": 0,
                "history": [],
                "removable": False,
            }
    return current


def default_merged_url() -> str:
    """Mirror fetch_tvbox.py: repo.txt locally, GITHUB_REPOSITORY in Actions."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
    if not repo:
        try:
            for raw in (HERE / "repo.txt").read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "/" in line and line != "owner/repo":
                    repo = line
                    break
        except OSError:
            pass
    if not repo or repo == "owner/repo":
        repo = "hegio/Geit"
    return "https://raw.githubusercontent.com/%s/%s/tvbox/lives/merged.m3u" % (repo, branch)


def load_items() -> list[Item]:
    items: list[Item] = []
    sources_path = DOCS_DIR / "sources.json"
    if sources_path.is_file():
        data = json.loads(sources_path.read_text(encoding="utf-8"))
        for source in data.get("sources", []):
            items.append(Item(
                kind="source",
                name=str(source.get("source", "")),
                url=str(source.get("effective_url", "")),
                status=str(source.get("status", "unverified")),
                http_status=source.get("http_status"),
                detail=str(source.get("detail", "")),
            ))
    geit_path = HERE / "geit-fish"
    if geit_path.is_file():
        config = json.loads(geit_path.read_text(encoding="utf-8"))
        merged_url = default_merged_url()
        for site in config.get("sites", []):
            if not isinstance(site, dict):
                continue
            items.append(Item(
                kind="site",
                name=str(site.get("name", "")),
                url=str(site.get("api", "")),
                status="requires_app" if site.get("type") == 3 else "unverified",
                detail="站点播放依赖影视 App 与 spider 解析",
            ))
        for live in config.get("lives", []):
            if not isinstance(live, dict):
                continue
            if str(live.get("url", "")) == merged_url:
                continue
            items.append(Item(
                kind="live",
                name=str(live.get("name", "")),
                url=str(live.get("url", "")),
                detail="待检查",
                source=str(live.get("source", "") or ""),
            ))
    merged_path = HERE / "tvbox" / "lives" / "merged.m3u"
    if merged_path.is_file():
        pending: dict[str, str] | None = None
        for raw in merged_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if line.upper().startswith("#EXTINF"):
                pending = {
                    "name": line.rsplit(",", 1)[-1].strip() if "," in line else "",
                    "group": "",
                }
            elif pending and line.startswith(("http://", "https://")):
                items.append(Item(
                    kind="channel",
                    name=pending["name"],
                    url=line,
                    detail="待检查",
                ))
                pending = None
    # Keep duplicate channel URLs: the same stream can appear under several
    # channel names, and a failure for one name must not hide another.
    seen: set[tuple[str, str, str]] = set()
    unique: list[Item] = []
    for item in items:
        key = (item.kind, item.url)
        if item.kind == "channel":
            key = (item.kind, item.name, item.url)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    items = unique
    return items


def rotate_channel_sample(items: list[Item], limit: int) -> list[Item]:
    """Rotate through channels across runs so long lists are covered over time."""
    if limit <= 0:
        return items
    channels = [item for item in items if item.kind == "channel"]
    others = [item for item in items if item.kind != "channel"]
    if not channels:
        return others
    run_index = int(os.environ.get("GITHUB_RUN_NUMBER", "0") or "0")
    start = (run_index * limit) % len(channels)
    rotated = channels[start:] + channels[:start]
    return others + rotated[:limit]


def validate_generated_configs() -> None:
    """Validate the generated configurations before writing a health report."""
    for name in ("geit-fish", "geitpy-fish"):
        path = HERE / name
        if not path.is_file():
            raise ValueError("缺少 %s" % name)
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config.get("sites"), list) or not isinstance(config.get("lives"), list):
            raise ValueError("%s 缺少 sites/lives" % name)
        if not str(config.get("spider", "")).startswith("https://"):
            raise ValueError("%s 的 spider 不是绝对 HTTPS 地址" % name)
        serialized = json.dumps(config, ensure_ascii=False)
        if "{{SECRET_" in serialized:
            raise ValueError("%s 仍包含密钥占位符" % name)
        findings = find_sensitive_credentials(config)
        if findings:
            fields = ", ".join(sorted({item.rsplit(":", 1)[-1] for item in findings}))
            raise ValueError("%s 仍包含真实凭据（%d 处，字段：%s）" % (name, len(findings), fields))
    for name in ("geit-fish", "geitpy-fish"):
        path = HERE / name
        if not path.is_file():
            raise ValueError("缺少 %s" % name)
        raw = path.read_text(encoding="utf-8-sig")
        if re.match(r"^[A-Za-z0-9_-]{8}\*\*", raw):
            raise ValueError("%s 仍是 Base64 包装格式，不是明文 JSON" % name)
        config = json.loads(raw)
        for key in ("sites", "lives", "parses", "rules", "doh", "flags", "ijk", "ads"):
            if not isinstance(config.get(key), list):
                raise ValueError("%s 缺少数组字段 %s" % (name, key))
        if not isinstance(config.get("logo"), str):
            raise ValueError("%s 的 logo 不是字符串" % name)
        if find_sensitive_credentials(config):
            raise ValueError("%s 仍包含真实凭据" % name)


def http_check(url: str) -> tuple[int | None, str]:
    if not url.startswith(("http://", "https://")):
        return None, "不是 HTTP(S) 地址"
    candidates = [url]
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.lower() == "raw.githubusercontent.com":
        candidates.extend(prefix + url for prefix in PROXY_FALLBACKS)
    last_detail = ""
    for candidate in candidates:
        # Requests cannot contain non-ASCII characters; quote only the path/query.
        parts = urllib.parse.urlsplit(candidate)
        encoded = urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@!$&'()*+,;=-._~"),
            urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&?/:@!$'()*+,;%-._~"),
            parts.fragment,
        ))
        for attempt in range(2):
            request = urllib.request.Request(encoded, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    response.read(MAX_PROBE_BYTES)
                    return int(response.status), ""
            except urllib.error.HTTPError as exc:
                last_detail = "HTTP %s" % exc.code
                # A definitive HTTP error should not trigger proxy retries.
                return int(exc.code), last_detail
            except Exception as exc:
                last_detail = str(exc)
                if attempt == 0:
                    time.sleep(0.5)
    return None, last_detail


def probe(url: str, ffprobe: str) -> tuple[str, str]:
    if not ffprobe:
        return "playback_unverified", "未找到 ffprobe"
    command = [
        ffprobe, "-v", "error", "-rw_timeout", "15000000",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", url,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
        output = (result.stdout or b"").decode("utf-8", "replace").strip()
        if result.returncode == 0 and output:
            return "probe_ok", "ffprobe 检测到媒体流: %s" % output.replace("\n", ",")
        error = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return "probe_failed", error[-1][:300] if error else "ffprobe 未检测到媒体流"
    except subprocess.TimeoutExpired:
        return "probe_failed", "ffprobe 超时"
    except Exception as exc:
        return "probe_failed", str(exc)


def probe_manifest(url: str, ffprobe: str) -> tuple[str, str]:
    """Probe one media URL found inside an HLS playlist."""
    if not ffprobe:
        return "manifest_ok", "播放列表可下载；无 ffprobe，未验证媒体分片"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            text = response.read(MAX_PROBE_BYTES).decode("utf-8", "replace")
        candidates = []
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if not re.match(r"^[a-z][a-z0-9+.-]*://", candidate, re.I):
                continue
            # A TXT channel list is not an HLS playlist. Only follow media
            # references that are valid URLs, not "channel,http://..." rows.
            if "," in candidate:
                continue
            joined = urllib.parse.urljoin(url, candidate)
            if joined.startswith(("http://", "https://")):
                candidates.append(joined)
        if not candidates:
            return "manifest_ok", "文本列表可下载；不是可直接探测的单条 HLS 播放列表"
        status, detail = probe(candidates[0], ffprobe)
        if status == "probe_ok":
            return status, "播放列表首条媒体流：" + detail
        return "probe_failed", "播放列表可下载，但首条媒体流探测失败：" + detail
    except Exception as exc:
        return "manifest_ok", "播放列表可下载，媒体分片探测未完成：" + str(exc)[:200]


def check(item: Item, ffprobe: str, do_probe: bool) -> Item:
    item.checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if item.kind == "source" and item.status in {"invalid_config", "invalid_placeholder"}:
        item.detail = "生成器未能解析该来源；格式判断需等待下一轮聚合重新确认"
        return item
    # Some type-3 sites use a built-in spider class name instead of a URL;
    # those can only be exercised inside the app.
    if item.kind == "site" and item.status == "requires_app" and not item.url.startswith(("http://", "https://")):
        item.detail = "站点使用内置 spider 标识，无法在服务端验证播放"
        return item
    was_requires_app = item.kind == "site" and item.status == "requires_app"
    status, detail = http_check(item.url)
    item.http_status = status
    if status is None:
        item.status = "http_error"
        item.detail = redact_sensitive_text(detail)
        return item
    if status >= 400:
        item.status = "http_error"
        item.detail = redact_sensitive_text(detail)
        return item
    if item.kind in {"live", "channel"}:
        if do_probe and item.url.lower().split("?", 1)[0].endswith((".m3u8", ".mpd", ".mp4", ".flv", ".ts")):
            item.status, item.detail = probe(item.url, ffprobe)
        elif item.url.lower().split("?", 1)[0].endswith((".m3u", ".txt")):
            if do_probe:
                item.status, item.detail = probe_manifest(item.url, ffprobe)
            else:
                item.status = "manifest_ok"
                item.detail = "播放列表可下载，具体频道仍需实测"
        else:
            item.status = "playback_unverified"
            item.detail = "HTTP 可访问，但无法仅凭状态码确认可播放"
    elif was_requires_app:
        item.status = "requires_app"
        item.detail = "API 地址可访问；播放仍需影视 App 与对应 spider 解析"
    else:
        item.status = "reachable"
        item.detail = "HTTP 可访问"
    return item


def write_report(items: list[Item]) -> None:
    output_dir = HEALTH_JSON.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    report_items = [
        {
            "kind": item.kind,
            "name": redact_sensitive_text(item.name),
            "url": redact_sensitive_text(item.url),
            "status": item.status,
            "http_status": item.http_status,
            "detail": redact_sensitive_text(item.detail),
            "checked_at": item.checked_at,
        }
        for item in items
    ]
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": counts,
        "coverage": {
            "checked_items": len(items),
            "note": "频道检查默认抽样；HTTP 200 不等于可播放，playback_unverified 需在影视 App 中复测",
        },
        "items": report_items,
    }
    findings = find_sensitive_credentials(report)
    if findings:
        raise ValueError(
            "健康报告脱敏后仍检测到真实凭据（%d 处，字段：%s）"
            % (len(findings), ", ".join(sorted({item.rsplit(":", 1)[-1] for item in findings})))
        )
    HEALTH_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HEALTH_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["kind", "name", "url", "status", "http_status", "detail", "checked_at"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(report["items"])
    blocked = output_dir / "blocked.csv"
    with blocked.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["kind", "name", "url", "status", "http_status", "detail", "checked_at"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            row for row in report["items"]
            if row["status"] in {"http_error", "probe_failed", "invalid_config", "invalid_placeholder"}
        )


def write_removable(items: list[Item]) -> None:
    """Record decisive live failures and flag sources that hit the streak limit.

    Reaching FAIL_STREAK_LIMIT means the source is auto-removed from geit-fish
    on the next aggregation run; below the limit it is only reported.
    """
    streaks = update_streaks(items)
    ordered = sorted(streaks.values(), key=lambda entry: (-int(entry.get("count", 0)), str(entry.get("name", ""))))
    flagged = [entry for entry in ordered if entry.get("removable")]
    pending = [entry for entry in ordered if 0 < int(entry.get("count", 0)) < FAIL_STREAK_LIMIT]
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "fail_streak_limit": FAIL_STREAK_LIMIT,
            "scope": "仅统计 geit-fish 中的 live 直播源；频道抽样轮转，不参与计数",
            "counted_failures": "连接被拒、DNS 失败、HTTP 4xx/5xx、无效流数据",
            "ignored_failures": "超时（境外探测对国内源误判率高）",
            "action": "达到上限的源会在下一次聚合时自动从 geit-fish 移除，其下频道同步从 merged.m3u 剔除",
        },
        "summary": {
            "flagged": len(flagged),
            "pending": len(pending),
            "clean": len(ordered) - len(flagged) - len(pending),
        },
        "removable": flagged,
        "pending": pending,
        "streaks": ordered,
    }
    REMOVABLE_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if flagged:
        for entry in flagged:
            log("可删除直播源（连续失败 %d 次）: %s | 来源: %s"
                % (entry["count"], entry["name"], entry.get("source") or "未知"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="用 ffprobe 探测直接媒体地址")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-channels", type=int, default=0,
                        help="限制合并 M3U 的抽样频道数；0 表示不抽样")
    parser.add_argument("--items-file", type=Path,
                        help="只检查 geit-fish 中的指定条目，供工作流分批覆盖全部条目")
    parser.add_argument("--output-dir", type=Path, default=DOCS_DIR,
                        help="健康报告输出目录")
    args = parser.parse_args()
    validate_generated_configs()
    items = load_items()
    if args.items_file and args.items_file.is_file():
        selected = json.loads(args.items_file.read_text(encoding="utf-8"))
        wanted = {
            (str(item.get("kind", "")), str(item.get("name", "")), str(item.get("url", "")))
            for item in selected
            if isinstance(item, dict)
        }
        items = [item for item in items if (item.kind, item.name, item.url) in wanted]
    items = rotate_channel_sample(items, args.max_channels)
    ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe and Path(r"C:\ffmpeg\bin\ffprobe.exe").is_file():
        ffprobe = r"C:\ffmpeg\bin\ffprobe.exe"
    log("检查 %d 个条目，ffprobe=%s" % (len(items), ffprobe or "不可用"))
    checked: list[Item] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(check, item, ffprobe, args.probe): item for item in items}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                checked.append(future.result())
            except Exception as exc:
                item = futures[future]
                item.status = "http_error"
                item.detail = str(exc)
                item.checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                checked.append(item)
            if index % 50 == 0:
                log("已检查 %d/%d" % (index, len(items)))
    checked.sort(key=lambda item: (item.kind, item.name, item.url))
    global HEALTH_JSON, HEALTH_CSV
    HEALTH_JSON = args.output_dir / "health.json"
    HEALTH_CSV = args.output_dir / "health.csv"
    global REMOVABLE_JSON
    REMOVABLE_JSON = args.output_dir / "removable.json"
    write_report(checked)
    write_removable(checked)
    log("健康报告已写入 %s" % args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
