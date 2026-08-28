#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集 lytvs.top 私有订阅配置（蝴蝶影视专属 Token 接口），并把订阅里引用的
所有文件（spider jar、各站点 api / ext、壁纸、logo 等）一并镜像下载到 data/mirror/。

反 403 设计：
- 优先使用 curl_cffi 模拟 Chrome 的真实 TLS/JA3 指纹（urllib 的 TLS 指纹会被
  源站 WAF 识别成脚本而 403）。
- curl_cffi 不可用时回退到 urllib（此时对强风控站点大概率仍 403，仅作兜底）。
- 支持 PROXY_URL 环境变量，当 GitHub Actions 的 IP 段被源站拉黑时走代理。

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

PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # 可选：走代理规避 IP 封禁

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
    "Referer": "https://sub.lytvs.top/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 不可下载的本地/占位地址
SKIP_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

# 是否启用 curl_cffi（模拟 Chrome TLS 指纹，绕过 WAF 对脚本指纹的 403）
try:
    from curl_cffi import requests as cffi_requests
    _CURL_CFFI_ERR = None
    USE_CURL_CFFI = True
except Exception as e:  # 兜底到 urllib
    cffi_requests = None
    _CURL_CFFI_ERR = e
    USE_CURL_CFFI = False

print(f"[engine] 下载引擎: {'curl_cffi (Chrome TLS 指纹模拟)' if USE_CURL_CFFI else f'urllib (curl_cffi 不可用: {_CURL_CFFI_ERR}; 强风控站点可能仍 403)'}"
      + (f" | 代理: {PROXY_URL}" if PROXY_URL else ""))

# curl_cffi 全局 Session：抓订阅接口时自动保存 Cookie，下载同域资源时自动带上
_cffi_session = None


def _get_cffi_session():
    global _cffi_session
    if _cffi_session is None and USE_CURL_CFFI:
        _cffi_session = cffi_requests.Session(impersonate="chrome")
    return _cffi_session


# 全局 Session Cookie（urllib 兜底用，从订阅接口的 Set-Cookie 提取）
_SESSION_COOKIES: str = ""


def _safe_print(text: str, to=sys.stdout) -> None:
    """兼容 Windows/Actions 各种终端编码的中文输出。"""
    try:
        to.write(text + "\n")
    except UnicodeEncodeError:
        try:
            enc = to.encoding or "utf-8"
            to.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
        except Exception:
            to.write(text.encode("utf-8", errors="replace").decode("latin-1", errors="replace") + "\n")
    to.flush()


def normalize_url(url: str) -> str:
    """对 URL 的非 ASCII path 做 percent-encoding，避免中文路径崩 urllib。"""
    try:
        p = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(p.path)
        path = urllib.parse.quote(path.encode("utf-8"), safe="/%")
        query = urllib.parse.unquote(p.query)
        query = urllib.parse.quote(query.encode("utf-8"), safe="=&%")
        return urllib.parse.urlunparse((p.scheme, p.netloc, path, p.params, query, p.fragment))
    except Exception:
        return url


def _is_lytvs(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host == "lytvs.top" or host.endswith(".lytvs.top")


def _fetch_curl_cffi(url: str, timeout: int) -> tuple[bytes, str]:
    session = _get_cffi_session()
    if session is None:
        raise RuntimeError("curl_cffi 不可用")
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    headers = dict(HEADERS)
    # lytvs.top 自己的资源用同域 Referer（订阅接口在 sub.lytvs.top，资源在 lytvs.top）
    if _is_lytvs(url):
        headers["Referer"] = "https://lytvs.top/"
    r = session.get(url, headers=headers, proxies=proxies,
                    timeout=timeout, allow_redirects=True)
    if r.status_code >= 400:
        raise urllib.error.HTTPError(url, r.status_code, "curl_cffi http error", r.headers, None)
    return r.content, r.headers.get("content-type", "")


def _fetch_urllib(url: str, timeout: int) -> tuple[bytes, str]:
    global _SESSION_COOKIES
    proxies = None
    if PROXY_URL:
        proxies = {"http": PROXY_URL, "https": PROXY_URL}
    handler = urllib.request.ProxyHandler(proxies) if proxies else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler)
    headers = dict(HEADERS)
    if _is_lytvs(url):
        headers["Referer"] = "https://lytvs.top/"
    if _SESSION_COOKIES:
        headers["Cookie"] = _SESSION_COOKIES
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        # 抓订阅接口时保存 Set-Cookie，供后续同域资源下载使用
        sc = resp.headers.get_all("Set-Cookie") or []
        if sc and not _SESSION_COOKIES:
            _SESSION_COOKIES = "; ".join(c.split(";", 1)[0].strip() for c in sc if c.strip())
        return resp.read(), resp.headers.get("Content-Type", "")


def fetch(url: str, timeout: int = 30, retries: int = 3) -> tuple[bytes, str]:
    """带重试的 HTTP GET，优先 curl_cffi（Chrome 指纹），失败回退 urllib。返回 (body, content-type)。"""
    url = normalize_url(url)
    last_err = None
    for attempt in range(retries + 1):
        try:
            if USE_CURL_CFFI:
                try:
                    return _fetch_curl_cffi(url, timeout)
                except Exception as e:
                    # 单次 curl_cffi 异常先尝试 urllib 兜底（引擎级可恢复错误）
                    if not isinstance(e, urllib.error.HTTPError):
                        _safe_print(f"  [warn] curl_cffi 失败，回退 urllib: {e}", to=sys.stderr)
                        return _fetch_urllib(url, timeout)
                    raise
            else:
                return _fetch_urllib(url, timeout)
        except urllib.error.HTTPError as e:
            last_err = e
            code = getattr(e, "code", None)
            if code in (403, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)  # 指数退避
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
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
        if "." not in host:   # 过滤明显非法的 hostname（避免 curl Bad hostname）
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
    lytvs_fail = 0   # lytvs.top 自己的资源失败数（用于判断是否 IP 被封）
    for label, url in assets:
        fname = safe_name(label, url)
        dest = MIRROR_DIR / fname
        try:
            data, ct = fetch(url, timeout=60, retries=3)
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
            if _is_lytvs(url):
                lytvs_fail += 1
            _safe_print(f"  mirror FAIL {label} {url} : {e}", to=sys.stderr)

    (MIRROR_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if lytvs_fail > 0:
        _safe_print(
            f"\n[提示] lytvs.top 自身资源失败 {lytvs_fail} 个，但第三方站点下载正常。"
            f"这说明 lytvs.top 对当前出口 IP（GitHub Actions）做了封锁。"
            f"请在仓库 Secrets 配置 PROXY_URL（住宅/移动代理）后重跑，即可绕过 IP 风控。",
            to=sys.stderr,
        )
    return ok, fail, len(assets)


def main() -> int:
    try:
        raw, ctype = fetch(SUB_URL, timeout=30, retries=3)
    except Exception as e:
        _safe_print(f"ERROR: 采集订阅配置失败: {e}", to=sys.stderr)
        return 1

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
