#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build TVBox configurations from the sources listed in sources.txt.

The collector publishes exactly two subscription files, both as plain
(unencrypted) JSON written to extension-less paths:

* geit-fish: playable TVBox configuration with sites and live entries
* geitpy-fish: geit-fish plus every valid Python spider in py/

Supporting derived files:

* tvbox/lives/merged.m3u: de-duplicated live channels from TXT/M3U sources
* docs/sources.json: a human-readable source inventory
* docs/tvbox.json: compatibility copy of geit-fish for the legacy endpoint
* docs/inventory.json and docs/inventory.csv: per-run source/site/live counts

The health report (docs/health.json, docs/health.csv, docs/blocked.csv) is
owned exclusively by check_health.py and is never written here.

Third-party source files are not committed verbatim. This avoids leaking
tokens, cookies and other credentials that are often embedded in TVBox
configurations.
"""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config_security import find_sensitive_credentials, redact_sensitive_text


HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "sources.txt"
REPO_FILE = HERE / "repo.txt"
PY_DIR = HERE / "py"
RECYCLE_DIR = HERE / "回收"
TVBOX_DIR = HERE / "tvbox"
LIVE_DIR = TVBOX_DIR / "lives"
DOCS_DIR = HERE / "docs"
# 两个订阅文件都没有后缀，影视 App 会直接按 JSON 解析；带 .txt 后缀的旧名已停用。
GEIT_PATH = HERE / "geit-fish"
GEITPY_PATH = HERE / "geitpy-fish"
MERGED_M3U_PATH = LIVE_DIR / "merged.m3u"
# 分类规则与代码分离。以后增减 sources.txt 时只需维护这份 JSON，
# 新来源会自动套用规则；未命中项进入待确认报告，但不会从 M3U 删除。
CHANNEL_RULES_PATH = TVBOX_DIR / "channel_rules.json"
# 直播源出处：健康报告据此标注每个 live 来自 sources.txt 的哪一行。
LIVE_SOURCE_KEY = "source"
# 健康工作流写出的失效频道 URL 清单，达到连续失败上限即不再录入 merged.m3u。
DEAD_CHANNELS_PATH = DOCS_DIR / "dead_channels.json"

USER_AGENT = "Mozilla/5.0 (compatible; GeitCollector/2.0; +https://github.com/hegio/Geit)"
TIMEOUT = 30
RETRY = 3
MAX_SOURCE_BYTES = 32 * 1024 * 1024
SECRET_PLACEHOLDER = re.compile(r"\{\{\s*SECRET_[^}]+\}\}", re.I)
PROXY_PREFIXES = (
    "https://gh-proxy.com/",
    "https://gh-proxy.org/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://ghproxy.com/",
)
PROXY_FALLBACKS = (
    "https://gh-proxy.com/",
    "https://gh-proxy.org/",
    "https://ghfast.top/",
)

# 影视 App 侧实测可用：raw.githubusercontent.com 的 spider.jar 在部分网络下
# 取不到，cnb.cool 这份镜像 HEAD 200 且 App 能正常加载。
SPIDER_JAR_URL = "https://cnb.cool/fish2035/xs/-/git/raw/main/spider.jar"
# 旧版本写入的地址，App 侧取不到 JAR，发现残留时强制换回 SPIDER_JAR_URL。
LEGACY_SPIDER_URL = "https://raw.githubusercontent.com/hegio/Geit/main/spider.jar"

# 影视 App 侧直连 raw.githubusercontent.com 不稳定，仓库自带 py 统一走 ghfast.top 镜像。
PY_PROXY_PREFIX = "https://ghfast.top/"
# ghfast.top 只代理 GitHub 主机，其他域名的 py 链接加前缀会得到 403，必须保持原样。
PROXIED_PY_HOSTS = ("raw.githubusercontent.com",)
# 影视 App 侧读取本仓库聚合 M3U 时统一走这个镜像；只代理仓库文件引用，
# 不能给 M3U 内部的直播流地址加前缀，否则播放器会把镜像当成直播域名。
MERGED_PROXY_PREFIX = "https://gh-proxy.com/"


@dataclass
class Channel:
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    source: str = ""
    source_line: int = 0
    group_raw: str = ""
    # TXT 的 "#genre# 前小节名" 与 "后的分组名" 是两种信息。前者常包含
    # 私密频道、抖音、咪咕等真实来源语义，不能因为后者是 DE=3 就丢掉。
    source_section: str = ""
    protocol: str = ""
    channel_key: str = ""
    quality: str = ""
    lines: list[str] = field(default_factory=list)


@dataclass
class SourceResult:
    source: str
    effective_url: str
    kind: str
    status: str
    detail: str = ""
    http_status: int | None = None
    content_type: str = ""
    bytes: int = 0
    sites: list[dict[str, Any]] = field(default_factory=list)
    lives: list[dict[str, Any]] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    python_files: dict[str, bytes] = field(default_factory=dict)
    config_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildStats:
    sources: list[SourceResult] = field(default_factory=list)
    sites: list[dict[str, Any]] = field(default_factory=list)
    lives: list[dict[str, Any]] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    python_files: dict[str, bytes] = field(default_factory=dict)
    config_meta: dict[str, Any] = field(default_factory=dict)


def log(message: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message))


def load_repo() -> tuple[str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
    if not repo:
        try:
            for raw in REPO_FILE.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "/" in line and line != "owner/repo":
                    repo = line
                    break
        except OSError:
            pass
    if not repo or repo == "owner/repo":
        repo = "hegio/Geit"
    return repo, branch


def raw_url(repo: str, branch: str, path: str) -> str:
    quoted = urllib.parse.quote(path.replace("\\", "/"), safe="/")
    return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, quoted)


def normalize_url(value: str) -> str:
    """Remove a known GitHub proxy prefix and percent-encode non-ASCII parts."""
    value = value.strip()
    for prefix in PROXY_PREFIXES:
        if value.startswith(prefix):
            inner = value[len(prefix):]
            if inner.startswith(("http://", "https://")):
                value = inner
            break
    parts = urllib.parse.urlsplit(value)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&?/:@!$'()*+,;%-._~")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def proxied_py_url(url: str) -> str:
    """Prepend the ghfast.top mirror to py links hosted on proxied GitHub hosts.

    Non-GitHub hosts (for example third-party jsnz.pages.dev) are returned
    unchanged because ghfast.top answers those with HTTP 403.
    """
    # normalize_url strips any existing proxy prefix and re-encodes the path,
    # so this is safe to apply to a URL that already went through it.
    value = normalize_url(url)
    host = urllib.parse.urlsplit(value).netloc.lower()
    if host not in PROXIED_PY_HOSTS:
        return value
    return PY_PROXY_PREFIX + value


def fetch(url: str) -> tuple[bytes, int, str, str]:
    """Fetch a URL with certificate verification enabled."""
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    normalized = normalize_url(url)
    candidates = [normalized]
    raw_parts = urllib.parse.urlsplit(normalized)
    if raw_parts.netloc.lower() == "raw.githubusercontent.com":
        candidates.extend(prefix + normalized for prefix in PROXY_FALLBACKS)
    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in range(RETRY):
            try:
                req = urllib.request.Request(candidate, headers=request_headers)
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as response:
                    data = response.read(MAX_SOURCE_BYTES + 1)
                    if len(data) > MAX_SOURCE_BYTES:
                        raise ValueError("响应超过 32 MiB 上限")
                    return data, int(response.status), response.headers.get("Content-Type", ""), candidate
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < RETRY:
                    time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("下载失败: %s" % last_error)


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def normalized_bytes(data: bytes) -> bytes:
    """Collapse CRLF to LF so hashing is stable across Windows and CI checkouts."""
    return data.replace(b"\r\n", b"\n")


def content_digest(data: bytes) -> str:
    return hashlib.sha256(normalized_bytes(data)).hexdigest()


def is_encrypted_payload(text: str) -> bool:
    value = text.strip()
    if len(value) < 80 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    if not value.startswith("2423"):
        return False
    marker = value.find("2324", 4)
    if marker < 4:
        return False
    cipher_hex = value[marker + 4:-26]
    return bool(cipher_hex) and len(cipher_hex) % 32 == 0


def decrypt_payload(text: str) -> bytes | None:
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError:
        log("WARN 缺少 pycryptodome，无法解密加密配置")
        return None
    value = text.strip()
    marker = value.find("2324", 4)
    if marker < 4:
        return None
    prefix_len = marker + 4
    cipher_hex = value[prefix_len:-26]
    if not cipher_hex or len(cipher_hex) % 32:
        return None
    try:
        key_text = bytes.fromhex(value[4:marker]).decode("utf-8", "ignore")
        iv_text = bytes.fromhex(value[-26:]).decode("utf-8", "ignore")
        key = key_text.ljust(16, "0")[:16].encode("utf-8")
        iv = iv_text.ljust(16, "0")[:16].encode("utf-8")
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(bytes.fromhex(cipher_hex))
        padding = plain[-1]
        if not 1 <= padding <= 16 or plain[-padding:] != bytes([padding]) * padding:
            return None
        plain = plain[:-padding]
        json.loads(plain.decode("utf-8-sig"))
        return plain
    except (ValueError, IndexError, UnicodeError, json.JSONDecodeError):
        return None


def parse_json(data: bytes) -> Any | None:
    text = decode_text(data).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        wrapped = re.fullmatch(r"[A-Za-z0-9_-]{8}\*\*([A-Za-z0-9+/=\s]+)", text)
        if wrapped:
            try:
                plain = base64.b64decode(re.sub(r"\s+", "", wrapped.group(1)), validate=True)
                return json.loads(plain.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        if not is_encrypted_payload(text):
            return None
        plain = decrypt_payload(text)
        if plain is None:
            return None
        try:
            return json.loads(plain.decode("utf-8-sig"))
        except json.JSONDecodeError:
            return None


def is_txt(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    channel_lines = 0
    for line in lines[:300]:
        if line.startswith("#"):
            continue
        if "#genre#" in line or re.match(r"^[^,]+,\s*https?://", line):
            channel_lines += 1
    return channel_lines >= max(1, min(3, len(lines) // 4))


def is_m3u(text: str) -> bool:
    return "#EXTM3U" in text[:2048].upper()


def parse_txt_channels(text: str, source: str = "", source_line: int = 0) -> list[Channel]:
    channels: list[Channel] = []
    group = ""
    source_section = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#genre#"):
            group = line.split("#genre#", 1)[1].strip(" ,")
            source_section = ""
            continue
        if line.startswith("#"):
            continue
        if "#genre#" in line:
            name, rest = line.split(",", 1) if "," in line else (line, "")
            group = rest.replace("#genre#", "").strip(" ,") or name.strip()
            source_section = name.strip()
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if name and re.match(r"^https?://", url, re.I):
            channels.append(Channel(
                name=name,
                url=url,
                group=group,
                group_raw=group,
                source_section=source_section,
                source=source,
                source_line=source_line,
            ))
    return channels


def _m3u_attr(line: str, name: str) -> str:
    match = re.search(r'(?:^|[\s,])%s="([^"]*)"' % re.escape(name), line, re.I)
    return match.group(1).strip() if match else ""


def parse_m3u_channels(text: str, source: str = "", source_line: int = 0) -> list[Channel]:
    channels: list[Channel] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF"):
            label = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            pending = {
                "name": label,
                "group": _m3u_attr(line, "group-title"),
                "logo": _m3u_attr(line, "tvg-logo"),
                "tvg_id": _m3u_attr(line, "tvg-id"),
            }
        elif line and not line.startswith("#") and re.match(r"^[a-z][a-z0-9+.-]*://", line, re.I):
            if pending and pending["name"]:
                channels.append(Channel(
                    url=line,
                    source=source,
                    source_line=source_line,
                    group_raw=pending["group"],
                    **pending,
                ))
            pending = None
    return channels


def sanitize_site(site: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(site, dict):
        return None, "站点不是对象"
    item = dict(site)
    api = str(item.get("api", "")).strip()
    if not api:
        return None, "缺少 api"
    if SECRET_PLACEHOLDER.search(api) or SECRET_PLACEHOLDER.search(str(item.get("ext", ""))):
        return None, "invalid_placeholder"
    if find_sensitive_credentials(item):
        return None, "sensitive_credentials"
    if api.startswith("./") or api.startswith("../"):
        return None, "相对 api 无法在订阅中解析"
    return item, ""


def sanitize_live(live: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(live, dict):
        return None, "直播项不是对象"
    item = dict(live)
    url = str(item.get("url", "")).strip()
    if not url:
        return None, "缺少直播地址"
    if SECRET_PLACEHOLDER.search(url):
        return None, "invalid_placeholder"
    if find_sensitive_credentials(item):
        return None, "sensitive_credentials"
    if url.startswith("./") or url.startswith("../"):
        return None, "相对直播地址无法在订阅中解析"
    if re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", url, re.I):
        return None, "图片地址不是直播列表"
    return item, ""


def normalize_config_references(value: Any, base_url: str) -> Any:
    """Resolve relative references while preserving non-path TVBox identifiers."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in {"api", "url", "ext", "jar", "logo", "epg"}:
                candidate = item.strip()
                if candidate.startswith(("./", "../")):
                    candidate = urllib.parse.urljoin(base_url, candidate)
                result[key] = normalize_url(candidate) if candidate.startswith(("http://", "https://")) else candidate
            elif isinstance(item, (dict, list)):
                result[key] = normalize_config_references(item, base_url)
            else:
                result[key] = item
        return result
    if isinstance(value, list):
        return [normalize_config_references(item, base_url) for item in value]
    return value


def extract_python_refs(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in {"api", "ext", "jar"}:
                if re.search(r"\.py(?:$|[?#])", item, re.I):
                    found.append(item)
            elif isinstance(item, (dict, list)):
                found.extend(extract_python_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(extract_python_refs(item))
    return found


def valid_python(data: bytes) -> bool:
    try:
        source = decode_text(data)
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    has_spider = any(isinstance(node, ast.ClassDef) and node.name == "Spider" for node in ast.walk(tree))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return has_spider and bool(methods & {
        "homeContent", "categoryContent", "detailContent", "searchContent", "playerContent",
    })


def safe_python_name(url: str, data: bytes, existing: dict[str, bytes]) -> str:
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    if not name.lower().endswith(".py"):
        name += ".py"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "spider.py"
    digest = content_digest(data)[:10]
    candidate = name
    if candidate in existing and existing[candidate] != data:
        candidate = "%s-%s.py" % (Path(name).stem, digest)
    return candidate


def python_name_rank(name: str) -> tuple[bool, int, str]:
    """Prefer a short canonical filename over a generated hash-suffixed copy."""
    has_hash_suffix = bool(re.search(r"-[0-9a-f]{10}\.py$", name, re.I))
    return has_hash_suffix, len(name), name.lower()


def deduplicate_python_data(files: dict[str, bytes]) -> dict[str, bytes]:
    groups: dict[str, list[tuple[str, bytes]]] = {}
    for name, data in files.items():
        groups.setdefault(content_digest(data), []).append((name, data))
    result: dict[str, bytes] = {}
    for entries in groups.values():
        canonical_name, data = min(entries, key=lambda item: python_name_rank(item[0]))
        result[canonical_name] = data
    return result


def quarantine_python(path: Path, category: str) -> None:
    """Move duplicate or invalid Python out of the build without deleting it."""
    target_dir = RECYCLE_DIR / category
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        digest = content_digest(path.read_bytes())[:10]
        target = target_dir / ("%s-%s%s" % (path.stem, digest, path.suffix))
    shutil.move(str(path), str(target))
    log("已移入回收/%s: %s" % (category, path.name))


def deduplicate_python_directory() -> None:
    PY_DIR.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[Path]] = {}
    for path in sorted(PY_DIR.glob("*.py")):
        data = path.read_bytes()
        if not valid_python(data):
            # Syntax compatibility can differ between local and CI Python
            # versions. Keep the file for inspection instead of deleting it.
            log("WARN 保留无效 Python（不参与配置）: %s" % path.name)
            continue
        groups.setdefault(content_digest(data), []).append(path)
    for paths in groups.values():
        keep = min(paths, key=lambda path: python_name_rank(path.name))
        for duplicate in paths:
            if duplicate != keep:
                quarantine_python(duplicate, "重复Python")


def load_local_python() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    PY_DIR.mkdir(parents=True, exist_ok=True)
    deduplicate_python_directory()
    for path in sorted(PY_DIR.glob("*.py")):
        data = path.read_bytes()
        if valid_python(data):
            result[path.name] = data
        else:
            log("WARN 跳过无效 Python: %s" % path.name)
    return result


def collect_one(
    source: str,
    local_python: dict[str, bytes],
    source_line: int = 0,
) -> SourceResult:
    effective = normalize_url(source)
    result = SourceResult(source=source, effective_url=effective, kind="unknown", status="unverified")
    try:
        data, status, content_type, effective = fetch(source)
        result.effective_url = effective
        result.http_status = status
        result.content_type = content_type
        result.bytes = len(data)
    except Exception as exc:
        result.status = "http_error"
        result.detail = redact_sensitive_text(exc)
        return result

    text = decode_text(data)
    parsed = parse_json(data)
    if isinstance(parsed, dict):
        result.kind = "tvbox_json"
        result.status = "reachable"
        parsed = normalize_config_references(parsed, effective)
        for key in ("parses", "rules", "doh", "flags", "ijk", "ads"):
            if key in parsed:
                result.config_meta[key] = parsed[key]
        sites = parsed.get("sites", [])
        lives = parsed.get("lives", [])
        if isinstance(sites, list):
            for site in sites:
                item, error = sanitize_site(site)
                if item:
                    result.sites.append(item)
                elif error == "invalid_placeholder":
                    result.detail = "存在需要密钥占位符的站点，已跳过"
                elif error == "sensitive_credentials":
                    result.detail = "存在含真实凭据的站点，已跳过"
        if isinstance(lives, list):
            for live in lives:
                item, error = sanitize_live(live)
                if item:
                    item.setdefault(LIVE_SOURCE_KEY, source)
                    result.lives.append(item)
                elif error == "invalid_placeholder":
                    result.detail = "存在需要密钥占位符的直播项，已跳过"
                elif error == "sensitive_credentials":
                    result.detail = "存在含真实凭据的直播项，已跳过"
        for ref in extract_python_refs(parsed):
            try:
                py_data, _, _, py_url = fetch(ref)
                if valid_python(py_data):
                    name = safe_python_name(py_url, py_data, {**local_python, **result.python_files})
                    result.python_files[name] = py_data
            except Exception as exc:
                log("WARN Python 子链接失败 %s: %s" % (ref, exc))
        return result

    if is_m3u(text):
        result.kind = "m3u"
        result.channels = parse_m3u_channels(text, source, source_line)
        result.status = "reachable" if result.channels else "manifest_ok"
        return result

    if is_txt(text):
        result.kind = "txt"
        result.channels = parse_txt_channels(text, source, source_line)
        result.status = "reachable" if result.channels else "invalid_config"
        return result

    result.kind = "unknown"
    result.status = "invalid_config"
    result.detail = "无法识别为 TVBox JSON、TXT 或 M3U（前 200 字节: %r）" % text[:200]
    return result


def merge_dicts(items: Iterable[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in items:
        key = "\x1f".join(str(item.get(field, "")) for field in key_fields)
        if key not in merged:
            merged[key] = dict(item)
        else:
            for field, value in item.items():
                if field not in merged[key] or merged[key][field] in (None, "", [], {}):
                    merged[key][field] = value
    return list(merged.values())


QUALITY_SUFFIX = re.compile(
    r"(?i)(?:超高清|超清|高清|标清|蓝光|原画|"
    r"4k|8k|uhd|fhd|hd|1080p|720p|"
    r"60帧|50帧|25帧|备用|备用线路?|线路\d*|"
    r"[\s\-_·]*(?:hd|4k|8k|uhd|fhd))$"
)
CCTV_EDITION_PATTERN = re.compile(r"(?i)(?:港澳版|香港版|台湾版|台灣版)")
CCTV_OVERSEAS_PATTERN = re.compile(
    r"(?i)(?:America|USA?|UK|Europe|欧洲|歐洲|美洲|北美|海外|亚洲|亞洲|"
    r"日本|韩国|韓國|新加坡|马来西亚|馬來西亞|泰国|泰國|越南|印度|"
    r"俄罗斯|俄羅斯|法国|法國|德国|德國|意大利|西班牙|土耳其|阿拉伯|迪拜)"
)
LOCAL_CHANNEL_PATTERN = re.compile(
    r"^(?:(?:北京|上海|天津|重庆|重慶|河北|山西|辽宁|遼寧|吉林|黑龙江|黑龍江|"
    r"江苏|江蘇|浙江|安徽|福建|江西|山东|山東|河南|湖北|湖南|广东|廣東|"
    r"广西|廣西|海南|四川|贵州|貴州|云南|雲南|西藏|陕西|陝西|甘肃|甘肅|"
    r"青海|宁夏|寧夏|新疆|内蒙古|內蒙古|深圳|广州|廣州|南京|苏州|蘇州|"
    r"无锡|無錫|杭州|宁波|寧波|福州|厦门|廈門|济南|濟南|青岛|青島|武汉|武漢|"
    r"长沙|長沙|成都|西安|沈阳|瀋陽|大连|大連|哈尔滨|哈爾濱|长春|長春|"
    r"郑州|鄭州|太原|合肥|南昌|昆明|南宁|南寧|贵阳|貴陽|兰州|蘭州|"
    r"乌鲁木齐|烏魯木齊|海口|三亚|三亞|东莞|東莞|佛山|珠海|中山|汕头|汕頭|"
    r"潮州|揭阳|揭陽|汕尾|肇庆|肇慶|清远|清遠|云浮|雲浮|阳江|陽江|湛江|"
    r"韶关|韶關|河源|梅州|惠州|江门|江門|茂名|南海|顺德|順德|番禺|花都|"
    r"南国|南國|垄上|壟上|都市|生活|公共|综合|綜合|新闻|新聞|影视|影視|"
    r"经济|經濟|科教|文旅|民生|财经|財經)"
    r"(?:新闻|新聞|综合|綜合|公共|都市|生活|经济|經濟|科教|文旅|民生|财经|財經|"
    r"城市|珠江|南方|岭南|嶺南|影视|影視|体育|體育|频道|頻道|台|电视|電視|"
    r"国际|國際|公共|购物|購物|少儿|少兒|纪录|紀錄|纪实|紀實)"
    r"(?:[-—]?\d+)?)"
    r"|^(?:珠江|南方|岭南|嶺南|公共|城市|新闻综合|新聞綜合|综合|綜合|都市|生活|"
    r"经济|經濟|科教|文旅|民生|财经|財經)(?:频道|頻道|台|电视|電視)?(?:[-—]?\d+)?$"
)
CATEGORY_RULES = (
    ("央视频道", re.compile(r"(?i)(?:CCTV|CGTN|央视|中央电视|中国之声)")),
    ("港澳台", re.compile(r"(?i)(?:TVB|翡翠|明珠|凤凰|香港|澳门|澳門|台湾|台灣|"
                           r"中天|东森|三立|民视|台视|华视|中视|Viu|RTHK|港台|"
                           r"马港|新加坡|马来西亚|RTM|HOY|无线|無綫|NOW新闻|NOW新聞|"
                           r"澳视|澳視|公视|公視|爱尔达|愛爾達|华剧|華劇|"
                           r"Astro|TVBS|东森|東森|纬来|緯來)")),
    ("卫视频道", re.compile(r"卫视")),
    ("海外频道", re.compile(r"(?i)(?:美国|英国|韩国|日本|泰国|越南|印度|俄罗斯|"
                             r"法国|德国|意大利|西班牙|土耳其|阿拉伯|迪拜|欧洲|北美|"
                             r"加拿大|澳大利亚|UK|USA|KBS|MBC|SBS|NHK|CNN|BBC|FOX|HBO)")),
    ("体育频道", re.compile(r"(?i)(?:体育|體育|足球|篮球|籃球|NBA|英超|西甲|德甲|意甲|法甲|"
                             r"欧冠|歐冠|赛车|賽車|网球|網球|羽毛球|乒乓|SPORT|ESPN|高尔夫|高爾夫|"
                             r"博斯高球|高球)")),
    ("音乐频道", re.compile(r"(?i)(?:音乐|音樂|MV|演唱会|演唱會|歌曲|DJ|电台|電台|广播|廣播|radio)")),
    ("影视少儿", re.compile(r"(?i)(?:电影|電影|影院|影视|影視|剧场|劇場|电视剧|連續劇|"
                             r"美剧|韩剧|日剧|港剧|台剧|动漫|動畫|少儿|兒童|纪录|紀錄|"
                             r"纪实|紀實|综艺|綜藝|娱乐|娛樂|戏曲|戲曲|曲艺|经典|經典|"
                             r"卡通|动画|動畫|大片|爱奇艺|優酷|优酷|腾讯视频|騰訊視頻|"
                             r"映画|映畫|戏剧|戲劇|影剧|影劇|亲子|親子|布袋戏|布袋戲|"
                             r"Cinema|World|采昌|龙祥|龍祥)")),
    ("教育纪实", re.compile(r"(?i)(?:CETV|教育|课堂|課堂)")),
    ("运营商/内网", re.compile(r"(?i)(?:移动|移動|联通|聯通|电信|電信|天翼|咪咕|IPTV|广电|廣電)")),
    ("地方频道", LOCAL_CHANNEL_PATTERN),
)

RAW_GROUP_RULES = (
    ("运营商/内网", re.compile(r"内网|內網|移动|移動|联通|聯通|电信|電信|天翼|广电|廣電")),
    ("港澳台", re.compile(r"港澳|香港|澳门|澳門|台湾|台灣|港台")),
    ("体育频道", re.compile(r"体育|體育|足球|篮球|籃球|网球|網球|羽毛球|乒乓")),
    ("影视少儿", re.compile(r"电影|電影|影视|影視|剧场|劇場|少儿|兒童|动漫|動漫")),
    ("音乐频道", re.compile(r"音乐|音樂|DJ|歌曲|演唱会|演唱會")),
    ("地方频道", re.compile(r"新闻|新聞|地方|城市|都市|生活|经济|經濟|科教")),
    ("综合直播", re.compile(r"综合|綜合|直播")),
)


def normalize_channel_name(name: str) -> str:
    """Strip presentation-only suffixes without collapsing distinct channels."""
    value = name.strip().replace("\u3000", " ")
    # Upstream files mix Simplified and Traditional characters.  A compact
    # fold table makes the existing category rules work for both forms without
    # changing the published channel name.
    value = value.translate(str.maketrans(
        "衛視臺廣東深圳湖南浙江江蘇東方華民公聞資訊綜藝財經劇紀錄戲曲與社會農業奧林匹克"
        "灣鳳凰澳門輪播點視頻頻劉龍愛",
        "卫视台广东深圳湖南浙江江苏东方华民公闻资讯综艺财经剧纪录戏曲与社会农业奥林匹克"
        "湾凤凰澳门轮播点视频频刘龙爱",
    ))
    value = re.sub(r"\s+", " ", value)
    previous = None
    while value and value != previous:
        previous = value
        value = QUALITY_SUFFIX.sub("", value).strip(" -_·")
    return value or name.strip()


def channel_identity(name: str) -> str:
    """Build a stable, readable identity used for multi-line aggregation."""
    value = normalize_channel_name(name).lower()
    value = re.sub(r"[\s\-_·:：/\\|,，.。()（）\[\]【】]+", "", value)
    return value


def normalize_group(group: str, name: str = "") -> str:
    """Replace opaque source labels with a small, stable category set.

    ``source_section`` is intentionally not accepted through this positional
    argument: existing callers pass the upstream group here.  The section-aware
    wrapper below keeps the public helper backward compatible.
    """
    raw = group.strip()
    # Keep the upstream private-channel bucket exactly as published.  These
    # entries are intentionally not folded into the public TV categories.
    if raw.startswith(("私密频道", "JAV點播", "JAV点播")):
        return raw
    normalized_name = normalize_channel_name(name)
    # Source labels such as "咪咕央视2" describe the upstream provider, not
    # the channel itself. Do not let their "央视" substring misclassify a
    # local or documentary channel.
    name_rules = CATEGORY_RULES
    source_rules = tuple(
        (category, pattern) for category, pattern in CATEGORY_RULES
        if category != "央视频道"
    )
    for category, pattern in name_rules:
        if category == "央视频道" and CCTV_EDITION_PATTERN.search(normalized_name):
            continue
        if pattern.search(normalized_name):
            return category
    for category, pattern in source_rules:
        if pattern.search(raw):
            return category
    # DE=3 is a source-side bucket mixing songs and ordinary channels. Only
    # promote clear "artist-title" entries; leave ambiguous names untouched.
    if (
        raw.upper() == "DE=3"
        and re.search(r"[-—]", name)
        and not re.search(r"频道|頻道|卫视|衛視|央视|央視|CCTV|体育|體育|电影|電影|"
                         r"剧场|劇場|新闻|新聞|纪录|紀錄", name, re.I)
        and not re.fullmatch(r"[^-—]{1,12}[-—]\d+", name.strip())
    ):
        return "音乐点播"
    for category, pattern in RAW_GROUP_RULES:
        if pattern.search(raw):
            return category
    return "待归类"


def load_channel_rules() -> dict[str, Any]:
    """Load maintainable classification rules from tvbox/channel_rules.json."""
    defaults: dict[str, Any] = {
        "private_section_prefixes": ["私密频道", "JAV點播", "JAV点播"],
        "section_categories": {},
        "section_prefix_categories": {},
        "raw_group_categories": {},
        "review_raw_groups": [],
        "channel_name_categories": {},
    }
    if not CHANNEL_RULES_PATH.is_file():
        log("WARN 缺少 tvbox/channel_rules.json，分类将使用代码内置规则")
        return defaults
    try:
        data = json.loads(CHANNEL_RULES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取分类规则 tvbox/channel_rules.json: %s" % exc)
    if not isinstance(data, dict):
        raise ValueError("tvbox/channel_rules.json 顶层必须是对象")
    result = dict(defaults)
    for key, expected in (
        ("private_section_prefixes", list),
        ("section_categories", dict),
        ("section_prefix_categories", dict),
        ("raw_group_categories", dict),
        ("review_raw_groups", list),
        ("channel_name_categories", dict),
    ):
        value = data.get(key, defaults[key])
        if not isinstance(value, expected):
            raise ValueError("channel_rules.json 的 %s 类型错误" % key)
        result[key] = value
    return result


CHANNEL_RULES = load_channel_rules()


def _is_private_section(value: str) -> bool:
    return value.strip().startswith(tuple(CHANNEL_RULES["private_section_prefixes"]))


def normalize_group_with_section(group: str, name: str = "", source_section: str = "") -> str:
    """Classify with the TXT section name retained alongside the upstream group."""
    raw = group.strip()
    section = source_section.strip()
    if _is_private_section(section):
        return section
    if _is_private_section(raw):
        return raw

    normalized_name = normalize_channel_name(name)
    lower_name = normalized_name.lower()
    if lower_name in CHANNEL_RULES["channel_name_categories"]:
        return str(CHANNEL_RULES["channel_name_categories"][lower_name])

    # A mainland CCTV channel stays in the CCTV category even when its name
    # also contains "体育" or "音乐".  The edition/overseas exceptions are
    # checked first because CCTV1 港澳版 and CCTV4 America are separate feeds.
    if (
        re.search(r"(?i)(?:CCTV|CGTN|央视|央視)", normalized_name)
        and not CCTV_EDITION_PATTERN.search(normalized_name)
        and not CCTV_OVERSEAS_PATTERN.search(normalized_name)
    ):
        return "央视频道"
    if CCTV_OVERSEAS_PATTERN.search(normalized_name) and re.search(
        r"(?i)(?:CCTV|CGTN|央视|央視)", normalized_name
    ):
        return "海外频道"
    # Phoenix TV is a Hong Kong/Taiwan-family broadcaster.  Its name contains
    # "卫视", so it must be classified before the generic satellite rule.
    if re.search(r"凤凰|鳳凰", normalized_name):
        return "港澳台"
    if re.search(r"卫视|衛視", normalized_name):
        return "卫视频道"
    if LOCAL_CHANNEL_PATTERN.search(normalized_name) or re.match(
        r"^(?:北京|上海|天津|重庆|重慶|河北|山西|辽宁|遼寧|吉林|黑龙江|黑龍江|"
        r"江苏|江蘇|浙江|安徽|福建|江西|山东|山東|河南|湖北|湖南|广东|廣東|"
        r"广西|廣西|海南|四川|贵州|貴州|云南|雲南|西藏|陕西|陝西|甘肃|甘肅|"
        r"青海|宁夏|寧夏|新疆|内蒙古|內蒙古|深圳|广州|廣州|南京|苏州|蘇州|"
        r"杭州|宁波|寧波|福州|厦门|廈門|济南|濟南|青岛|青島|武汉|武漢|"
        r"长沙|長沙|成都|西安|沈阳|瀋陽|大连|大連|哈尔滨|哈爾濱|长春|長春|"
        r"郑州|鄭州|太原|合肥|南昌|昆明|南宁|南寧|贵阳|貴陽|兰州|蘭州)",
        normalized_name,
    ):
        return "地方频道"

    # A few names are fixed movie/personality rotation channels even though
    # their source group looks like a generic satellite bundle.
    if re.search(r"(?i)(?:轮播|放映厅|专场)", normalized_name):
        return "影视轮播"
    # "广播电视总台" is an organization name, not a music channel.  Check
    # local/terrestrial channel suffixes before the broad 音乐/广播 rule.
    if re.search(r"(?:广播电视总台|廣播電視總台).*(?:新闻|新聞|自贸|自貿|社会与法|社會與法|文旅|少儿|少兒|综合|綜合|都市|生活)频道?$",
                 normalized_name):
        return "地方频道"
    if re.search(r"(?i)(?:周杰伦|演唱会|歌曲|音乐|电台|广播)", normalized_name):
        return "音乐频道"
    if re.search(r"(?i)(?:掼蛋|体坛|体育|體育|足球|篮球|籃球|网球|網球|羽毛球|乒乓)", normalized_name):
        return "体育频道"

    # A recognizable channel name is stronger evidence than the provider
    # section it happens to arrive in. This keeps CCTV, satellite, local and
    # sports channels stable when the same channel is present in several
    # source bundles. Opaque names still fall through to section/group rules.
    for category, pattern in CATEGORY_RULES:
        # Hong Kong/Taiwan editions such as "CCTV1港澳版" are genuinely local
        # feeds; do not turn them into mainland CCTV channels just because the
        # name starts with CCTV.
        if category == "央视频道" and CCTV_EDITION_PATTERN.search(normalized_name):
            continue
        if pattern.search(normalized_name):
            return category

    section_category = CHANNEL_RULES["section_categories"].get(section)
    if section_category:
        return str(section_category)
    for prefix, category in CHANNEL_RULES["section_prefix_categories"].items():
        if section.startswith(prefix):
            return str(category)

    if raw in CHANNEL_RULES["raw_group_categories"]:
        category = str(CHANNEL_RULES["raw_group_categories"][raw])
        # A local-channel name inside a Hong Kong/Taiwan bundle must retain
        # its local classification. The existing name rules handle that.
        if not (category == "港澳台" and LOCAL_CHANNEL_PATTERN.search(normalized_name)):
            return category

    # The source group is only a container.  Let the existing name rules
    # decide first, then leave genuinely opaque abbreviations for review.
    if raw in set(CHANNEL_RULES["review_raw_groups"]):
        result = normalize_group(raw, normalized_name)
        return "待人工确认" if result == "待归类" else result
    if raw == "咪咕央视2":
        return "央视频道"
    result = normalize_group(raw, normalized_name)
    return "待人工确认" if result == "待归类" else result


def url_digest(url: str) -> str:
    """Stable opaque key for matching a URL without publishing its credentials."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def channel_quality(name: str) -> str:
    value = name.lower()
    if re.search(r"8k", value):
        return "8K"
    if re.search(r"4k|uhd", value):
        return "4K"
    if re.search(r"超高清|超清", value):
        return "超清"
    if re.search(r"1080p|fhd", value):
        return "1080P"
    if re.search(r"720p", value):
        return "720P"
    if re.search(r"高清|hd", value):
        return "高清"
    if re.search(r"标清", value):
        return "标清"
    return ""


def safe_channel_url(url: str) -> str:
    """Keep a URL matchable for audits without publishing embedded credentials."""
    parts = urllib.parse.urlsplit(url)
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if re.sub(r"[^a-z0-9]", "", key.lower()) in {
            "accountinfo", "accesstoken", "apikey", "auth", "authorization",
            "authtoken", "bearer", "clientsecret", "cookie", "key", "password",
            "passwd", "pwd", "refreshtoken", "secret", "session", "sessionid",
            "sig", "sign", "signature", "token",
        }:
            continue
        safe_query.append((key, value))
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((
        parts.scheme,
        netloc,
        parts.path,
        urllib.parse.urlencode(safe_query, doseq=True),
        "",
    ))


def merge_channels(channels: Iterable[Channel]) -> list[Channel]:
    merged: OrderedDict[str, Channel] = OrderedDict()
    for channel in channels:
        key = channel.url.strip()
        if not key.startswith(("http://", "https://")):
            continue
        if key not in merged:
            channel.protocol = urllib.parse.urlsplit(key).scheme.lower()
            channel.channel_key = channel_identity(channel.name)
            channel.group = normalize_group_with_section(
                channel.group_raw or channel.group,
                channel.name,
                channel.source_section,
            )
            channel.quality = channel_quality(channel.name)
            channel.lines = [key]
            merged[key] = channel
        elif not merged[key].group and channel.group:
            merged[key].group = channel.group
    return list(merged.values())


def write_m3u(channels: list[Channel]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for channel in channels:
        attrs = []
        tvg_id = channel.tvg_id or channel.channel_key
        if tvg_id:
            attrs.append('tvg-id="%s"' % tvg_id.replace('"', "'"))
        if channel.logo:
            attrs.append('tvg-logo="%s"' % channel.logo.replace('"', "'"))
        if channel.group:
            attrs.append('group-title="%s"' % channel.group.replace('"', "'"))
        if channel.quality:
            attrs.append('quality="%s"' % channel.quality.replace('"', "'"))
        suffix = (" " + " ".join(attrs)) if attrs else ""
        lines.append("#EXTINF:-1%s,%s" % (suffix, channel.name))
        lines.append(channel.url)
    MERGED_M3U_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_channel_ledger(channels: list[Channel]) -> None:
    """Write a compact, source-traceable channel index for audits and health checks."""
    groups: OrderedDict[str, list[Channel]] = OrderedDict()
    for channel in channels:
        groups.setdefault(channel.channel_key or channel_identity(channel.name), []).append(channel)
    entries = []
    for key, lines in groups.items():
        first = lines[0]
        line_entries = []
        for line in lines:
            line_entries.append({
                "url": safe_channel_url(line.url),
                "url_hash": url_digest(line.url),
                "source": safe_channel_url(line.source) if line.source else "",
                "source_line": line.source_line,
                "group_raw": line.group_raw,
                "source_section": line.source_section,
                "protocol": line.protocol,
                "quality": line.quality,
            })
        entries.append({
            "channel_key": key,
            "name": normalize_channel_name(first.name),
            "category": first.group,
            "quality": first.quality,
            "line_count": len(lines),
            "sources": sorted({
                safe_channel_url(line.source) for line in lines if line.source
            }),
            "source_lines": sorted({line.source_line for line in lines if line.source_line}),
            "groups_raw": sorted({line.group_raw for line in lines if line.group_raw}),
            "source_sections": sorted({
                line.source_section for line in lines if line.source_section
            }),
            "protocols": sorted({line.protocol for line in lines if line.protocol}),
            "urls": [safe_channel_url(line.url) for line in lines],
            "lines": line_entries,
        })
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "channels": len(channels),
            "channel_keys": len(entries),
            "multi_line_keys": sum(1 for entry in entries if entry["line_count"] > 1),
            "untraced": sum(1 for line in channels if not line.source),
        },
        "channels": entries,
    }
    if find_sensitive_credentials(report):
        raise ValueError("频道台账包含真实凭据，已中止写入")
    write_json(DOCS_DIR / "channel_ledger.json", report)
    write_classification_review(entries)


def write_classification_review(entries: list[dict[str, Any]]) -> None:
    """Publish unresolved categories without dropping or guessing any channel."""
    pending = [entry for entry in entries if entry["category"] in {"待归类", "待人工确认"}]
    by_category: dict[str, int] = {}
    for entry in entries:
        category = str(entry.get("category", ""))
        by_category[category] = by_category.get(category, 0) + 1
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "policy": {
            "unresolved_action": "仅报告，保留在 merged.m3u，不自动删除或猜测归类",
            "rules_file": "tvbox/channel_rules.json",
            "private_groups": "私密频道/JAV 保留来源原分组",
        },
        "summary": {
            "channels": len(entries),
            "classified": len(entries) - len(pending),
            "pending_review": len(pending),
            "categories": by_category,
        },
        "pending": [
            {
                "channel_key": entry["channel_key"],
                "name": entry["name"],
                "category": entry["category"],
                "groups_raw": entry["groups_raw"],
                "source_sections": entry["source_sections"],
                "sources": entry["sources"],
                "line_count": entry["line_count"],
            }
            for entry in pending
        ],
    }
    if find_sensitive_credentials(report):
        raise ValueError("分类审核报告包含真实凭据，已中止写入")
    write_json(DOCS_DIR / "classification_review.json", report)


def load_dead_channel_urls() -> set[str]:
    """Read the channel URLs that failed decisively often enough to stop listing.

    docs/dead_channels.json is written by the health workflow. Only individual
    channel URLs appear there; a live group is never removed, because groups
    are curated by hand and dropping one would take healthy channels with it.
    A missing or unreadable file means nothing is filtered, so aggregation
    never depends on the health report being present.
    """
    if not DEAD_CHANNELS_PATH.is_file():
        return set()
    try:
        data = json.loads(DEAD_CHANNELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("WARN dead_channels.json 无法解析，本轮不跳过任何频道")
        return set()
    urls = data.get("removed_urls", []) if isinstance(data, dict) else []
    if not isinstance(urls, list):
        return set()
    return {str(url) for url in urls if str(url)}


def drop_dead_lives(lives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every live group; nothing here is removed automatically.

    A live group is a hand-maintained category and may carry hundreds of
    channel URLs. Deleting it would discard healthy channels, so the health
    workflow only reports live streaks and the aggregator leaves geit-fish
    untouched. Dead channel URLs are filtered out of merged.m3u instead.
    """
    return lives


def build_config(stats: BuildStats, repo: str, branch: str) -> dict[str, Any]:
    sites = merge_dicts(stats.sites, ("key", "api"))
    lives = merge_dicts(stats.lives, ("name", "url"))
    merged_live_url = MERGED_PROXY_PREFIX + raw_url(repo, branch, "tvbox/lives/merged.m3u")
    lives = [live for live in lives if live.get("url") != merged_live_url]
    lives = drop_dead_lives(lives)
    if stats.channels:
        lives.append({
            "name": "Geit 聚合直播",
            "type": 0,
            "url": merged_live_url,
            "group": "聚合",
            LIVE_SOURCE_KEY: "本仓库合并 tvbox/lives/merged.m3u",
        })
    config: dict[str, Any] = {
        "name": "Geit",
        "spider": SPIDER_JAR_URL,
        "logo": "",
        "wallpaper": "",
        "sites": sites,
        "lives": lives,
        "parses": [],
        "rules": [],
        "doh": [],
        "flags": [],
        "ijk": [],
        "ads": [],
    }
    for key in ("parses", "rules", "doh", "flags", "ijk", "ads"):
        if key in stats.config_meta:
            config[key] = stats.config_meta[key]
    return config


def python_site(name: str, repo: str, branch: str) -> dict[str, Any]:
    return {
        "key": "py-" + Path(name).stem,
        "name": Path(name).stem,
        "type": 3,
        "api": proxied_py_url(raw_url(repo, branch, "py/" + name)),
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 0,
        "changeable": 1,
        "playerType": 2,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def subscription_config(value: dict[str, Any]) -> dict[str, Any]:
    """Build the plain JSON payload written to the subscription files."""
    config = json.loads(json.dumps(value, ensure_ascii=False))
    # The working Fish/Dolphin examples do not require this TVBox-only field.
    config.pop("name", None)
    config.setdefault("logo", "")
    config.setdefault("wallpaper", "")
    for key in ("parses", "rules", "doh", "flags", "ijk", "ads"):
        config.setdefault(key, [])
    return config


def write_subscription_json(path: Path, value: dict[str, Any]) -> None:
    """Write an unencrypted JSON subscription file."""
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(previous, dict):
                existing = previous
        except (OSError, json.JSONDecodeError):
            existing = {}
    config = subscription_config(value)
    # Manual edits to these presentation fields must survive the next CI run.
    for key in ("spider", "logo", "wallpaper"):
        current = str(existing.get(key, "") or "").strip()
        if key == "spider" and current == LEGACY_SPIDER_URL:
            # 不要把已知失效的旧 spider 地址继续保留下去。
            continue
        if current:
            config[key] = existing[key]
    write_json(path, config)


def strip_unresolved_secret_placeholders(value: Any) -> Any:
    """Remove sites whose key or URL still contains an unresolved secret."""
    if not isinstance(value, dict) or not isinstance(value.get("sites"), list):
        return value
    cleaned = json.loads(json.dumps(value, ensure_ascii=False))
    cleaned["sites"] = [
        site for site in cleaned["sites"]
        if "{{SECRET_" not in json.dumps(site, ensure_ascii=False)
    ]
    return cleaned


def write_docs(stats: BuildStats, repo: str, branch: str) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    source_manifest = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": repo,
        "branch": branch,
        "count": len(stats.sources),
        "sources": [
            {
                "source": redact_sensitive_text(item.source),
                "effective_url": redact_sensitive_text(item.effective_url),
                "kind": item.kind,
                "status": item.status,
                "http_status": item.http_status,
                "content_type": item.content_type,
                "bytes": item.bytes,
                "sites": len(item.sites),
                "lives": len(item.lives),
                "channels": len(item.channels),
                "detail": redact_sensitive_text(item.detail),
            }
            for item in stats.sources
        ],
    }
    source_findings = find_sensitive_credentials(source_manifest)
    if source_findings:
        raise ValueError("来源清单包含真实凭据，已中止写入")
    write_json(DOCS_DIR / "sources.json", source_manifest)
    # docs/tvbox.json is the legacy endpoint; keep it byte-identical to geit-fish.
    write_json(DOCS_DIR / "tvbox.json", json.loads(GEIT_PATH.read_text(encoding="utf-8")))


def write_inventory(stats: BuildStats) -> None:
    """Write the per-run source/site/live inventory.

    This deliberately does NOT touch docs/health.json or docs/health.csv:
    those are owned by check_health.py and hold real probe results. Writing a
    shallow inventory over them would silently destroy the health report.
    """
    rows = []
    for item in stats.sources:
        rows.append({
            "kind": "source",
            "name": redact_sensitive_text(item.source),
            "url": redact_sensitive_text(item.effective_url),
            "status": item.status,
            "http_status": item.http_status or "",
            "detail": redact_sensitive_text(item.detail),
        })
    for site in stats.sites:
        api = str(site.get("api", ""))
        rows.append({
            "kind": "site",
            "name": redact_sensitive_text(site.get("name", "")),
            "url": redact_sensitive_text(api),
            "status": "requires_app" if site.get("type") == 3 else "unverified",
            "http_status": "",
            "detail": "需要影视 App 和对应 spider 解析，HTTP 检查不能证明可播放",
        })
    for live in stats.lives:
        rows.append({
            "kind": "live",
            "name": redact_sensitive_text(live.get("name", "")),
            "url": redact_sensitive_text(live.get("url", "")),
            "status": "unverified",
            "http_status": "",
            "detail": "播放能力需由 check_health.py 或影视 App 实测",
        })
    report = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "sources": len(stats.sources),
            "sites": len(stats.sites),
            "lives": len(stats.lives),
            "channels": len(stats.channels),
        },
        "items": rows,
    }
    if find_sensitive_credentials(report):
        raise ValueError("清单报告包含真实凭据，已中止写入")
    write_json(DOCS_DIR / "inventory.json", report)
    with (DOCS_DIR / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["kind", "name", "url", "status", "http_status", "detail"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_index(stats: BuildStats, repo: str, branch: str) -> None:
    geit_fish = raw_url(repo, branch, "geit-fish")
    geitpy_fish = raw_url(repo, branch, "geitpy-fish")
    health = raw_url(repo, branch, "docs/health.csv")
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Geit · TVBox 配置</title>
<style>
:root{--ink:#18211b;--muted:#667269;--paper:#f3efe5;--card:#fffdf7;--accent:#b8472c;--line:#d9d1c1}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#fff9e8 0,transparent 35%),var(--paper);color:var(--ink);font-family:"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
main{max-width:920px;margin:auto;padding:56px 22px 72px}.eyebrow{letter-spacing:.18em;text-transform:uppercase;color:var(--accent);font-weight:700;font-size:12px}
h1{font-family:Georgia,"Songti SC",serif;font-size:clamp(42px,8vw,78px);line-height:.95;margin:12px 0 18px;font-weight:600}
.lead{max-width:680px;color:var(--muted);font-size:17px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px;margin:34px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 12px 30px #514b3b12}
.card h2{font-size:19px;margin:0 0 8px}.card p{color:var(--muted);font-size:14px;min-height:44px}
code{display:block;background:#eee8db;border-radius:10px;padding:10px;word-break:break-all;font-size:12px}
.stats{display:flex;gap:24px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}
.stats b{display:block;color:var(--ink);font-size:26px;font-family:Georgia,serif}.note{margin-top:26px;padding:16px 18px;border-left:3px solid var(--accent);background:#fffaf0;color:#665b4b;font-size:14px}
</style></head><body><main>
<div class="eyebrow">TVBox /影视 App 配置</div>
<h1>Geit</h1>
<p class="lead">由 sources.txt 自动聚合、去重和校验。第三方原始配置不会原样提交，密钥占位符和相对路径会在生成阶段剔除。</p>
<section class="grid">
<article class="card"><h2>geit-fish</h2><p>聚合站点、直播源和 spider.jar 的明文 JSON 订阅。</p><code>__GEIT_FISH__</code></article>
<article class="card"><h2>geitpy-fish</h2><p>在 geit-fish 基础上追加 py/ 目录中的有效 Python 站点。</p><code>__GEITPY_FISH__</code></article>
<article class="card"><h2>健康报告</h2><p>记录 HTTP 可达性、配置解析状态与播放验证状态。</p><code>__HEALTH__</code></article>
</section>
<div class="stats"><span><b>__SITES__</b>站点</span><span><b>__LIVES__</b>直播配置</span><span><b>__CHANNELS__</b>去重频道</span><span><b>__SOURCES__</b>来源</span></div>
<p class="note">GitHub 网页中的 /blob/ 地址是 HTML 页面，不能直接作为订阅；请使用上方 raw 地址。HTTP 200 只表示文件可下载，并不保证每个频道都能播放。</p>
</main></body></html>"""
    replacements = {
        "__GEIT_FISH__": geit_fish,
        "__GEITPY_FISH__": geitpy_fish,
        "__HEALTH__": health,
        "__SITES__": str(len(stats.sites)),
        "__LIVES__": str(len(stats.lives)),
        "__CHANNELS__": str(len(stats.channels)),
        "__SOURCES__": str(len(stats.sources)),
    }
    for marker, value in replacements.items():
        html = html.replace(marker, value)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def validate_outputs(config: dict[str, Any], py_config: dict[str, Any], repo: str, branch: str) -> None:
    for label, value in (("geit-fish", config), ("geitpy-fish", py_config)):
        if not isinstance(value.get("sites"), list) or not isinstance(value.get("lives"), list):
            raise ValueError("%s 缺少 sites/lives" % label)
        if not str(value.get("spider", "")).startswith("https://"):
            raise ValueError("%s 的 spider 不是绝对 HTTPS 地址" % label)
        encoded = json.dumps(value, ensure_ascii=False)
        if SECRET_PLACEHOLDER.search(encoded):
            raise ValueError("%s 仍包含密钥占位符" % label)
        findings = find_sensitive_credentials(value)
        if findings:
            raise ValueError(
                "%s 仍包含真实凭据（%d 处，字段：%s）"
                % (label, len(findings), ", ".join(sorted({item.rsplit(":", 1)[-1] for item in findings})))
            )
        if re.search(r'"(?:api|url)"\s*:\s*"\.\.?/', encoded):
            raise ValueError("%s 仍包含相对 api/url" % label)
        for live in value.get("lives", []):
            url = str(live.get("url", "")) if isinstance(live, dict) else ""
            if re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", url, re.I):
                raise ValueError("%s 包含图片型直播地址: %s" % (label, url))
    api_values = [str(site.get("api", "")) for site in py_config.get("sites", []) if site.get("type") == 3]
    if len(api_values) != len(set(api_values)):
        raise ValueError("geitpy-fish 存在重复站点 API")
    for site in py_config.get("sites", []):
        api = str(site.get("api", ""))
        if ".py" not in api.lower() and ".js" not in api.lower():
            continue
        host = urllib.parse.urlsplit(api).netloc.lower()
        if host not in PROXIED_PY_HOSTS:
            continue
        bare = proxied_py_url(api)
        prefix = raw_url(repo, branch, "py/")
        if bare.startswith(prefix):
            name = urllib.parse.unquote(bare[len(prefix):])
            if not (PY_DIR / name).is_file():
                raise ValueError("geitpy-fish 引用了不存在的 Python 文件: %s" % name)


def validate_subscription_config(value: dict[str, Any], label: str) -> None:
    expected_lists = ("sites", "lives", "parses", "rules", "doh", "flags", "ijk", "ads")
    for key in expected_lists:
        if not isinstance(value.get(key), list):
            raise ValueError("%s 缺少数组字段 %s" % (label, key))
    if not isinstance(value.get("logo"), str):
        raise ValueError("%s 的 logo 不是字符串" % label)
    if not str(value.get("spider", "")).startswith("https://"):
        raise ValueError("%s 的 spider 不是绝对 HTTPS 地址" % label)
    if find_sensitive_credentials(value):
        raise ValueError("%s 仍包含真实凭据" % label)


def main() -> int:
    repo, branch = load_repo()
    log("=== Geit collector: %s@%s ===" % (repo, branch))
    if not SOURCES_FILE.is_file():
        log("FAIL 缺少 sources.txt")
        return 2

    local_python = load_local_python()
    stats = BuildStats()
    source_lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(
        SOURCES_FILE.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source_lines.append((line_number, line))

    for index, (line_number, source) in enumerate(source_lines, 1):
        log("(%d/%d) %s" % (index, len(source_lines), redact_sensitive_text(source)))
        result = collect_one(
            source,
            {**local_python, **stats.python_files},
            source_line=line_number,
        )
        stats.sources.append(result)
        stats.sites.extend(result.sites)
        stats.lives.extend(result.lives)
        stats.channels.extend(result.channels)
        stats.python_files.update(result.python_files)
        for key, value in result.config_meta.items():
            stats.config_meta.setdefault(key, value)
        log("  -> %s, sites=%d, lives=%d, channels=%d"
            % (result.status, len(result.sites), len(result.lives), len(result.channels)))

    stats.python_files = deduplicate_python_data(stats.python_files)
    for name, data in stats.python_files.items():
        target = PY_DIR / name
        if not target.exists():
            target.write_bytes(data)
            local_python[name] = data
    local_python = load_local_python()

    stats.sites = merge_dicts(stats.sites, ("key", "api"))
    stats.lives = merge_dicts(stats.lives, ("name", "url"))
    stats.channels = merge_channels(stats.channels)
    # Drop individual channel URLs the health workflow proved dead. Groups are
    # kept as-is; a group disappearing is always a manual edit.
    dead_urls = load_dead_channel_urls()
    if dead_urls:
        before = len(stats.channels)
        stats.channels = [ch for ch in stats.channels if ch.url.strip() not in dead_urls]
        log("跳过 %d 个失效频道 URL（连续失败达上限），保留 %d 个" % (before - len(stats.channels), len(stats.channels)))
    write_m3u(stats.channels)
    write_channel_ledger(stats.channels)

    config = build_config(stats, repo, branch)
    py_config = json.loads(json.dumps(config, ensure_ascii=False))
    existing_apis = {str(site.get("api", "")) for site in py_config.get("sites", [])}
    for name in sorted(local_python):
        site = python_site(name, repo, branch)
        if site["api"] not in existing_apis:
            py_config["sites"].append(site)
            existing_apis.add(site["api"])

    # Validate in memory before touching any published artifact.
    config = strip_unresolved_secret_placeholders(config)
    py_config = strip_unresolved_secret_placeholders(py_config)
    validate_outputs(config, py_config, repo, branch)
    subscription = subscription_config(config)
    py_subscription = subscription_config(py_config)
    validate_subscription_config(subscription, "geit-fish")
    validate_subscription_config(py_subscription, "geitpy-fish")
    write_subscription_json(GEIT_PATH, config)
    write_subscription_json(GEITPY_PATH, py_config)
    write_docs(stats, repo, branch)
    write_inventory(stats)
    write_index(stats, repo, branch)

    log("生成 geit-fish：%d 个站点，%d 个直播源" % (len(config["sites"]), len(config["lives"])))
    log("生成 geitpy-fish：%d 个站点（含 %d 个 Python）" % (len(py_config["sites"]), len(local_python)))
    log("生成明文订阅文件：geit-fish、geitpy-fish")
    log("合并直播频道：%d 条" % len(stats.channels))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FATAL %s" % exc)
        raise
