#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集 lytvs.top 私有订阅配置（蝴蝶影视专属 Token 接口），并把订阅里引用的
所有文件（spider jar、各站点 api / ext、壁纸、logo 等）一并镜像下载到 data/mirror/。

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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SUB_URL = os.environ.get("SUB_URL")
if not SUB_URL:
    print("ERROR: 缺少环境变量 SUB_URL（应在 GitHub Secrets 中配置完整订阅 URL）", file=sys.stderr)
    sys.exit(1)

DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(exist_ok=True)
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(exist_ok=True)
MIRROR_DIR = DATA_DIR / "mirror"
MIRROR_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Referer": "https://sub.lytvs.top/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 不可下载的本地/占位地址
SKIP_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

# Content-Type -> 扩展名（用于 URL 没有扩展名时兜底）
CT_EXT = {
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-python": ".py",
    "text/x-python": ".py",
    "application/java-archive": ".jar",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/json": ".json",
    "text/plain": ".txt",
}


def _safe_print(text: str, to=sys.stdout) -> None:
    """兼容 Windows/Actions 各种终端编码的中文输出。"""
    try:
        to.write(text + "\n")
    except UnicodeEncodeError:
        try:
            encoded = text.encode(to.encoding or "utf-8", errors="replace").decode(to.encoding or "utf-8", errors="replace")
            to.write(encoded + "\n")
        except Exception:
            to.write(text.encode("utf-8", errors="replace").decode("latin-1", errors="replace") + "\n")
    to.flush()


def normalize_url(url: str) -> str:
    """对 URL 的非 ASCII path 做 percent-encoding，避免中文路径崩 urllib。"""
    try:
        p = urllib.parse.urlparse(url)
        # 先尝试 unquote，再重新 quote，避免双重编码
        path = urllib.parse.unquote(p.path)
        path = urllib.parse.quote(path.encode("utf-8"), safe="/%")
        query = urllib.parse.unquote(p.query)
        query = urllib.parse.quote(query.encode("utf-8"), safe="=&%")
        return urllib.parse.urlunparse((p.scheme, p.netloc, path, p.params, query, p.fragment))
    except Exception:
        return url


def fetch(url: str, timeout: int = 30, retries: int = 2) -> tuple[bytes, str]:
    """带重试的 HTTP GET，返回 (body, content-type)。"""
    url = normalize_url(url)
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_err
    raise last_err


def collect_urls(cfg: dict) -> list[tuple[str, str]]:
    """从配置中提取所有可下载的资产 URL，返回 [(label, url), ...]（去重）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url):
        if not isinstance(url, str):
            return
        u = url.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return
        if "{" in u or "}" in u:   # 模板占位符，不能直接下载
            return
        host = urllib.parse.urlparse(u).hostname or ""
        if host in SKIP_HOSTS:
            return
        if u in seen:
            return
        seen.add(u)
        out.append((label, u))

    # 根级资产
    add("root:spider", cfg.get("spider"))
    add("root:wallpaper", cfg.get("wallpaper"))
    add("root:logo", cfg.get("logo"))
    add("root:danmaku", cfg.get("danmaku"))  # 多为 localhost 模板，会被上面的 host 过滤掉

    # 站点级资产
    for s in cfg.get("sites", []) or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("key", "site"))
        add(f"site:{key}:api", s.get("api"))
        add(f"site:{key}:ext", s.get("ext"))
        add(f"site:{key}:download", s.get("download"))
    return out


def safe_name(label: str, url: str) -> str:
    """根据 label 与 URL 生成安全的本地文件名，避免重名与路径穿越。"""
    path = urllib.parse.urlparse(url).path
    base = path.rsplit("/", 1)[-1] or "file"
    base = base.split("?")[0].split("#")[0]
    base = urllib.parse.unquote(base)
    base = re.sub(r"[^\w.\-]+", "_", base) or "file"
    suffix = pathlib.Path(base).suffix
    if not suffix:
        suffix = ".bin"
    prefix = re.sub(r"[^\w\-]+", "_", label)
    return f"{prefix}__{base}" if base != "file" else f"{prefix}__file{suffix}"


def mirror_assets(cfg: dict) -> tuple[int, int, int]:
    """下载所有引用的文件到 data/mirror/，返回 (成功数, 失败数, 总数)。"""
    assets = collect_urls(cfg)
    manifest: dict = {}
    ok = fail = 0
    for label, url in assets:
        fname = safe_name(label, url)
        dest = MIRROR_DIR / fname
        try:
            data, ct = fetch(url, timeout=60, retries=2)
            dest.write_bytes(data)
            manifest[url] = {
                "file": f"mirror/{fname}",
                "size": len(data),
                "sha": hashlib.sha256(data).hexdigest()[:12],
                "type": ct,
            }
            ok += 1
            _safe_print(f"  mirror OK   {label} -> {fname} ({len(data)}B, {ct})")
        except Exception as e:  # 单个文件失败不中断整体
            fail += 1
            _safe_print(f"  mirror FAIL {label} {url} : {e}", to=sys.stderr)

    (MIRROR_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ok, fail, len(assets)


def main() -> int:
    raw, ctype = fetch(SUB_URL, timeout=30, retries=2)

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

    # 4) 镜像下载订阅内引用的所有文件
    mirror_ok = mirror_fail = mirror_total = 0
    if is_json:
        try:
            cfg = json.loads(raw)
            mirror_ok, mirror_fail, mirror_total = mirror_assets(cfg)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{stamp} mirror total={mirror_total} ok={mirror_ok} fail={mirror_fail}\n")
        except Exception as e:
            _safe_print(f"WARN: 镜像下载阶段异常: {e}", to=sys.stderr)

    # 5) 清理过老的快照，避免仓库无限膨胀（默认保留 60 天）
    keep_days = int(os.environ.get("KEEP_DAYS", "60"))
    olds = sorted(HISTORY_DIR.glob(f"*.{ext}"))
    for old in olds[:-keep_days]:
        old.unlink()

    _safe_print(f"OK size={len(raw)} sha={sha} sites={site_count} "
                f"-> {latest.name} / history/{day}.{ext}")
    if mirror_total:
        _safe_print(f"MIRROR total={mirror_total} ok={mirror_ok} fail={mirror_fail} "
                    f"-> data/mirror/ (manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
