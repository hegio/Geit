#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build TVBox configurations from the sources listed in sources.txt.

The collector deliberately writes derived files only:

* geit.json: playable TVBox configuration with sites and live entries
* geitpy.json: geit.json plus every valid Python spider in py/
* geit-fish.txt / geitpy-fish.txt: unencrypted Fish-compatible JSON profiles
* tvbox/lives/merged.m3u: de-duplicated live channels from TXT/M3U sources
* docs/sources.json: a human-readable source inventory
* docs/health.json and docs/health.csv: link health report

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
GEIT_PATH = HERE / "geit.json"
GEITPY_PATH = HERE / "geitpy.json"
GEIT_FISH_PATH = HERE / "geit-fish.txt"
GEITPY_FISH_PATH = HERE / "geitpy-fish.txt"
MERGED_M3U_PATH = LIVE_DIR / "merged.m3u"

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


@dataclass
class Channel:
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""


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


def parse_txt_channels(text: str) -> list[Channel]:
    channels: list[Channel] = []
    group = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#genre#"):
            group = line.split("#genre#", 1)[1].strip(" ,")
            continue
        if line.startswith("#"):
            continue
        if "#genre#" in line:
            name, rest = line.split(",", 1) if "," in line else (line, "")
            group = rest.replace("#genre#", "").strip(" ,") or name.strip()
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if name and re.match(r"^https?://", url, re.I):
            channels.append(Channel(name=name, url=url, group=group))
    return channels


def _m3u_attr(line: str, name: str) -> str:
    match = re.search(r'(?:^|[\s,])%s="([^"]*)"' % re.escape(name), line, re.I)
    return match.group(1).strip() if match else ""


def parse_m3u_channels(text: str) -> list[Channel]:
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
                channels.append(Channel(url=line, **pending))
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
    digest = hashlib.sha256(data).hexdigest()[:10]
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
        groups.setdefault(hashlib.sha256(data).hexdigest(), []).append((name, data))
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
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
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
        groups.setdefault(hashlib.sha256(data).hexdigest(), []).append(path)
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


def collect_one(source: str, local_python: dict[str, bytes]) -> SourceResult:
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
        result.channels = parse_m3u_channels(text)
        result.status = "reachable" if result.channels else "manifest_ok"
        return result

    if is_txt(text):
        result.kind = "txt"
        result.channels = parse_txt_channels(text)
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


def merge_channels(channels: Iterable[Channel]) -> list[Channel]:
    merged: OrderedDict[str, Channel] = OrderedDict()
    for channel in channels:
        key = channel.url.strip()
        if not key.startswith(("http://", "https://")):
            continue
        if key not in merged:
            merged[key] = channel
        elif not merged[key].group and channel.group:
            merged[key].group = channel.group
    return list(merged.values())


def write_m3u(channels: list[Channel]) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for channel in channels:
        attrs = []
        if channel.tvg_id:
            attrs.append('tvg-id="%s"' % channel.tvg_id.replace('"', "'"))
        if channel.logo:
            attrs.append('tvg-logo="%s"' % channel.logo.replace('"', "'"))
        if channel.group:
            attrs.append('group-title="%s"' % channel.group.replace('"', "'"))
        suffix = (" " + " ".join(attrs)) if attrs else ""
        lines.append("#EXTINF:-1%s,%s" % (suffix, channel.name))
        lines.append(channel.url)
    MERGED_M3U_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_config(stats: BuildStats, repo: str, branch: str) -> dict[str, Any]:
    sites = merge_dicts(stats.sites, ("key", "api"))
    lives = merge_dicts(stats.lives, ("name", "url"))
    merged_live_url = raw_url(repo, branch, "tvbox/lives/merged.m3u")
    lives = [live for live in lives if live.get("url") != merged_live_url]
    if stats.channels:
        lives.append({
            "name": "Geit 聚合直播",
            "type": 0,
            "url": merged_live_url,
            "group": "聚合",
        })
    config: dict[str, Any] = {
        "name": "Geit",
        "spider": raw_url(repo, branch, "spider.jar"),
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
        "api": raw_url(repo, branch, "py/" + name),
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 0,
        "changeable": 1,
        "playerType": 2,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fish_compatible_config(value: dict[str, Any]) -> dict[str, Any]:
    """Build a plain JSON profile matching common Fish/Dolphin clients."""
    config = json.loads(json.dumps(value, ensure_ascii=False))
    # The working Fish/Dolphin examples do not require this TVBox-only field.
    config.pop("name", None)
    config.setdefault("logo", "")
    config.setdefault("wallpaper", "")
    for key in ("parses", "rules", "doh", "flags", "ijk", "ads"):
        config.setdefault(key, [])
    return config


def write_fish_json(path: Path, value: dict[str, Any]) -> None:
    """Write an unencrypted JSON file for Fish/Dolphin-compatible clients."""
    write_json(path, fish_compatible_config(value))


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
    # Keep the legacy TVBox endpoint identical to the root config.
    write_json(DOCS_DIR / "tvbox.json", json.loads(GEIT_PATH.read_text(encoding="utf-8")))


def write_health_report(stats: BuildStats) -> None:
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
        raise ValueError("健康报告包含真实凭据，已中止写入")
    write_json(DOCS_DIR / "health.json", report)
    with (DOCS_DIR / "health.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["kind", "name", "url", "status", "http_status", "detail"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_index(stats: BuildStats, repo: str, branch: str) -> None:
    geit = raw_url(repo, branch, "geit.json")
    geitpy = raw_url(repo, branch, "geitpy.json")
    geit_fish = raw_url(repo, branch, "geit-fish.txt")
    geitpy_fish = raw_url(repo, branch, "geitpy-fish.txt")
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
<article class="card"><h2>geit.json</h2><p>聚合站点、直播源和 spider.jar 的完整播放配置。</p><code>__GEIT__</code></article>
<article class="card"><h2>geitpy.json</h2><p>在 geit.json 基础上追加 py/ 目录中的有效 Python 站点。</p><code>__GEITPY__</code></article>
<article class="card"><h2>geit-fish.txt</h2><p>明文 JSON，无前缀、Base64 或加密，补齐鱼壳/海豚常用顶层字段。</p><code>__GEIT_FISH__</code></article>
<article class="card"><h2>geitpy-fish.txt</h2><p>明文 fish JSON，包含 geitpy.json 中的 Python 站点。</p><code>__GEITPY_FISH__</code></article>
<article class="card"><h2>健康报告</h2><p>记录 HTTP 可达性、配置解析状态与播放验证状态。</p><code>__HEALTH__</code></article>
</section>
<div class="stats"><span><b>__SITES__</b>站点</span><span><b>__LIVES__</b>直播配置</span><span><b>__CHANNELS__</b>去重频道</span><span><b>__SOURCES__</b>来源</span></div>
<p class="note">GitHub 网页中的 /blob/ 地址是 HTML 页面，不能直接作为订阅；请使用上方 raw 地址。HTTP 200 只表示文件可下载，并不保证每个频道都能播放。</p>
</main></body></html>"""
    replacements = {
        "__GEIT__": geit,
        "__GEITPY__": geitpy,
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
    for label, value in (("geit.json", config), ("geitpy.json", py_config)):
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
        raise ValueError("geitpy.json 存在重复站点 API")
    for site in py_config.get("sites", []):
        api = str(site.get("api", ""))
        prefix = raw_url(repo, branch, "py/")
        if api.startswith(prefix):
            name = urllib.parse.unquote(api[len(prefix):])
            if not (PY_DIR / name).is_file():
                raise ValueError("geitpy.json 引用了不存在的 Python 文件: %s" % name)


def validate_fish_config(value: dict[str, Any], label: str) -> None:
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
    source_lines: list[str] = []
    for raw in SOURCES_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source_lines.append(line)

    for index, source in enumerate(source_lines, 1):
        log("(%d/%d) %s" % (index, len(source_lines), redact_sensitive_text(source)))
        result = collect_one(source, {**local_python, **stats.python_files})
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
    write_m3u(stats.channels)

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
    fish_config = fish_compatible_config(config)
    py_fish_config = fish_compatible_config(py_config)
    validate_fish_config(fish_config, "geit-fish.txt")
    validate_fish_config(py_fish_config, "geitpy-fish.txt")
    write_json(GEIT_PATH, config)
    write_json(GEITPY_PATH, py_config)
    write_fish_json(GEIT_FISH_PATH, config)
    write_fish_json(GEITPY_FISH_PATH, py_config)
    write_docs(stats, repo, branch)
    write_health_report(stats)
    write_index(stats, repo, branch)

    log("生成 geit.json：%d 个站点，%d 个直播源" % (len(config["sites"]), len(config["lives"])))
    log("生成 geitpy.json：%d 个站点（含 %d 个 Python）" % (len(py_config["sites"]), len(local_python)))
    log("生成明文鱼壳兼容文件：geit-fish.txt、geitpy-fish.txt")
    log("合并直播频道：%d 条" % len(stats.channels))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FATAL %s" % exc)
        raise
