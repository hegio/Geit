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
import hashlib
import ipaddress
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
DEAD_CHANNELS_JSON = DOCS_DIR / "dead_channels.json"
CHANNEL_LEDGER_JSON = DOCS_DIR / "channel_ledger.json"
CHANNEL_HEALTH_JSON = DOCS_DIR / "channel_health.json"
# 人可读的剔除记录：每个被剔除的频道名字 + URL + 头负责人可手动恢复的位置。
REMOVED_CHANNELS_JSON = DOCS_DIR / "removed_channels.json"
REMOVED_CHANNELS_CSV = DOCS_DIR / "removed_channels.csv"
HEALTH_STATE_JSON = DOCS_DIR / "health_state.json"
USER_AGENT = "Mozilla/5.0 (compatible; GeitHealth/3.0)"
# Many IPTV gateways behave differently for player, ffmpeg, Android and Exo
# clients. Trying all of them before declaring an HTTP failure prevents the
# single-UA false negatives seen with k.061899.xyz and similar providers.
USER_AGENTS = (
    USER_AGENT,
    "Lavf/58.76.100",
    "okhttp/4.9.0",
    "ExoPlayerLib/2.18.1",
)
TIMEOUT = 15
MAX_PROBE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024
# 连续失败超过 5 次（即第 6 次）才自动删除对应直播源。
# 失效判定次数：连续 3 次确定性失效即从 merged.m3u 剔除。
# 超时、403/429/451、临时 5xx、登录/版权闺门、上游故障都不计入。
FAIL_STREAK_LIMIT = 3
# 超时不计入删除判定：境外探测对国内源超时很常见，属于误判高发区。
SOFT_FAILURE_MARKERS = ("timed out", "read operation timed out", "超时")
SOFT_NETWORK_MARKERS = (
    "network is unreachable",
    "temporary failure",
    "name or service not known",
    "nodename nor servname",
    "connection reset",
    "remote end closed connection",
    "connection refused",
    "connection aborted",
    "ssl",
    "certificate",
    "temporary",
    "server returned 5xx",
)
# These status codes often mean region lock, authorization or rate limiting.
# They are useful diagnostics but must not count toward auto-removal.
SOFT_HTTP_STATUSES = {401, 403, 407, 408, 425, 429, 451}
# A 200 whose body is a JSON gate ("由于版权原因，请登录后观看")
# means the source needs a session, not that it is gone.
AUTH_GATE_MARKERS = (
    "请登录",
    "登录后观看",
    "版权原因",
    "需要登录",
    "未授权",
    "please login",
    "please sign in",
    "sign in to",
    "log in to",
    "unauthorized",
    "authentication required",
)
HARD_HTTP_STATUSES = {404, 410}
# 单条 404 的重试间隔（秒）。只对硬失效重试一次，用于区分 CDN 节点抖动
# 与真实下线；v.lfthirtytwo.com / vip.lzcdn2.com 实测单请求 404 率 25-50%。
CONFIRM_RETRY_DELAY = 1.5
PROXY_FALLBACKS = (
    "https://gh-proxy.com/",
    "https://gh-proxy.org/",
    "https://ghfast.top/",
)
# Domestic carrier headends and intranet-only domains. A GitHub runner cannot
# reach these, so probing them would only produce false dead-link verdicts.
# Everything else is probed, including private/adult sections.
ISP_INTRANET_SUFFIXES = (
    ".gmcc.net",       # 广东移动 OTT 头端
    ".itv.cmvideo.cn",  # 移动咪咕
    ".chinanetcenter.com",
)
ISP_INTRANET_HOST_MARKERS = (
    "ott.cctv.cn",
    ".hw.gmcc.net",
    "gslbserv.",
    "rrs0",
)
MERGED_PROXY_PREFIX = "https://gh-proxy.com/"
# Public HLS stream used once per run to prove ffprobe can actually decode.
# Without it a crashing binary mislabels every link as dead.
PROBE_SELF_TEST_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"


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
    source_line: int = 0
    channel_key: str = ""
    category: str = ""
    url_hash: str = ""
    final_url: str = ""
    content_type: str = ""
    failure_class: str = ""
    evidence: str = ""
    attempts: int = 0
    host: str = ""
    redirect_count: int = 0


@dataclass
class HttpResult:
    status: int | None
    detail: str
    final_url: str = ""
    content_type: str = ""
    body: bytes = b""
    redirects: list[str] | None = None
    user_agent: str = ""
    attempts: int = 0
    error: str = ""
    failure_class: str = ""


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))


def _m3u_attr(line: str, name: str) -> str:
    match = re.search(r'(?:^|[\s,])%s="([^"]*)"' % re.escape(name), line, re.I)
    return match.group(1).strip() if match else ""


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
    # Check environmental/soft evidence before trusting failure_class.  A
    # probe can be labelled hard by an older caller and still contain a
    # timeout or 5XX detail; that must never count toward deletion.
    if item.failure_class in {"soft", "internal", "unverified"}:
        return False
    if is_soft_failure(item):
        return False
    if item.failure_class == "hard":
        return (
            item.http_status in HARD_HTTP_STATUSES
            or item.status in {"probe_failed", "invalid_config", "invalid_placeholder"}
        )
    if item.http_status in HARD_HTTP_STATUSES:
        return True
    if item.http_status is not None and item.http_status >= 400:
        return False
    return item.status in {"probe_failed", "invalid_config", "invalid_placeholder"}


def is_soft_failure(item: "Item") -> bool:
    """True when a failure is likely environmental and must never delete a line."""
    detail = str(item.detail or "").lower()
    if item.http_status in SOFT_HTTP_STATUSES:
        return True
    if item.http_status is not None and item.http_status >= 500:
        return True
    if item.failure_class == "soft":
        return True
    if re.search(r"\b(?:http\s*)?(?:401|403|407|408|425|429|451|5\d\d)\b", detail, re.I):
        return True
    if re.search(r"\b5xx\b", detail, re.I):
        return True
    return any(marker in detail for marker in SOFT_FAILURE_MARKERS + SOFT_NETWORK_MARKERS)


def _soft_failure_text(detail: str) -> bool:
    """Return True when a probe/error message is environmental, not decisive."""
    text = str(detail or "").lower()
    if any(marker in text for marker in AUTH_GATE_MARKERS):
        return True
    if any(marker in text for marker in SOFT_FAILURE_MARKERS + SOFT_NETWORK_MARKERS):
        return True
    return bool(
        re.search(r"\b(?:http\s*)?(?:401|403|407|408|425|429|451|5\d\d)\b", text, re.I)
        or re.search(r"\b5xx\b", text, re.I)
    )


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
                "failure_class": item.failure_class,
                "http_status": item.http_status,
                "host": item.host,
            })
            current[url] = {
                "url": url,
                "name": item.name,
                "source": str(getattr(item, "source", "") or ""),
                "count": count,
                "history": history[-FAIL_STREAK_LIMIT:],
                "removable": count >= FAIL_STREAK_LIMIT,
                "failure_class": item.failure_class,
                "host": item.host,
            }
        elif item.status == "probe_ok":
            # Only a decoded media stream is strong evidence that the source
            # has recovered. A reachable URL, a downloadable manifest, or a
            # timeout must not erase a prior decisive-failure streak.
            current[url] = {
                "url": url,
                "name": item.name,
                "source": str(getattr(item, "source", "") or ""),
                "count": 0,
                "history": [],
                "removable": False,
                "failure_class": "",
                "host": item.host,
            }
        elif item.status == "skip_internal":
            # Private/adult feeds are never probed. Preserve their prior state
            # instead of inventing a recovery or another decisive failure.
            if prior:
                current[url] = {
                    **prior,
                    "url": url,
                    "name": item.name,
                    "source": str(getattr(item, "source", "") or ""),
                    "removable": False,
                    "host": item.host,
                }
        else:
            # Preserve decisive-failure history for every non-probe result,
            # including soft failures and playback_unverified. Otherwise a
            # runner outage or a URL whose container is merely reachable can
            # silently reset the five-check safety window.
            if prior:
                current[url] = {
                    **prior,
                    "url": url,
                    "name": item.name,
                    "source": str(getattr(item, "source", "") or ""),
                    "removable": False,
                    "host": item.host,
                }
            else:
                current[url] = {
                    "url": url,
                    "name": item.name,
                    "source": str(getattr(item, "source", "") or ""),
                    "count": 0,
                    "history": [],
                    "removable": False,
                    "failure_class": "",
                    "host": item.host,
                }
    return current


def channel_is_private_or_internal(item: "Item") -> bool:
    """True only for sources that a GitHub runner genuinely cannot reach.

    The earlier keyword list matched on the group title, so every channel in
    "私密频道_169169" and "JAV點播_169169" was skipped forever even though they
    are ordinary public CDN hosts such as t33.cdn2020.com. That left 3,467
    channels unmonitored. Now only真·内网 targets are exempt: a private or
    loopback IP literal, or an ISP intranet hostname. Everything else,
    including the private/adult sections, is probed like any other channel.
    """
    # A literal private/loopback address is unreachable from outside by design.
    host = urllib.parse.urlsplit(str(item.url)).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_private or address.is_loopback
    host_l = host.lower()
    if not host_l:
        return False
    # Domestic ISP intranet hostnames: multicast/OTT headends and carrier
    # domains that only resolve inside the carrier network.
    if host_l.endswith(ISP_INTRANET_SUFFIXES):
        return True
    if any(marker in host_l for marker in ISP_INTRANET_HOST_MARKERS):
        return True
    return False


def url_digest(url: str) -> str:
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def safe_channel_url(url: str) -> str:
    """Remove credentials while retaining a URL that is useful for diagnostics."""
    parts = urllib.parse.urlsplit(str(url))
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if normalized in {
            "accountinfo", "accesstoken", "apikey", "auth", "authorization",
            "authtoken", "bearer", "clientsecret", "cookie", "key", "password",
            "passwd", "pwd", "refreshtoken", "secret", "session", "sessionid",
            "sig", "sign", "signature", "token",
        }:
            continue
        safe_query.append((key, value))
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc.rsplit("@", 1)[-1],
        parts.path,
        urllib.parse.urlencode(safe_query, doseq=True),
        "",
    ))


def write_channel_health(items: list[Item]) -> None:
    """Write a conservative channel-level ledger.

    This report never removes a channel. It classifies each URL into
    keep/observe/candidate so a later, separately reviewed step can act.
    """
    ordered = _channel_health_rows(items)
    summary: dict[str, int] = {}
    for row in ordered:
        summary[row["verdict"]] = summary.get(row["verdict"], 0) + 1
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "scope": "仅频道 URL；只报告，不自动删除 merged.m3u；未抽样频道保留历史记录",
            "remove_limit": FAIL_STREAK_LIMIT,
            "soft_failures": "超时、网络不可达、5xx、403/429、播放未验证只观察，不计入删除",
            "internal_sources": "运营商内网/私网地址跳过，不因境外探测失败判定失效",
            "evidence": "每条记录保存最终 URL、Content-Type、失败类别、尝试 UA 数和重定向次数",
        },
        "summary": summary,
        "lines": ordered,
    }
    if find_sensitive_credentials(report):
        raise ValueError("频道健康报告包含真实凭据，已中止写入")
    CHANNEL_HEALTH_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _channel_health_rows(items: list[Item]) -> list[dict[str, Any]]:
    """Build one health row per channel URL, carrying history across runs."""
    previous: dict[str, dict[str, Any]] = {}
    current_hashes: set[str] = set()
    if CHANNEL_HEALTH_JSON.is_file():
        try:
            data = json.loads(CHANNEL_HEALTH_JSON.read_text(encoding="utf-8"))
            for entry in data.get("lines", []):
                if isinstance(entry, dict):
                    key = str(entry.get("url_hash", "") or url_digest(entry.get("url", "")))
                    previous[key] = entry
        except (OSError, json.JSONDecodeError):
            previous = {}
    if CHANNEL_LEDGER_JSON.is_file():
        try:
            ledger = json.loads(CHANNEL_LEDGER_JSON.read_text(encoding="utf-8"))
            for entry in ledger.get("channels", []):
                if not isinstance(entry, dict):
                    continue
                for line in entry.get("lines", []):
                    if isinstance(line, dict) and line.get("url_hash"):
                        current_hashes.add(str(line["url_hash"]))
        except (OSError, json.JSONDecodeError):
            current_hashes = set()
    # Preserve unsampled channel history, but drop URLs that no longer exist.
    # An empty ledger means a build problem, not that every old URL vanished.
    lines: dict[str, dict[str, Any]] = {
        key: entry for key, entry in previous.items()
        if not current_hashes or key in current_hashes
    }
    for item in items:
        if item.kind != "channel":
            continue
        key = item.url_hash or url_digest(item.url)
        old = previous.get(key, {})
        decisive = (
            item.failure_class == "hard"
            and item.status != "upstream_incident"
            and not is_soft_failure(item)
        )
        soft = is_soft_failure(item) or item.status in {
            "playback_unverified", "manifest_ok", "upstream_incident", "skip_internal",
        }
        internal = channel_is_private_or_internal(item)
        count = int(old.get("decisive_failures", 0) or 0)
        if decisive and not internal:
            count += 1
        # Only a decoded media stream proves recovery. A downloadable manifest
        # or a reachable API endpoint must not erase a decisive failure streak.
        elif item.status == "probe_ok":
            count = 0
        if internal:
            verdict = "skip_internal"
        elif item.status == "upstream_incident":
            verdict = "observe_upstream"
        elif decisive and count >= FAIL_STREAK_LIMIT:
            verdict = "candidate_remove"
        elif decisive:
            verdict = "observe"
        elif soft:
            verdict = "observe_soft"
        else:
            verdict = "keep"
        lines[key] = {
            "url": safe_channel_url(item.url),
            "url_hash": key,
            "name": item.name,
            "channel_key": item.channel_key,
            "category": item.category,
            "source": item.source,
            "source_line": item.source_line,
            "status": item.status,
            "http_status": item.http_status,
            "final_url": safe_channel_url(item.final_url),
            "content_type": item.content_type,
            "failure_class": item.failure_class,
            "evidence": redact_sensitive_text(item.evidence),
            "attempts": item.attempts,
            "host": item.host,
            "redirect_count": item.redirect_count,
            "detail": redact_sensitive_text(item.detail),
            "checked_at": item.checked_at,
            "decisive_failures": count,
            "soft_failure": soft,
            "internal": internal,
            "verdict": verdict,
        }
    ordered = sorted(
        lines.values(),
        key=lambda row: (row["verdict"], row["name"], row.get("url", "")),
    )
    return ordered


def write_dead_channels(items: list[Item]) -> None:
    """List channel URLs that failed decisively often enough to stop re-listing.

    The unit of removal is a single channel URL, never the live group that
    carries it: a group such as "裤佬IPTV直播" may hold hundreds of URLs and
    only the dead ones should stop being written to merged.m3u. Groups are
    added and removed by hand, so the aggregator must never drop a whole group
    on its own.
    """
    doomed = [
        row for row in _channel_health_rows(items)
        if row.get("verdict") == "candidate_remove"
    ]
    ordered = sorted(doomed, key=lambda row: (str(row.get("name", "")), str(row.get("url", ""))))
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "scope": "仅 merged.m3u 中的单个频道 URL；不删除直播大类，不改 geit-fish",
            "remove_limit": FAIL_STREAK_LIMIT,
            "action": "达到连续失败上限的频道 URL 不再写入 merged.m3u；恢复为 probe_ok 后自动重新录入",
            "counted_failures": "仅高置信确定性失效：404/410、明确拒绝、持续无效媒体流等",
            "ignored_failures": "超时、403/429/451、临时 5xx、上游整体故障不计入也不清空",
        },
        "summary": {
            "dead": len(ordered),
            "hosts": len({str(row.get("host", "")) for row in ordered if row.get("host")}),
        },
        "removed_urls": [str(row.get("url", "")) for row in ordered],
        "lines": ordered,
    }
    if find_sensitive_credentials(report):
        raise ValueError("失效频道清单包含真实凭据，已中止写入")
    DEAD_CHANNELS_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if ordered:
        for row in ordered:
            log("失效频道 URL（连续失败 %d 次，不再录入 merged.m3u）: %s | %s | 来源: %s" % (
                row.get("decisive_failures", 0),
                row.get("name"),
                row.get("url"),
                row.get("source") or "未知",
            ))


def recover_removed_channels(items: list[Item], ffprobe: str = "", do_probe: bool = True) -> list[Item]:
    """Re-probe URLs that a previous run removed but this run never sampled.

    Removal is driven by the channel ledger, and the sampling cursor only
    covers a slice of it per run. A URL deleted during one run can therefore
    stay absent from merged.m3u for several runs even after it recovers. The
    removed list is small (tens to a few hundred entries), so re-checking all
    of it every run is cheap and removes that blind spot entirely.

    Returns the freshly checked items; they carry a probe_ok status when the
    URL is playable again, which clears the decisive-failure streak and pulls
    the URL back into merged.m3u on the next aggregation.
    """
    if not REMOVED_CHANNELS_JSON.is_file():
        return []
    try:
        data = json.loads(REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    previous = data.get("channels", []) if isinstance(data, dict) else []
    if not isinstance(previous, list):
        return []
    already_checked = {item.url for item in items if item.kind == "channel"}
    candidates = [
        row for row in previous
        if isinstance(row, dict) and str(row.get("url", "")) not in already_checked
    ]
    if not candidates:
        return []
    # Private/ISP-intranet hosts are skipped by check(); probing them again
    # every run would waste the budget and could resurrect a dead entry.
    probeable: list[Item] = []
    for row in candidates:
        item = Item(
            kind="channel",
            name=str(row.get("name", "")),
            url=str(row.get("url", "")),
            status="unverified",
            source=str(row.get("source", "") or ""),
            source_line=int(row.get("source_line", 0) or 0),
            channel_key=str(row.get("channel_key", "") or ""),
            category=str(row.get("category", "") or ""),
        )
        if channel_is_private_or_internal(item):
            continue
        probeable.append(item)
    if not probeable:
        return []
    log("复检 %d 个历史剔除 URL（不在本轮抽样范围内）" % len(probeable))
    recovered: list[Item] = []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(probeable)))) as pool:
        futures = {pool.submit(check, item, ffprobe, do_probe): item for item in probeable}
        for future in as_completed(futures):
            try:
                recovered.append(future.result())
            except Exception as exc:
                item = futures[future]
                item.status = "http_error"
                item.failure_class = "soft"
                item.detail = "复检异常：%s" % exc
                item.checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                recovered.append(item)
    recovered.sort(key=lambda item: (item.name, item.url))
    healthy = sum(1 for item in recovered if item.status == "probe_ok")
    if healthy:
        log("复检发现 %d 个已恢复的频道 URL，将重新录入 merged.m3u" % healthy)
    return recovered


def write_removed_channels(items: list[Item]) -> None:
    """Record every channel URL dropped from merged.m3u, with its name.

    A human needs three things to act on a removal: which channel it was, the
    URL to test by hand, and where it came from so the upstream can be fixed.
    The JSON keeps the full record; the CSV opens directly in a spreadsheet.
    Entries accumulate across runs and are cleared only when a URL comes back
    as probe_ok, so the file stays a complete removal history.

    URLs removed by an earlier run are re-checked by recover_removed_channels()
    before this runs, so a source deleted during a CDN flap recovers within one
    run instead of staying out of merged.m3u until the cursor reaches it again.
    """
    doomed = [
        row for row in _channel_health_rows(items)
        if row.get("verdict") == "candidate_remove"
    ]
    # Preserve history: an entry stays listed until its URL recovers.
    previous: list[dict[str, Any]] = []
    if REMOVED_CHANNELS_JSON.is_file():
        try:
            data = json.loads(REMOVED_CHANNELS_JSON.read_text(encoding="utf-8"))
            rows = data.get("channels", []) if isinstance(data, dict) else []
            previous = [r for r in rows if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            previous = []
    recovered = {
        str(row.get("url", ""))
        for row in _channel_health_rows(items)
        if row.get("verdict") in {"keep", "observe_soft", "observe_upstream", "observe", "skip_internal"}
    }
    history = {
        str(row.get("url", "")): row
        for row in previous
        if str(row.get("url", "")) not in recovered
    }
    # A URL leaves this file only when it is re-probed and no longer counts as
    # a removal candidate. Anything still doomed must stay listed, so the
    # "recovered" count is exactly the set that dropped out.
    doomed_urls = {str(row.get("url", "")) for row in doomed}
    recovered_urls = {
        str(row.get("url", "")) for row in previous
        if str(row.get("url", "")) in recovered
        and str(row.get("url", "")) not in doomed_urls
    }
    for row in doomed:
        key = str(row.get("url", ""))
        entry = dict(history.get(key) or {})
        entry.update({
            "name": row.get("name", ""),
            "url": key,
            "category": row.get("category", ""),
            "host": row.get("host", ""),
            "source": row.get("source", ""),
            "source_line": row.get("source_line", ""),
            "failure_class": row.get("failure_class", ""),
            "http_status": row.get("http_status", ""),
            "evidence": row.get("evidence", ""),
            "decisive_failures": row.get("decisive_failures", 0),
            "removed_at": entry.get("removed_at")
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_checked": row.get("checked_at", ""),
        })
        history[key] = entry
    ordered = sorted(
        history.values(),
        key=lambda r: (str(r.get("removed_at", "")), str(r.get("name", ""))),
        reverse=True,
    )
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "scope": "从 merged.m3u 剔除的单个频道 URL；直播大类从不自动剔除",
            "remove_limit": FAIL_STREAK_LIMIT,
            "counted_failures": "仅硬失效：404/410、明确拒绝、分片无法解码",
            "ignored_failures": "超时、403/429/451、临时 5xx、登录/版权闺门、上游故障不计入",
            "recovery": "恢复为 probe_ok 后自动从本清单移除并重新录入 merged.m3u",
        },
        "summary": {
            "removed_total": len(ordered),
            "removed_this_run": len(doomed),
            "recovered_this_run": len(recovered_urls),
            "hosts": len({str(r.get("host", "")) for r in ordered if r.get("host")}),
        },
        "channels": ordered,
    }
    if find_sensitive_credentials(report):
        raise ValueError("剔除记录含敏感凭证，拒绝写入")
    REMOVED_CHANNELS_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    header = "removed_at,name,category,url,host,source,source_line,http_status,decisive_failures,evidence"
    lines = [header]
    for row in ordered:
        cells = [
            str(row.get("removed_at", "")),
            str(row.get("name", "")),
            str(row.get("category", "")),
            str(row.get("url", "")),
            str(row.get("host", "")),
            str(row.get("source", "")),
            str(row.get("source_line", "")),
            str(row.get("http_status", "")),
            str(row.get("decisive_failures", 0)),
            str(row.get("evidence", "")).replace("\n", " ")[:200],
        ]
        lines.append(",".join('"%s"' % c.replace('"', '""') for c in cells))
    REMOVED_CHANNELS_CSV.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    if doomed:
        log("本轮剔除 %d 个失效频道 URL，详情见 %s"
            % (len(doomed), REMOVED_CHANNELS_JSON.name))
    if recovered_urls:
        log("本轮恢复 %d 个此前剔除的频道 URL，已重新录入 merged.m3u" % len(recovered_urls))


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
    return MERGED_PROXY_PREFIX + (
        "https://raw.githubusercontent.com/%s/%s/tvbox/lives/merged.m3u" % (repo, branch)
    )


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
                    "group": _m3u_attr(line, "group-title"),
                    "tvg_id": _m3u_attr(line, "tvg-id"),
                }
            elif pending and line.startswith(("http://", "https://")):
                items.append(Item(
                    kind="channel",
                    name=pending["name"],
                    url=line,
                    detail="待检查",
                    channel_key=pending["tvg_id"],
                    category=pending["group"],
                    url_hash=url_digest(line),
                ))
                pending = None
    ledger_path = CHANNEL_LEDGER_JSON
    if ledger_path.is_file():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ledger = {}
        by_hash: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for entry in ledger.get("channels", []):
            if not isinstance(entry, dict):
                continue
            for line in entry.get("lines", []):
                if isinstance(line, dict) and line.get("url_hash"):
                    by_hash[str(line["url_hash"])] = (entry, line)
        for item in items:
            if item.kind != "channel":
                continue
            match = by_hash.get(item.url_hash)
            if not match:
                continue
            entry, line = match
            item.channel_key = str(entry.get("channel_key", "") or item.channel_key)
            item.category = str(entry.get("category", "") or item.category)
            item.source = str(line.get("source", "") or "")
            item.source_line = int(line.get("source_line", 0) or 0)
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


def apply_host_circuit_breaker(items: list[Item]) -> None:
    """Downgrade host-wide failures so one upstream outage cannot delete lines.

    A host is considered suspect when at least six of its checked lines failed
    in the same run and at least half of the host's checked lines failed.
    Those results stay visible as ``upstream_incident`` and never count toward
    the per-URL decisive-failure streak.
    """
    checked = [item for item in items if item.kind in {"channel", "live"}]
    by_host: dict[str, list[Item]] = {}
    for item in checked:
        if item.host:
            by_host.setdefault(item.host, []).append(item)
    for host, rows in by_host.items():
        failures = [row for row in rows if row.status in {"http_error", "probe_failed"}]
        if len(rows) < 6 or len(failures) < 6 or len(failures) * 2 < len(rows):
            continue
        hard = [row for row in failures if row.failure_class == "hard"]
        if len(hard) < 3 or len(hard) * 2 < len(rows):
            continue
        for row in failures:
            row.failure_class = "soft"
            row.status = "upstream_incident"
            row.detail = "同一主机本轮 %d/%d 条同时失败，按上游故障处理，不累计删除" % (
                len(failures), len(rows)
            )


def rotate_channel_sample(items: list[Item], limit: int) -> list[Item]:
    """Rotate through channels using a persistent cursor, not run numbers."""
    if limit <= 0:
        return items
    channels = [item for item in items if item.kind == "channel"]
    others = [item for item in items if item.kind != "channel"]
    if not channels:
        return others
    # Private/ISP-intranet channels are never probed (see check()), so they must
    # not consume the sample budget. Otherwise a cursor that lands inside a
    # private block returns nothing but skipped lines and the run loses its
    # probe capacity entirely.
    probeable = [item for item in channels if not channel_is_private_or_internal(item)]
    if not probeable:
        return others
    channels = probeable
    state = load_health_state()
    urls = state.get("urls", {}) if isinstance(state.get("urls", {}), dict) else {}
    # New or recently failing URLs are checked first. A probe_ok URL remains
    # eligible, but only after the backlog has had a fair chance.
    def priority(item: Item) -> tuple[int, str]:
        record = urls.get(item.url_hash or url_digest(item.url), {})
        if not record:
            return (0, item.url)
        if record.get("failure_class") in {"hard", "soft"}:
            return (1, str(record.get("last_checked", "")))
        if record.get("status") == "probe_ok":
            return (3, str(record.get("last_checked", "")))
        return (2, str(record.get("last_checked", "")))

    ordered = sorted(channels, key=priority)
    # Preserve a round-robin cursor inside each priority band. This prevents a
    # large backlog from starving one host while still covering new entries.
    start = int(state.get("next_channel_offset", 0) or 0) % len(ordered)
    rotated = ordered[start:] + ordered[:start]
    return others + rotated[:limit]


def load_health_state() -> dict[str, Any]:
    if not HEALTH_STATE_JSON.is_file():
        return {}
    try:
        value = json.loads(HEALTH_STATE_JSON.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_health_state(
    all_items: list[Item],
    checked_items: list[Item],
    sample_limit: int,
    budget_seconds: int,
    started_at: float,
    attempted_at: str = "",
) -> None:
    """Persist a stable cursor and per-URL evidence across workflow runs."""
    previous = load_health_state()
    previous_offset = int(previous.get("next_channel_offset", 0) or 0)
    previous_run = previous.get("last_run", {})
    if not isinstance(previous_run, dict):
        previous_run = {}
    old_urls = previous.get("urls", {}) if isinstance(previous.get("urls", {}), dict) else {}
    # Only retain entries that still exist in this run's generated inputs.
    # Otherwise the committed state file grows forever as sources are removed.
    current_hashes = {
        item.url_hash or url_digest(item.url)
        for item in all_items
        if item.kind in {"live", "channel"}
    }
    urls = {
        key: value
        for key, value in old_urls.items()
        if isinstance(value, dict) and (not current_hashes or key in current_hashes)
    }
    for item in checked_items:
        key = item.url_hash or url_digest(item.url)
        prior = urls.get(key, {})
        urls[key] = {
            **prior,
            "url_hash": key,
            "url": safe_channel_url(item.url),
            "name": item.name,
            "kind": item.kind,
            "host": item.host,
            "status": item.status,
            "failure_class": item.failure_class,
            "http_status": item.http_status,
            "content_type": item.content_type,
            "attempts": item.attempts,
            "last_checked": item.checked_at,
            "last_success": item.checked_at if item.status == "probe_ok" else str(prior.get("last_success", "") or ""),
            "last_evidence": redact_sensitive_text(item.evidence)[:500],
        }
    # The cursor indexes the same filtered list rotate_channel_sample() uses.
    # Counting every channel here (including never-probed private ones) would
    # desynchronise the cursor from the sampled list and skip real channels.
    channel_count = sum(
        1 for item in all_items
        if item.kind == "channel" and not channel_is_private_or_internal(item)
    )
    checked_channels = sum(1 for item in checked_items if item.kind == "channel")
    # The cursor is advanced by the full requested sample. The priority queue
    # ensures URLs not reached because of a timeout budget remain at the front
    # on the next run, while URLs already probed move behind them.
    requested_channels = min(
        sample_limit if sample_limit > 0 else channel_count,
        channel_count,
    )
    next_offset = (previous_offset + requested_channels) % channel_count if channel_count else 0
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state = {
        "updated": finished_at,
        "next_channel_offset": next_offset,
        "channel_count": channel_count,
        "sample_limit": sample_limit,
        "last_run": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
            "attempted_at": attempted_at or str(previous_run.get("attempted_at", "") or ""),
            "finished_at": finished_at,
            "checked_items": len(checked_items),
            "checked_channels": checked_channels,
            "budget_seconds": budget_seconds,
        },
        "urls": urls,
    }
    HEALTH_STATE_JSON.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def encode_url(url: str) -> str:
    """Percent-encode only the path/query so urllib accepts non-ASCII URLs."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&?/:@!$'()*+,;%-._~"),
        parts.fragment,
    ))


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_once(url: str, user_agent: str, read_bytes: int) -> HttpResult:
    redirects = _RedirectRecorder()
    opener = urllib.request.build_opener(redirects)
    request = urllib.request.Request(
        encode_url(url),
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        },
    )
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(max(1, read_bytes))
            return HttpResult(
                status=int(response.status),
                detail="",
                final_url=response.geturl(),
                content_type=response.headers.get("Content-Type", ""),
                body=body,
                redirects=redirects.chain,
                user_agent=user_agent,
                attempts=1,
            )
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            pass
        return HttpResult(
            status=int(exc.code),
            detail="HTTP %s" % exc.code,
            final_url=exc.geturl(),
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            body=body,
            redirects=redirects.chain,
            user_agent=user_agent,
            attempts=1,
            error=str(exc),
        )
    except Exception as exc:
        return HttpResult(
            status=None,
            detail=str(exc),
            redirects=redirects.chain,
            user_agent=user_agent,
            attempts=1,
            error=str(exc),
        )


def _classify_http_result(result: HttpResult) -> str:
    status = result.status
    if status is None:
        return "soft"
    if status in HARD_HTTP_STATUSES:
        return "hard"
    if status in SOFT_HTTP_STATUSES or status >= 500:
        return "soft"
    if status >= 400:
        return "soft"
    return ""


def http_check(url: str, read_bytes: int = 4096) -> HttpResult:
    """Try compatible player UAs before treating an HTTP failure as decisive."""
    if not url.startswith(("http://", "https://")):
        return HttpResult(None, "不是 HTTP(S) 地址", failure_class="soft")
    candidates = [url]
    parts = urllib.parse.urlsplit(url)
    if parts.netloc.lower() == "raw.githubusercontent.com":
        candidates.extend(prefix + url for prefix in PROXY_FALLBACKS)
    # A hard 404 that comes back as 200 on a retry proves the failure was a
    # CDN node hiccup, not a dead link. Measured on v.lfthirtytwo.com and
    # vip.lzcdn2.com: roughly 25-50% of single requests return 404 while the
    # stream plays fine. Without a retry, four UAs hitting the same bad node
    # in one instant produced "all UAs failed" and pushed live sources toward
    # the removal threshold.
    confirm_delay = CONFIRM_RETRY_DELAY
    attempts = 0
    errors: list[str] = []
    attempt_results: list[HttpResult] = []
    for candidate in candidates:
        for user_agent in USER_AGENTS:
            attempts += 1
            result = _request_once(candidate, user_agent, read_bytes)
            result.attempts = attempts
            result.failure_class = _classify_http_result(result)
            attempt_results.append(result)
            if result.status is not None and result.status < 400:
                return result
            errors.append("%s %s" % (user_agent.split("/", 1)[0], result.detail))
            # Only a hard status earns a retry. Timeouts, 5xx, 403/429/451 and
            # network errors are already treated as environmental and must not
            # cost an extra request.
            if result.failure_class == "hard" and confirm_delay:
                time.sleep(confirm_delay)
                confirm_delay = 0
                attempts += 1
                retry = _request_once(candidate, user_agent, read_bytes)
                retry.attempts = attempts
                retry.failure_class = _classify_http_result(retry)
                attempt_results.append(retry)
                if retry.status is not None and retry.status < 400:
                    return retry
                errors.append("%s(重试) %s" % (
                    user_agent.split("/", 1)[0], retry.detail
                ))
            # A 5xx/403/429 from every client is still environmental. Continue
            # trying UAs because some gateways only reject unknown clients.
            if result.status is not None and result.status not in HARD_HTTP_STATUSES:
                continue
            # For 404/410, another UA may still succeed; only stop after all
            # compatible clients have independently failed.
    # Keep the strongest evidence from the final attempt rather than issuing
    # a fifth request. If that result is a hard status, require every UA to
    # have failed before allowing it to count as decisive.
    # A hard status is decisive only when every compatible client received a
    # hard status. If one client timed out, hit a 5xx, or was rejected, the
    # result remains environmental and cannot contribute to deletion.
    if attempt_results and all(
        row.status in HARD_HTTP_STATUSES for row in attempt_results
    ):
        result.failure_class = "hard"
    else:
        result.failure_class = "soft"
    result.attempts = attempts
    result.detail = " | ".join(errors[-4:]) or result.detail
    if any(_soft_failure_text(item) for item in errors):
        result.failure_class = "soft"
    if result.failure_class == "hard":
        # A hard verdict requires a clean sweep: every request in this call
        # must have returned a hard status. One retry per UA is allowed, so a
        # CDN node that flaps 404/200 cannot look like a dead source.
        result.failure_class = "hard" if all(
            row.status in HARD_HTTP_STATUSES for row in attempt_results
        ) else "soft"
    return result


def _ffprobe_headers(user_agent: str, referer: str = "") -> list[str]:
    headers = ["-user_agent", user_agent]
    if referer:
        headers.extend(["-headers", "Referer: %s\r\n" % referer])
    return headers


def probe(
    url: str,
    ffprobe: str,
    user_agent: str = "",
    referer: str = "",
) -> tuple[str, str]:
    """Probe one media URL. A referer is forwarded because several hosts
    (the 163189 family) reject requests that arrive without one."""
    if not ffprobe:
        return "playback_unverified", "未找到 ffprobe"
    # A binary that segfaults on the first real stream reports rc=139 for
    # every URL, which used to be recorded as "probe_failed" and pushed
    # healthy live sources toward the deletion threshold. Treat a crash as an
    # unavailable probe instead of a dead link.
    agents = [user_agent] if user_agent else list(USER_AGENTS)
    errors: list[str] = []
    for agent in agents:
        command = [
            ffprobe, "-v", "error", "-rw_timeout", "15000000",
            "-analyzeduration", "5000000", "-probesize", "5000000",
            *_ffprobe_headers(agent, referer),
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", url,
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=35)
            output = (result.stdout or b"").decode("utf-8", "replace").strip()
            if result.returncode == 0 and output:
                return "probe_ok", "ffprobe(%s) 检测到媒体流: %s" % (
                    agent.split("/", 1)[0],
                    output.replace("\n", ","),
                )
            # Negative return codes are the shell reporting a signal: -11 is
            # SIGSEGV, -6 SIGABRT. A crash says nothing about the stream.
            # Shells also report a signal as 128+n (139 = SIGSEGV), so cover
            # both encodings.
            if result.returncode is not None and (
                result.returncode < 0 or result.returncode >= 128
            ):
                return "playback_unverified", (
                    "ffprobe 进程崩溃（信号 %d），本机探测不可用，不判定失效"
                    % (result.returncode if result.returncode >= 128 else -result.returncode)
                )
            error = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            errors.append(error[-1][:220] if error else "ffprobe 未检测到媒体流")
        except subprocess.TimeoutExpired:
            errors.append("ffprobe 超时")
        except Exception as exc:
            errors.append(str(exc))
        if _soft_failure_text(errors[-1]):
            break
    joined = " | ".join(errors)
    if _soft_failure_text(joined):
        return "manifest_ok", "探测超时/网络抖动，保留观察：" + joined[:300]
    return "probe_failed", joined[:300] if joined else "ffprobe 未检测到媒体流"


def _manifest_media_lines(text: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return HLS variants and media references, resolving relative URLs."""
    variants: list[str] = []
    media: list[str] = []
    lines = text.splitlines()
    variant_lines: set[int] = set()
    for index, raw in enumerate(lines):
        if index in variant_lines:
            continue
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXT-X-STREAM-INF"):
            for child_index, child in enumerate(lines[index + 1:], index + 1):
                child = child.strip()
                if not child:
                    continue
                if child.startswith("#"):
                    break
                joined = urllib.parse.urljoin(base_url, child)
                if joined.startswith(("http://", "https://")):
                    variants.append(joined)
                    variant_lines.add(child_index)
                break
            continue
        if line.startswith("#"):
            match = re.search(r'URI="([^"]+)"', line, re.I)
            if upper.startswith("#EXT-X-MAP") and match:
                joined = urllib.parse.urljoin(base_url, match.group(1))
                if joined.startswith(("http://", "https://")):
                    media.append(joined)
            continue
        if "," in line:
            continue
        joined = urllib.parse.urljoin(base_url, line)
        if joined.startswith(("http://", "https://")):
            media.append(joined)
    return variants, media


def probe_manifest(url: str, ffprobe: str, user_agent: str = "") -> tuple[str, str]:
    """Download a manifest, follow master variants, and verify media segments."""
    if not ffprobe:
        return "manifest_ok", "播放列表可下载；无 ffprobe，未验证媒体分片"
    agents = [user_agent] if user_agent else list(USER_AGENTS)
    last_error = ""
    for agent in agents:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": agent})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read(MAX_MANIFEST_BYTES).decode("utf-8", "replace")
                final_url = response.geturl()
            variants, candidates = _manifest_media_lines(text, final_url)
            if variants:
                # Prefer the last (usually lowest-bandwidth) variant for a
                # quick health check, then fall back to the first few variants.
                candidates = list(reversed(variants))[:3] + candidates
            if not candidates:
                return "manifest_ok", "文本列表可下载；不是可直接探测的单条 HLS 播放列表"
            errors: list[str] = []
            for candidate in candidates[:5]:
                status, detail = probe(candidate, ffprobe, agent, final_url)
                if status == "probe_ok":
                    return "probe_ok", "播放列表媒体分片可解码：" + detail
                errors.append(detail)
            joined_errors = " | ".join(error for error in errors if error)
            if _soft_failure_text(joined_errors):
                return "manifest_ok", "播放列表可下载，分片探测超时/网络抖动，保留观察：" + joined_errors[:300]
            last_error = joined_errors
        except Exception as exc:
            last_error = str(exc)
            if _soft_failure_text(last_error):
                return "manifest_ok", "播放列表探测超时，保留观察：" + last_error[:200]
    if _soft_failure_text(last_error):
        return "manifest_ok", "播放列表可下载，分片探测遇到网络/上游错误，保留观察：" + last_error[:300]
    return "probe_failed", "播放列表可下载，但采样分片均不可解码：" + last_error[:300]


def check(item: Item, ffprobe: str, do_probe: bool) -> Item:
    item.checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    item.host = host_of(item.url)
    # Private/adult sections are intentionally excluded from automated health
    # probes: their upstreams require app-side context and should not be
    # requested or published in a health report.
    if item.kind in {"live", "channel"} and channel_is_private_or_internal(item):
        item.status = "skip_internal"
        item.detail = "私密/运营商内网来源，跳过自动探测，避免误删与敏感请求"
        return item
    if item.kind == "source" and item.status in {"invalid_config", "invalid_placeholder"}:
        item.detail = "生成器未能解析该来源；格式判断需等待下一轮聚合重新确认"
        return item
    # Some type-3 sites use a built-in spider class name instead of a URL;
    # those can only be exercised inside the app.
    if item.kind == "site" and item.status == "requires_app" and not item.url.startswith(("http://", "https://")):
        item.detail = "站点使用内置 spider 标识，无法在服务端验证播放"
        return item
    was_requires_app = item.kind == "site" and item.status == "requires_app"
    http_result = http_check(item.url, 64 * 1024 if item.kind in {"live", "channel"} else 4096)
    item.http_status = http_result.status
    item.final_url = http_result.final_url
    item.content_type = http_result.content_type
    item.attempts = http_result.attempts
    item.redirect_count = len(http_result.redirects or [])
    if http_result.status is None:
        item.status = "http_error"
        item.failure_class = "soft"
        item.evidence = "所有兼容 UA 均未取得 HTTP 响应"
        item.detail = redact_sensitive_text(http_result.detail)
        return item
    if http_result.status >= 400:
        item.status = "http_error"
        item.failure_class = http_result.failure_class or "soft"
        item.evidence = "HTTP %s；尝试 %d 次；Content-Type=%s；重定向 %d 次" % (
            http_result.status,
            http_result.attempts,
            http_result.content_type or "未知",
            item.redirect_count,
        )
        item.detail = redact_sensitive_text(http_result.detail)
        return item
    if item.kind in {"live", "channel"}:
        body_prefix = http_result.body[:4096].lstrip()
        body_head = http_result.body[:8192].upper()
        is_playlist = (
            b"#EXTM3U" in body_head
            or "mpegurl" in http_result.content_type.lower()
        )
        # A playlist body proves HLS even when the server says text/plain and
        # the URL carries no extension. Such links are playable in a real
        # client; only ffprobe cannot guess the format, so resolve the media
        # lines and probe a segment instead of declaring the link dead.
        is_extensionless_hls = (
            b"#EXTM3U" in body_head
            and not item.url.lower().split("?", 1)[0].endswith(
                (".m3u8", ".m3u", ".mpd", ".mp4", ".flv", ".ts", ".txt")
            )
        )
        is_manifest_list = item.url.lower().split("?", 1)[0].endswith((".m3u", ".txt"))
        media_suffix = item.url.lower().split("?", 1)[0].endswith((".m3u8", ".mpd", ".mp4", ".flv", ".ts"))
        if is_manifest_list and do_probe:
            # A TXT/M3U channel bundle can contain hundreds of channels. Never
            # hand that list to ffprobe as if it were one media stream.
            item.status, item.detail = probe_manifest(
                http_result.final_url or item.url, ffprobe, http_result.user_agent
            )
        elif is_extensionless_hls and do_probe:
            item.status, item.detail = probe_manifest(
                http_result.final_url or item.url, ffprobe, http_result.user_agent
            )
        elif is_playlist and do_probe:
            if b"#EXT-X-STREAM-INF" in http_result.body[:MAX_MANIFEST_BYTES].upper():
                item.status, item.detail = probe_manifest(
                    http_result.final_url or item.url, ffprobe, http_result.user_agent
                )
            else:
                item.status, item.detail = probe(
                    http_result.final_url or item.url, ffprobe, http_result.user_agent
                )
        elif (media_suffix or (body_prefix and not body_prefix.startswith((b"<", b"{", b"[")))) and do_probe:
            item.status, item.detail = probe(http_result.final_url or item.url, ffprobe, http_result.user_agent)
        elif is_manifest_list:
            item.status = "manifest_ok"
            item.detail = "播放列表可下载，具体频道仍需实测"
        else:
            item.status = "playback_unverified"
            item.detail = "HTTP 可访问，但正文/类型未能确认是媒体或 HLS 播放列表"
        # A JSON gate body (login/copyright) is an auth requirement, not a
        # dead link. Append it so the soft-failure gate can see it.
        gate_text = http_result.body[:2048].decode("utf-8", "replace")
        if _soft_failure_text(gate_text):
            item.detail = "%s | 响应体要求登录/授权: %s" % (
                item.detail, gate_text.strip().replace("\n", " ")[:160]
            )
        if item.status in {"probe_failed", "manifest_ok"} and _soft_failure_text(item.detail):
            item.failure_class = "soft"
        elif item.status == "probe_failed":
            item.failure_class = "hard"
        else:
            item.failure_class = ""
        item.evidence = "HTTP %s；UA=%s；Content-Type=%s；重定向 %d 次" % (
            http_result.status,
            http_result.user_agent.split("/", 1)[0],
            http_result.content_type or "未知",
            item.redirect_count,
        )
    elif was_requires_app:
        item.status = "requires_app"
        item.detail = "API 地址可访问；播放仍需影视 App 与对应 spider 解析"
        item.evidence = "HTTP %s；Content-Type=%s" % (
            http_result.status,
            http_result.content_type or "未知",
        )
    else:
        item.status = "reachable"
        item.detail = "HTTP 可访问"
        item.evidence = "HTTP %s；Content-Type=%s" % (
            http_result.status,
            http_result.content_type or "未知",
        )
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
            "final_url": safe_channel_url(item.final_url),
            "content_type": item.content_type,
            "failure_class": item.failure_class,
            "evidence": redact_sensitive_text(item.evidence),
            "attempts": item.attempts,
            "host": item.host,
            "redirect_count": item.redirect_count,
            "detail": redact_sensitive_text(item.detail),
            "checked_at": item.checked_at,
            "source": redact_sensitive_text(item.source),
            "source_line": item.source_line,
        }
        for item in items
    ]
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": counts,
        "coverage": {
            "checked_items": len(items),
            "note": "多 UA、重定向、Content-Type 和 HLS/ffprobe 联合探测；HTTP 200 不等于可播放",
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
    csv_fields = [
        "kind", "name", "url", "status", "http_status", "failure_class",
        "host", "attempts", "content_type", "detail", "checked_at",
        "source", "source_line", "final_url", "evidence", "redirect_count",
    ]
    with HEALTH_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=csv_fields,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(report["items"])
    blocked = output_dir / "blocked.csv"
    with blocked.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=csv_fields,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(
            row for row in report["items"]
            if row["status"] in {"http_error", "probe_failed", "invalid_config", "invalid_placeholder"}
        )


def write_removable(items: list[Item]) -> None:
    """Record decisive live failures for humans to act on.

    Live groups are never removed automatically. A group is a hand-curated
    category such as "裤佬IPTV直播"; deleting it would take every channel it
    carries with it, including healthy ones. This report therefore only
    reports the streak, and the aggregation workflow no longer reads it to
    drop anything. The thing that actually stops being re-listed is a single
    dead channel URL, written by write_dead_channels().
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
            "counted_failures": "仅高置信确定性失效：404/410、明确拒绝、持续无效媒体流等",
            "ignored_failures": "超时、403/429/451 等地区限制/鉴权/限流、临时 5xx；不计入也不清空既有失败历史",
            "recovery": "只有 probe_ok（ffprobe 成功解码媒体流）才清零；reachable、manifest_ok、playback_unverified 均保留历史",
            "action": "仅报告，不自动删除任何直播大类；直播大类由人工增减",
            "channel_urls": "真正会被停止录入的是 merged.m3u 中的单个频道 URL，见 dead_channels.json",
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
            log("直播大类连续失败 %d 次（仅报告，不自动删除）: %s | 来源: %s"
                % (entry["count"], entry["name"], entry.get("source") or "未知"))


def write_ai_review(output_dir: Path | None = None) -> dict[str, Any]:
    """Run the optional AI review without ever affecting health decisions."""
    try:
        import ai_assist

        mode = os.environ.get("AI_MODE", "none").strip().lower()
        return ai_assist.write_ai_review(mode, output_dir)
    except Exception as exc:
        log("AI 辅助失败，健康检查继续（fail-open）: %s" % exc)
        return {"status": "unavailable", "provider": "", "suggestions": 0}


def main() -> int:
    started_at = time.time()
    attempted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="用 ffprobe 探测直接媒体地址")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-channels", type=int, default=0,
                        help="限制合并 M3U 的抽样频道数；0 表示不抽样")
    parser.add_argument("--budget-seconds", type=int, default=0,
                        help="本轮时间预算；到达后保留游标，剩余频道留给下一轮")
    parser.add_argument("--items-file", type=Path,
                        help="只检查 geit-fish 中的指定条目，供工作流分批覆盖全部条目")
    parser.add_argument("--output-dir", type=Path, default=DOCS_DIR,
                        help="健康报告输出目录")
    args = parser.parse_args()
    global HEALTH_STATE_JSON
    HEALTH_STATE_JSON = args.output_dir / "health_state.json"
    # Record the attempt before network work. If this run is cancelled or its
    # report cannot be committed, the gate can retry instead of waiting
    # another full 48 hours from a failed attempt.
    attempt_state = load_health_state()
    old_run = attempt_state.get("last_run", {})
    if not isinstance(old_run, dict):
        old_run = {}
    attempt_state["last_run"] = {**old_run, "attempted_at": attempted_at}
    HEALTH_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_STATE_JSON.write_text(
        json.dumps(attempt_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
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
    all_items = items
    items = rotate_channel_sample(items, args.max_channels)
    ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe and Path(r"C:\ffmpeg\bin\ffprobe.exe").is_file():
        ffprobe = r"C:\ffmpeg\bin\ffprobe.exe"
    # A ffprobe that crashes on real streams would label every URL
    # probe_failed and push healthy sources toward deletion. Verify the
    # binary against a public test stream once, then drop it for the whole
    # run if it cannot decode anything. HTTP checks still happen.
    if args.probe and ffprobe:
        status, detail = probe(PROBE_SELF_TEST_URL, ffprobe)
        if status != "probe_ok":
            log("WARN ffprobe 自检未通过（%s: %s），本轮退化为仅 HTTP 探测" % (status, detail[:200]))
            ffprobe = ""
            args.probe = False
        else:
            log("ffprobe 自检通过: %s" % detail[:160])
    log("检查 %d 个条目，ffprobe=%s" % (len(items), ffprobe or "不可用"))
    checked: list[Item] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(check, item, ffprobe, args.probe): item for item in items}
        try:
            for index, future in enumerate(as_completed(futures), 1):
                if args.budget_seconds and time.time() - started_at >= args.budget_seconds:
                    log("达到 %d 秒时间预算，本轮提前结束" % args.budget_seconds)
                    break
                try:
                    checked.append(future.result())
                except Exception as exc:
                    item = futures[future]
                    item.status = "http_error"
                    item.failure_class = "soft"
                    item.detail = str(exc)
                    item.checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    checked.append(item)
                if index % 50 == 0:
                    log("已检查 %d/%d" % (index, len(items)))
        finally:
            for pending_future in futures:
                pending_future.cancel()
    global HEALTH_JSON, HEALTH_CSV
    HEALTH_JSON = args.output_dir / "health.json"
    HEALTH_CSV = args.output_dir / "health.csv"
    global REMOVABLE_JSON
    REMOVABLE_JSON = args.output_dir / "removable.json"
    global DEAD_CHANNELS_JSON
    DEAD_CHANNELS_JSON = args.output_dir / "dead_channels.json"
    global CHANNEL_HEALTH_JSON
    CHANNEL_HEALTH_JSON = args.output_dir / "channel_health.json"
    global REMOVED_CHANNELS_JSON, REMOVED_CHANNELS_CSV
    REMOVED_CHANNELS_JSON = args.output_dir / "removed_channels.json"
    REMOVED_CHANNELS_CSV = args.output_dir / "removed_channels.csv"
    # URLs removed by an earlier run are re-checked even when the cursor never
    # reaches them, so a source deleted during a CDN flap recovers within one
    # run instead of staying out of merged.m3u for several runs.
    if REMOVED_CHANNELS_JSON.is_file():
        checked.extend(recover_removed_channels(checked, ffprobe, args.probe))
        checked.sort(key=lambda item: (item.kind, item.name, item.url))
    apply_host_circuit_breaker(checked)
    write_report(checked)
    write_removable(checked)
    write_channel_health(checked)
    # Channel URLs are the unit of removal; the live group that carries them
    # is only reported so a human can decide.
    write_dead_channels(checked)
    write_removed_channels(checked)
    write_health_state(
        all_items,
        checked,
        args.max_channels,
        args.budget_seconds,
        started_at,
        attempted_at,
    )
    ai_status = write_ai_review(args.output_dir)
    log("AI 辅助状态: %s" % ai_status.get("status", "unknown"))
    log("健康报告已写入 %s" % args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
