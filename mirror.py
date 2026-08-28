#!/usr/bin/env python3
"""
lytvs 镜像脚本
从接口获取配置 JSON，下载所有引用的静态文件到本地，
重写 JSON 中的 URL 为本地相对路径，生成可直接使用的镜像配置。
仅使用 Python 标准库，零依赖。并发下载加速。
"""

import os
import sys
import json
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://sub.lytvs.top/get"
TOKEN_ENV = "LYTVS_TOKEN"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIRROR_DIR = os.path.join(BASE_DIR, "mirror")
FILES_DIR = os.path.join(MIRROR_DIR, "files")
CONFIG_PATH = os.path.join(MIRROR_DIR, "config.json")

TIMEOUT = 30
MAX_RETRIES = 3
WORKERS = 5          # 并发数
DELAY = 0.15         # 每次请求后的延迟

DL_EXTS = {
    ".py", ".js", ".jar", ".m3u", ".m3u8", ".txt", ".json", ".xml",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".html", ".gz", ".csv",
}
OWN_DOMAINS = {"lytvs.top"}


def is_downloadable(url):
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in ("127.0.0.1", "localhost") or host.startswith(("192.168.", "10.")):
        return False
    if any(host.endswith(d) for d in OWN_DOMAINS):
        return True
    path = parsed.path.lower()
    return any(path.endswith(ext) for ext in DL_EXTS)


def url_to_local(url):
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    path = parsed.path.lstrip("/").replace("://", "/")
    return f"{host}/{path}" if path else f"{host}/index"


def download_one(url):
    """下载单个文件，返回 (url, local_path, status)。"""
    local = url_to_local(url)
    dest = os.path.join(FILES_DIR, local.replace("/", os.sep))

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return url, local, "skip"

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "lytvs-mirror/1.0"})
            data = urlopen(req, timeout=TIMEOUT).read()
            if not data:
                raise ValueError("空响应")
            with open(dest, "wb") as f:
                f.write(data)
            time.sleep(DELAY)
            return url, local, "ok"
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(1 + attempt)
            else:
                return url, local, "fail"
    return url, local, "fail"


def extract_urls(obj, found=None):
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for v in obj.values():
            extract_urls(v, found)
    elif isinstance(obj, list):
        for item in obj:
            extract_urls(item, found)
    elif isinstance(obj, str):
        base = obj.split("{")[0].rstrip("?=&")
        if is_downloadable(base):
            found.add(base)
    return found


def rewrite_urls(obj, url_map):
    if isinstance(obj, dict):
        return {k: rewrite_urls(v, url_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_urls(i, url_map) for i in obj]
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        base = obj.split("{")[0].rstrip("?=&")
        if base in url_map:
            return obj.replace(base, url_map[base])
    return obj


def main():
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"ERROR: 请设置环境变量 {TOKEN_ENV}", file=sys.stderr)
        return 1

    # 1. 获取配置
    print("1. 获取接口配置...")
    req = Request(f"{API_BASE}?token={token}", headers={"User-Agent": "lytvs-mirror/1.0"})
    raw = urlopen(req, timeout=60).read()
    config = json.loads(raw)
    print(f"   成功: {len(config.get('sites', []))} 站点, "
          f"{len(config.get('lives', []))} 直播, "
          f"{len(config.get('parses', []))} 解析")

    # 2. 提取 URL
    print("2. 提取可下载资源 URL...")
    urls = sorted(extract_urls(config))
    print(f"   共 {len(urls)} 个资源待镜像")

    # 3. 并发下载
    print(f"3. 并发下载 ({WORKERS} 线程)...")
    os.makedirs(FILES_DIR, exist_ok=True)
    url_map = {}
    ok = skip = fail = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(download_one, u): u for u in urls}
        done = 0
        for fut in as_completed(futures):
            url, local, status = fut.result()
            done += 1
            if status in ("ok", "skip"):
                url_map[url] = f"files/{local}"
                if status == "ok":
                    ok += 1
                else:
                    skip += 1
            else:
                fail += 1
                print(f"    FAIL: {url[:90]}")
            if done % 50 == 0 or done == len(urls):
                print(f"  进度: {done}/{len(urls)}  (ok={ok} skip={skip} fail={fail})")

    print(f"\n   结果: {ok} 新下载, {skip} 已跳过, {fail} 失败")

    # 4. 重写并保存
    print("4. 重写配置 URL -> 本地路径...")
    mirrored = rewrite_urls(config, url_map)
    os.makedirs(MIRROR_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(mirrored, f, ensure_ascii=False, indent=2)
    rewritten = len(url_map)
    total_urls = len(urls)
    print(f"   镜像配置: {os.path.relpath(CONFIG_PATH)}")
    print(f"   URL 重写: {rewritten}/{total_urls}")

    # 5. 统计
    total_size = 0
    file_count = 0
    for root, _, files in os.walk(FILES_DIR):
        for fn in files:
            total_size += os.path.getsize(os.path.join(root, fn))
            file_count += 1
    print(f"\n镜像完成:")
    print(f"  文件数: {file_count}")
    print(f"  总大小: {total_size / 1024 / 1024:.1f} MB")
    print(f"  目录:   {os.path.relpath(MIRROR_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
