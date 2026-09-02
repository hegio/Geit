#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 配置文件 / 核心代码 极简采集器
====================================
读取 sources.txt 里的地址列表，定时抓取并生成 GitHub Pages 订阅页。

支持两种写法（每行一个，# 开头为注释）：
  1) 单文件地址：直接抓一个文件（GitHub raw / 第三方站点 / 带 gh-proxy 前缀都行）。
       https://lytvs.top/xxx.json
       https://gh-proxy.com/https://raw.githubusercontent.com/OWNER/REPO/main/file   # 自动剥离前缀直连 raw
  2) 整仓库镜像：REPO:OWNER/REPO[@分支]
       自动列出仓库内全部「核心文件」（.py/.js/.json/.txt/.m3u 等，跳过 .jar 等二进制），逐个抓取。
       REPO:FGBLH/HKL

特性（刻意做减法，只保留稳定够用的）：
  * 自动把 gh-proxy / ghproxy 等代理前缀剥离，直连 raw.githubusercontent.com（Actions 内网直达最稳）
  * 自动对地址里的非 ASCII 字符（中文文件名等）做 percent-encode
  * 自动解密「肥猫工具箱」格式的 AES-CBC 加密 TVBox 接口（如 ok海豚18）
  * 解密后会继续抓取配置里引用的 .py / .js / .json 等核心文件
  * 单文件失败自动重试，不影响其他文件
  * 内容未变化则跳过写入，减少无意义提交
  * 每次生成 docs/tvbox.json（汇总清单）+ docs/index.html（可视页）

用法：
  本地：  python fetch_tvbox.py
  云端：  GitHub Actions 每 6 小时自动跑（见 .github/workflows/collect.yml）
"""

import os
import shutil
import re
import sys
import json
import ssl
import time
import urllib.request
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(HERE, "sources.txt")
REPO_FILE = os.path.join(HERE, "repo.txt")
OUTPUT_DIR = os.path.join(HERE, "tvbox")
DOCS_DIR = os.path.join(HERE, "docs")
LOG_DIR = os.path.join(HERE, "logs")

# py.json 及其蜘蛛 .py 副本统一放在 tvbox/ 下：tvbox/py.json + tvbox/py/*.py
PY_DIR = os.path.join(OUTPUT_DIR, "py")
PYJSON_PATH = os.path.join(OUTPUT_DIR, "py.json")

USER_AGENT = "Mozilla/5.0 (compatible; TVBoxCollector/1.0)"
RETRY = 4
TIMEOUT = 30

# 要采集的「核心文件」扩展名白名单（仓库镜像模式用）；不在其中的（如 .jar/.png/.zip）会被跳过
CODE_EXTS = {
    ".py", ".js", ".json", ".txt", ".m3u", ".md", ".yaml", ".yml", ".toml",
    ".ini", ".html", ".css", ".sh", ".conf", ".cfg", ".lua", ".go", ".ts",
    ".tsx", ".jsx", ".vue", ".xml", ".csv", ".lock",
}

# py.json 的静态部分模板（TVBox 配置）：spider / lives / rules / doh / flags / ijk / ads。
# 运行时 json.loads 此字符串，再把 sites 替换为「本次采集到的 .py 文件」后写出 py.json。
# 如需增删规则 / 直播源 / 广告域名，直接改这里即可。
PYJSON_TEMPLATE_JSON = r"""{
  "spider": "./spider.jar",
  "lives": [
    {
      "name": "灵鹿直播",
      "type": 0,
      "url": "https://wget.la/https://github.com/yanghanhanyingshi/iptv/blob/main/live.txt",
      "epg": "https://iptv-sources2.pages.dev/epg/pw-7/{date}/{name}.json",
      "logo": "https://epg.51zmt.top:8000/logo/{name}.png"
    }
  ],
  "rules": [
    {
      "name": "量子广告",
      "hosts": [
        "vip.lz",
        "hd.lz",
        ".cdnlz"
      ],
      "regex": [
        "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:6\\.666667,[\\s\\S]*?#EXT-X-DISCONTINUITY",
        "#EXTINF.*?\\s+.*?1o.*?\\.ts\\s+"
      ]
    },
    {
      "name": "非凡广告",
      "hosts": [
        "vip.ffzy",
        "hd.ffzy"
      ],
      "regex": [
        "20.52",
        "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:7\\.400000,[\\s\\S]*?#EXT-X-DISCONTINUITY",
        "#EXTINF.*?\\s+.*?1170(20|32).*?\\.ts\\s+",
        "#EXTINF.*?\\s+.*?116977.*?\\.ts\\s+"
      ]
    },
    {
      "name": "索尼广告",
      "hosts": [
        "suonizy"
      ],
      "regex": [
        "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:1\\.000000,[\\s\\S]*?#EXT-X-DISCONTINUITY",
        "#EXTINF.*?\\s+.*?p1ayer.*?\\.ts\\s+",
        "#EXTINF.*?\\s+.*?\\/video\\/original.*?\\.ts\\s+"
      ]
    },
    {
      "name": "暴风广告",
      "hosts": [
        "bfzy",
        "bfbfvip"
      ],
      "regex": [
        "#EXTINF.*?\\s+.*?adjump.*?\\.ts\\s+"
      ]
    },
    {
      "name": "星星广告",
      "hosts": [
        "aws.ulivetv.net"
      ],
      "regex": [
        "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:8,[\\s\\S]*?#EXT-X-DISCONTINUITY"
      ]
    },
    {
      "name": "快看广告",
      "hosts": [
        "kuaikan"
      ],
      "regex": [
        "#EXT-X-KEY:METHOD=NONE\\r*\\n*#EXTINF:5,[\\s\\S]*?#EXT-X-DISCONTINUITY",
        "#EXT-X-KEY:METHOD=NONE\\r*\\n*#EXTINF:2\\.4,[\\s\\S]*?#EXT-X-DISCONTINUITY"
      ]
    },
    {
      "name": "磁力广告",
      "hosts": [
        "magnet"
      ],
      "regex": [
        "更多",
        "请访问",
        "example",
        "社 區",
        "x u u",
        "直 播",
        "更 新",
        "社 区",
        "有趣",
        "有 趣",
        "英皇体育",
        "全中文AV在线",
        "澳门皇冠赌场",
        "哥哥快来",
        "美女荷官",
        "裸聊",
        "新片首发",
        "UUE29"
      ]
    },
    {
      "name": "一起看广告",
      "hosts": [
        "yqk88"
      ],
      "regex": [
        "18.4",
        "15.1666",
        "16.5333",
        "#EXT-X-DISCONTINUITY\\r*\\n*[\\s\\S]*?#EXT-X-CUE-IN"
      ]
    },
    {
      "name": "火山嗅探",
      "hosts": [
        "huoshan.com"
      ],
      "regex": [
        "item_id="
      ]
    },
    {
      "name": "抖音嗅探",
      "hosts": [
        "douyin.com"
      ],
      "regex": [
        "is_play_url="
      ]
    },
    {
      "name": "proxy",
      "hosts": [
        "raw.githubusercontent.com",
        "googlevideo.com",
        "cdn.v82u1l.com",
        "cdn.iz8qkg.com",
        "cdn.kin6c1.com",
        "c.biggggg.com",
        "c.olddddd.com",
        "haiwaikan.com",
        "www.histar.tv",
        "youtube.com",
        "uhibo.com",
        ".*boku.*",
        ".*nivod.*",
        "*.t4tv.hz.cz",
        ".*ulivetv.*"
      ]
    },
    {
      "name": "NO",
      "hosts": [
        "m3u.nikanba.live"
      ],
      "regex": [
        "#EXT-X-DISCONTINUITY\\r*\\n*#EXTINF:10.100000,[\\s\\S]*?#EXT-X-DISCONTINUITY"
      ]
    },
    {
      "name": "智能AI已过滤广告🥨欢迎继续收看节目",
      "hosts": [
        "http"
      ],
      "disable": [
        "aliyuncs.com",
        "olemovienews.com",
        "ninjia.online",
        "vdtuzv.com",
        "json.icu",
        "/asp/hls/",
        "huya.com",
        "zsyzcy.cn",
        "/nby/",
        "yjys.me",
        "122.228.8.29:4433/Cache",
        "huohua",
        "cdn.json.icu"
      ],
      "rules": [
        {
          "regexp": "AI"
        }
      ],
      "toLog": 0
    },
    {
      "name": "农民嗅探",
      "hosts": [
        "toutiaovod.com"
      ],
      "regex": [
        "video/tos/cn"
      ]
    },
    {
      "name": "AI智能广告拦截增强",
      "hosts": [
        ".m3u8",
        ".mp4",
        ".ts"
      ],
      "regex": [
        "#EXTINF:.*,.*广告.*",
        "#EXTINF:.*,.*AD.*",
        "#EXTINF:.*,.*ad.*",
        "#EXT-X-DISCONTINUITY\\r*\\n*[\\s\\S]*?#EXT-X-DISCONTINUITY",
        "/ad/",
        "/ads/",
        "/advert/",
        "/banner/",
        "click",
        "jump",
        "promo",
        "guanggao",
        "gg_"
      ]
    },
    {
      "name": "解析广告跳转过滤",
      "hosts": [
        "http"
      ],
      "disable": [
        "popup",
        "openurl",
        "jump",
        "redirect",
        "click",
        "adservice",
        "adsystem",
        "doubleclick",
        "googlesyndication",
        "cnzz"
      ],
      "toLog": 0
    }
  ],
  "doh": [
    {
      "name": "Google",
      "url": "https://dns.google/dns-query",
      "ips": [
        "8.8.4.4",
        "8.8.8.8"
      ]
    },
    {
      "name": "Cloudflare",
      "url": "https://cloudflare-dns.com/dns-query",
      "ips": [
        "1.1.1.1",
        "1.0.0.1",
        "2606:4700:4700::1111",
        "2606:4700:4700::1001"
      ]
    },
    {
      "name": "AdGuard",
      "url": "https://dns.adguard.com/dns-query",
      "ips": [
        "94.140.14.140",
        "94.140.14.141"
      ]
    },
    {
      "name": "DNSWatch",
      "url": "https://resolver2.dns.watch/dns-query",
      "ips": [
        "84.200.69.80",
        "84.200.70.40"
      ]
    },
    {
      "name": "Quad9",
      "url": "https://dns.quad9.net/dns-quer",
      "ips": [
        "9.9.9.9",
        "149.112.112.112"
      ]
    }
  ],
  "flags": [
    "youku",
    "优酷",
    "优 酷",
    "优酷视频",
    "qq",
    "腾讯",
    "腾 讯",
    "腾讯视频",
    "iqiyi",
    "qiyi",
    "奇艺",
    "爱奇艺",
    "爱 奇 艺",
    "m1905",
    "xigua",
    "letv",
    "leshi",
    "乐视",
    "乐 视",
    "sohu",
    "搜狐",
    "搜 狐",
    "搜狐视频",
    "tudou",
    "pptv",
    "mgtv",
    "芒果",
    "imgo",
    "芒果TV",
    "芒 果 T V",
    "bilibili",
    "哔 哩",
    "哔 哩 哔 哩"
  ],
  "ijk": [
    {
      "group": "软解码",
      "options": [
        {
          "category": 4,
          "name": "opensles",
          "value": "0"
        },
        {
          "category": 4,
          "name": "overlay-format",
          "value": "842225234"
        },
        {
          "category": 4,
          "name": "framedrop",
          "value": "1"
        },
        {
          "category": 4,
          "name": "soundtouch",
          "value": "1"
        },
        {
          "category": 4,
          "name": "start-on-prepared",
          "value": "1"
        },
        {
          "category": 1,
          "name": "http-detect-range-support",
          "value": "0"
        },
        {
          "category": 1,
          "name": "fflags",
          "value": "fastseek"
        },
        {
          "category": 2,
          "name": "skip_loop_filter",
          "value": "48"
        },
        {
          "category": 4,
          "name": "reconnect",
          "value": "1"
        },
        {
          "category": 4,
          "name": "max-buffer-size",
          "value": "5242880"
        },
        {
          "category": 4,
          "name": "enable-accurate-seek",
          "value": "0"
        },
        {
          "category": 4,
          "name": "mediacodec",
          "value": "0"
        },
        {
          "category": 4,
          "name": "mediacodec-auto-rotate",
          "value": "0"
        },
        {
          "category": 4,
          "name": "mediacodec-handle-resolution-change",
          "value": "0"
        },
        {
          "category": 4,
          "name": "mediacodec-hevc",
          "value": "0"
        },
        {
          "category": 1,
          "name": "dns_cache_timeout",
          "value": "600000000"
        }
      ]
    },
    {
      "group": "硬解码",
      "options": [
        {
          "category": 4,
          "name": "opensles",
          "value": "0"
        },
        {
          "category": 4,
          "name": "overlay-format",
          "value": "842225234"
        },
        {
          "category": 4,
          "name": "framedrop",
          "value": "1"
        },
        {
          "category": 4,
          "name": "soundtouch",
          "value": "1"
        },
        {
          "category": 4,
          "name": "start-on-prepared",
          "value": "1"
        },
        {
          "category": 1,
          "name": "http-detect-range-support",
          "value": "0"
        },
        {
          "category": 1,
          "name": "fflags",
          "value": "fastseek"
        },
        {
          "category": 2,
          "name": "skip_loop_filter",
          "value": "48"
        },
        {
          "category": 4,
          "name": "reconnect",
          "value": "1"
        },
        {
          "category": 4,
          "name": "max-buffer-size",
          "value": "5242880"
        },
        {
          "category": 4,
          "name": "enable-accurate-seek",
          "value": "0"
        },
        {
          "category": 4,
          "name": "mediacodec",
          "value": "1"
        },
        {
          "category": 4,
          "name": "mediacodec-auto-rotate",
          "value": "1"
        },
        {
          "category": 4,
          "name": "mediacodec-handle-resolution-change",
          "value": "1"
        },
        {
          "category": 4,
          "name": "mediacodec-hevc",
          "value": "1"
        },
        {
          "category": 1,
          "name": "dns_cache_timeout",
          "value": "600000000"
        }
      ]
    },
    {
      "group": "广告优化",
      "options": [
        {
          "category": 4,
          "name": "skip_loop_filter",
          "value": "48"
        },
        {
          "category": 4,
          "name": "framedrop",
          "value": "1"
        },
        {
          "category": 4,
          "name": "reconnect",
          "value": "1"
        }
      ]
    }
  ],
  "ads": [
    "mimg.0c1q0l.cn",
    "www.googletagmanager.com",
    "www.google-analytics.com",
    "mc.usihnbcq.cn",
    "mg.g1mm3d.cn",
    "mscs.svaeuzh.cn",
    "cnzz.hhttm.top",
    "tp.vinuxhome.com",
    "cnzz.mmstat.com",
    "www.baihuillq.com",
    "s23.cnzz.com",
    "z3.cnzz.com",
    "c.cnzz.com",
    "stj.v1vo.top",
    "z12.cnzz.com",
    "img.mosflower.cn",
    "tips.gamevvip.com",
    "ehwe.yhdtns.com",
    "xdn.cqqc3.com",
    "www.jixunkyy.cn",
    "sp.chemacid.cn",
    "hm.baidu.com",
    "s9.cnzz.com",
    "z6.cnzz.com",
    "um.cavuc.com",
    "mav.mavuz.com",
    "wofwk.aoidf3.com",
    "z5.cnzz.com",
    "xc.hubeijieshikj.cn",
    "tj.tianwenhu.com",
    "xg.gars57.cn",
    "k.jinxiuzhilv.com",
    "cdn.bootcss.com",
    "ppl.xunzhuo123.com",
    "xomk.jiangjunmh.top",
    "img.xunzhuo123.com",
    "z1.cnzz.com",
    "s13.cnzz.com",
    "xg.huataisangao.cn",
    "z7.cnzz.com",
    "xg.huataisangao.cn",
    "z2.cnzz.com",
    "s96.cnzz.com",
    "q11.cnzz.com",
    "thy.dacedsfa.cn",
    "xg.whsbpw.cn",
    "s19.cnzz.com",
    "z8.cnzz.com",
    "s4.cnzz.com",
    "f5w.as12df.top",
    "ae01.alicdn.com",
    "www.92424.cn",
    "k.wudejia.com",
    "vivovip.mmszxc.top",
    "qiu.xixiqiu.com",
    "cdnjs.hnfenxun.com",
    "cms.qdwght.com",
    "ad.",
    "ads.",
    "ads1.",
    "ads2.",
    "adserver.",
    "advert.",
    "advertising.",
    "analytics.",
    "tracking.",
    "track.",
    "stat.",
    "stats.",
    "tongji.",
    "hm.baidu.com",
    "cnzz.com",
    "51.la",
    "umeng.com",
    "mob.com",
    "adjust.com",
    "appsflyer.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "googletagmanager.com",
    "google-analytics.com",
    "sentry.io",
    "firebase.io",
    "mixpanel.com",
    "bugly.qq.com",
    "aliyuncs.com/ad",
    "qiniu.com/ad"
  ],
  "sites": []
}"""

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# 运行期状态
_seen_urls = set()
_seen_content = set()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "collect.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def encode_url(u):
    """对 path / query 中的非 ASCII 字符做 percent-encode，scheme://host 保持原样。
    先 unquote 再 quote，对「已编码」和「含原始中文」两种地址都幂等正确（避免双重编码）。
    """
    p = urllib.parse.urlsplit(u)
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/")
    query = urllib.parse.quote(urllib.parse.unquote(p.query), safe="=&")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


def normalize_source(raw):
    """剥离 GitHub 代理前缀（gh-proxy.com/https://raw... -> 真实 raw 地址），并对中文编码。"""
    raw = raw.strip()
    m = re.match(r"^https?://[^/]+/(https?://raw\.githubusercontent\.com/.+)$", raw)
    if m:
        inner = m.group(1)
        log("剥离代理前缀 -> %s" % inner)
        return encode_url(inner)
    return encode_url(raw)


def fetch_bytes(url, headers=None):
    last = None
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx).read()
        except Exception as e:
            last = e
            if i < RETRY - 1:
                time.sleep(2 * (i + 1))
                log("  重试 %d 失败: %s" % (i + 1, e))
    raise last


def derive_name(url, data=None):
    """从地址推导出安全的文件名，保留原始扩展名。无扩展名且内容为 JSON 时补 .json。"""
    p = urllib.parse.urlsplit(url)
    base = urllib.parse.unquote(p.path.rsplit("/", 1)[-1])
    if not base:
        base = "config"
    if "." not in base and data is not None:
        try:
            json.loads(data.decode("utf-8", "replace"))
            base += ".json"
        except Exception:
            pass
    base = base.replace("/", "_").replace("\\", "_")
    return base


def _get_aes():
    """延迟导入 pycryptodome；未安装时返回 None（脚本会跳过解密，不影响其它功能）。"""
    try:
        from Crypto.Cipher import AES  # type: ignore
        return AES
    except Exception:
        return None


def is_encrypted_payload(text):
    """粗略判断是否为「肥猫工具箱」AES-CBC 加密格式：
    全十六进制、以 $#(2423) 开头、中间有 #$ 分隔、尾部 26 个十六进制字符（13 字节 IV）。
    """
    if not text or len(text) < 80:
        return False
    t = text.strip()
    if not re.fullmatch(r"[0-9a-fA-F]+", t):
        return False
    if not t.startswith("2423"):
        return False
    end_marker = "2324"
    idx = t.find(end_marker, 4)
    if idx < 4:
        return False
    prefix_len = idx + 4
    if len(t) < prefix_len + 26:
        return False
    cipher_hex = t[prefix_len:-26]
    if not cipher_hex or len(cipher_hex) % 32 != 0:
        return False
    return True


def decrypt_aes_payload(payload_hex):
    """解密肥猫工具箱 AES-128-CBC 加密负载，成功返回 bytes（JSON），失败返回 None。"""
    AES = _get_aes()
    if AES is None:
        log("解密依赖 pycryptodome 未安装，跳过解密")
        return None
    text = payload_hex.strip()
    end_marker = "2324"
    idx = text.find(end_marker, 4)
    if idx < 4:
        return None
    prefix_len = idx + 4
    prefix = text[:prefix_len]
    suffix = text[-26:]
    cipher_hex = text[prefix_len:-26]
    if len(cipher_hex) % 32 != 0:
        return None
    try:
        key_str = bytes.fromhex(prefix[4:-4]).decode("utf-8", "ignore")
        iv_str = bytes.fromhex(suffix).decode("utf-8", "ignore")
    except Exception:
        return None
    key = key_str.ljust(16, "0")[:16].encode("utf-8")
    iv = iv_str.ljust(16, "0")[:16].encode("utf-8")
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        pt = cipher.decrypt(bytes.fromhex(cipher_hex))
        pad_len = pt[-1]
        if pad_len == 0 or pad_len > 16:
            return None
        if not all(pt[-i - 1] == pad_len for i in range(pad_len)):
            return None
        return pt[:-pad_len]
    except Exception:
        return None


def try_decrypt(data):
    """若 data 是加密 TVBox 接口则解密并返回 JSON bytes；否则返回 None。"""
    try:
        text = data.decode("utf-8", "ignore").strip()
    except Exception:
        return None
    if not is_encrypted_payload(text):
        return None
    pt = decrypt_aes_payload(text)
    if pt is None:
        return None
    try:
        json.loads(pt.decode("utf-8", "replace"))
        return pt
    except Exception:
        return None


def is_plain_json(data):
    try:
        json.loads(data.decode("utf-8", "replace"))
        return True
    except Exception:
        return False


def extract_json_urls(obj):
    """从 dict/list 中提取所有 .py/.js/.json/.txt/.m3u 等链接。"""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and re.search(r"https?://[^\s\"'<>]+\.(py|js|json|txt|m3u|md|html|css|yaml|yml|toml|xml|csv)", v, re.I):
                found.add(v)
            elif isinstance(v, (dict, list)):
                found.update(extract_json_urls(v))
    elif isinstance(obj, list):
        for item in obj:
            found.update(extract_json_urls(item))
    return found


def load_repo():
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo:
        try:
            with open(REPO_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        repo = line
                        break
        except Exception:
            pass
    if not repo:
        repo = "OWNER/tvbox-mirror"
    return repo, branch


def list_repo_files(repo, branch, token=None):
    """用 GitHub API 递归列出仓库内全部 blob；保留核心文件扩展名，无扩展名仅当可解密或为 JSON 才收。"""
    api = "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (repo, branch)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    data = fetch_bytes(api, headers=headers)
    obj = json.loads(data)
    if obj.get("truncated"):
        log("WARN 仓库文件过多被截断，建议缩小范围")
    out = []
    for it in obj.get("tree", []):
        if it.get("type") != "blob":
            continue
        path = it["path"]
        ext = os.path.splitext(path)[1].lower()
        if ext in CODE_EXTS:
            out.append(path)
        elif ext == "":
            # 无扩展名：仅当内容为 TVBox 配置（可解密 / 纯 JSON）才采集
            try:
                raw = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, path)
                bd = fetch_bytes(encode_url(raw))
                if try_decrypt(bd) is not None or is_plain_json(bd):
                    out.append(path)
            except Exception:
                pass
    return out


def _content_sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()[:16]


def collect_file(url, subdir=""):
    """抓取单个 url，保存到 tvbox/[subdir]。
    若内容是 AES 加密 TVBox 配置则自动解密并保存为 .json，同时继续抓取其中引用的核心文件。
    返回 [(相对路径, raw链接, jsdelivr链接), ...]
    """
    url = normalize_source(url)
    if url in _seen_urls:
        log("SKIP 已处理 %s" % url)
        return []
    _seen_urls.add(url)

    data = fetch_bytes(url)

    # 尝试解密
    decrypted = try_decrypt(data)
    if decrypted is not None:
        data = decrypted
        log("已自动解密 %s" % url)

    name = derive_name(url, data)
    rel = os.path.join(subdir, name) if subdir else name
    out = os.path.join(OUTPUT_DIR, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    sha = _content_sha(data)
    if sha in _seen_content:
        log("SKIP 重复内容 %s" % rel)
        return []
    _seen_content.add(sha)

    changed = True
    if os.path.exists(out):
        with open(out, "rb") as f:
            if f.read() == data:
                changed = False
    if changed:
        with open(out, "wb") as f:
            f.write(data)
        log("OK %s (%d 字节)" % (rel, len(data)))
    else:
        log("SKIP 未变化 %s" % rel)

    repo, branch = load_repo()
    jd = "https://cdn.jsdelivr.net/gh/%s@%s/tvbox/%s" % (
        repo, branch, urllib.parse.quote(rel),
    )
    raw = "https://raw.githubusercontent.com/%s/%s/tvbox/%s" % (
        repo, branch, urllib.parse.quote(rel),
    )
    results = [(rel, raw, jd)]

    # 解密后，继续采集配置里引用的核心文件
    if decrypted is not None:
        try:
            cfg = json.loads(decrypted.decode("utf-8", "replace"))
            child_urls = extract_json_urls(cfg)
            log("解密配置中发现 %d 个子链接，继续采集..." % len(child_urls))
            for cu in sorted(child_urls):
                try:
                    results.extend(collect_file(cu, subdir))
                except Exception as e:
                    log("FAIL 子链接 %s -> %s" % (cu, e))
        except Exception:
            pass

    return results


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TVBox 订阅源</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#222}
 h1{font-size:20px} code{background:#f3f3f3;padding:2px 6px;border-radius:4px;word-break:break-all}
 li{margin:8px 0;list-style:none} button{margin-left:8px;cursor:pointer}
 .meta{color:#888;font-size:13px}
</style></head>
<body>
<h1>TVBox 配置 / 核心文件</h1>
<p class="meta">更新时间：{{updated}} ｜ 共 {{count}} 个文件</p>
<ul>
{{items}}
</ul>
<script>
function copy(btn, txt){
  navigator.clipboard.writeText(txt).then(function(){
    var o=btn.textContent; btn.textContent='已复制'; setTimeout(function(){btn.textContent=o;},1500);
  });
}
</script>
</body></html>
"""


def generate_pages(sources):
    os.makedirs(DOCS_DIR, exist_ok=True)
    repo, branch = load_repo()
    updated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    manifest = {
        "updated": updated,
        "count": len(sources),
        "sources": [{"name": n, "raw": raw, "url": jd} for (n, raw, jd) in sources],
    }
    with open(os.path.join(DOCS_DIR, "tvbox.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    items = []
    for n, raw, jd in sources:
        items.append(
            "<li><b>%s</b><br>链接：<code>%s</code>"
            '<button onclick="copy(this,\'%s\')">复制</button></li>'
            % (esc(n), esc(jd), esc(jd))
        )
    html = (
        PAGE_TEMPLATE.replace("{{updated}}", esc(updated))
        .replace("{{count}}", str(len(sources)))
        .replace("{{items}}", "\n".join(items))
    )
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    log("已生成订阅页：%d 个文件" % len(sources))


def _copy_py_to_pydir(rel):
    """把采集到的 .py 复制到 tvbox/py/ 目录（与 tvbox/py.json 同级子目录），
    使 py.json 里的 api "./py/<name>.py" 可被 TVBox 正确加载。
    返回根目录下的文件名；重名时用「子目录限定名」避免覆盖；失败返回 None。"""
    src = os.path.join(OUTPUT_DIR, rel)
    if not os.path.isfile(src):
        return None
    name = os.path.basename(rel)
    os.makedirs(PY_DIR, exist_ok=True)
    dst = os.path.join(PY_DIR, name)
    if os.path.exists(dst):
        try:
            if os.path.getsize(src) == os.path.getsize(dst):
                return name
        except Exception:
            pass
        name = rel.replace("/", "__")
        dst = os.path.join(PY_DIR, name)
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        log("WARN 复制 %s -> py/ 失败: %s" % (rel, e))
        return None
    return name


def _collect_lives(collected):
    """从本次采集到的 TVBox 配置(.json)中提取 lives 直播源，去重后以列表返回。
    以 (name, url) 为唯一键，自动补全 type 等字段，保留所有扩展字段(epg/logo/ua/...)。
    """
    lives = []
    seen = set()
    for rel, _raw, _jd in collected:
        if not rel.lower().endswith(".json"):
            continue
        path = os.path.join(OUTPUT_DIR, rel)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        for lv in cfg.get("lives", []) or []:
            if not isinstance(lv, dict):
                continue
            name = lv.get("name")
            url = lv.get("url")
            if not name or not url:
                continue
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            entry = {"name": name, "type": lv.get("type", 0), "url": url}
            for opt in ("epg", "logo", "ua", "origin", "referer",
                        "pass", "ext", "group", "channels", "header", "playUrl"):
                if opt in lv and lv[opt] not in (None, ""):
                    entry[opt] = lv[opt]
            lives.append(entry)
    return lives


def generate_py_json(collected):
    """在 tvbox/ 下生成 py.json（TVBox 配置）：
      * 输出位置：tvbox/py.json；蜘蛛 .py 副本：tvbox/py/*.py
      * spider / lives / rules / doh / flags / ijk / ads 取自静态模板 PYJSON_TEMPLATE_JSON
      * sites 由本次采集到的 .py 文件自动生成：每个 .py 一个 type=3 站点，api 指向 ./py/<name>.py
      * lives 在模板原有基础上，再合并本次采集到的真实直播源（来自采集的 .json 配置）
      * 同时把 .py 复制到 tvbox/py/，保证 ./py/<name>.py 在 GitHub Pages / jsDelivr 下可直接加载
    """
    py_rels = []
    seen = set()
    for rel, _raw, _jd in collected:
        if not rel.lower().endswith(".py"):
            continue
        b = os.path.basename(rel)
        if b in seen:
            continue
        seen.add(b)
        py_rels.append(rel)

    names = []
    for rel in py_rels:
        n = _copy_py_to_pydir(rel)
        if n:
            names.append(n)

    sites = []
    for n in sorted(names):
        sites.append({
            "key": n,
            "name": n,
            "type": 3,
            "api": "./py/" + n,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 0,
            "changeable": 1,
            "playerType": 2,
        })

    cfg = json.loads(PYJSON_TEMPLATE_JSON)
    cfg["sites"] = sites

    # 合并本次采集到的直播源（保留模板原有 lives，新增的去重后追加在后）
    static_lives = cfg.get("lives", []) or []
    live_keys = {(l.get("name"), l.get("url")) for l in static_lives}
    extra_lives = []
    for lv in _collect_lives(collected):
        k = (lv.get("name"), lv.get("url"))
        if k in live_keys:
            continue
        live_keys.add(k)
        extra_lives.append(lv)
    if extra_lives:
        cfg["lives"] = static_lives + extra_lives
        log("py.json 合并直播源：模板 %d + 采集 %d = %d 条"
            % (len(static_lives), len(extra_lives), len(cfg["lives"])))
    else:
        cfg["lives"] = static_lives

    out_path = PYJSON_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log("已生成 tvbox/py.json：%d 个 spider 站点" % len(sites))


def load_telegram_creds():
    """读取 Telegram 凭据：优先环境变量，其次本地 telegram.env（仅本地测试用，已被 .gitignore 忽略，不会入库）。"""
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if token and chat:
        return token, chat
    try:
        with open(os.path.join(HERE, "telegram.env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TG_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TG_CHAT_ID="):
                    chat = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return token, chat


def notify_telegram(ok, total, failed, repo):
    """采集结束后推送 Telegram 通知。未配置 TG_BOT_TOKEN / TG_CHAT_ID 时自动跳过（不报错）。"""
    token, chat = load_telegram_creds()
    if not token or not chat:
        log("TG 未配置，跳过推送（如需推送请在仓库 Actions Secrets 设置 TG_BOT_TOKEN / TG_CHAT_ID）")
        return
    if "/" in repo:
        owner, name = repo.split("/", 1)
        pages_url = "https://%s.github.io/%s/" % (owner, name)
    else:
        pages_url = ""
    lines = ["🤖 <b>TVBox 自动采集完成</b>",
             "✅ 成功 <b>%d</b> / %d 个文件" % (ok, total)]
    if failed:
        lines.append("⚠️ 失败 %d 个：" % len(failed))
        for f in failed[:15]:
            lines.append("  • " + esc(f))
        if len(failed) > 15:
            lines.append("  … 其余 %d 个见 logs/collect.log" % (len(failed) - 15))
    else:
        lines.append("🎉 全部成功，无失败")
    if pages_url:
        lines.append("🔗 订阅页：%s" % pages_url)
    lines.append("⏰ %s" % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    text = "\n".join(lines)
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, data=payload, headers={"User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx).read()
            rj = json.loads(resp)
            if rj.get("ok"):
                log("TG 推送成功")
            else:
                log("TG 推送返回错误: %s" % rj.get("description"))
            return
        except Exception as e:
            if i < RETRY - 1:
                time.sleep(2 * (i + 1))
                log("TG 推送重试 %d 失败: %s" % (i + 1, e))
    log("TG 推送失败（已忽略，不影响采集）")


def main():
    log("=== TVBox collector start ===")
    global _seen_urls, _seen_content
    _seen_urls = set()
    _seen_content = set()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    repo, branch = load_repo()
    token = os.environ.get("GITHUB_TOKEN")

    sources = []
    with open(SOURCES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sources.append(line)
    log("待处理 %d 条" % len(sources))

    collected = []
    failed = []
    for src in sources:
        if src.upper().startswith("REPO:"):
            spec = src[len("REPO:"):].strip()
            if "@" in spec:
                repo, branch = spec.split("@", 1)
            else:
                repo, branch = spec, "main"
            try:
                paths = list_repo_files(repo, branch, token)
            except Exception as e:
                log("FAIL REPO %s -> %s" % (repo, e))
                failed.append("REPO %s -> %s" % (repo, e))
                continue
            log("REPO %s 发现 %d 个核心文件" % (repo, len(paths)))
            safe = repo.replace("/", "__")
            for p in paths:
                raw_url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, p)
                try:
                    collected.extend(collect_file(encode_url(raw_url), subdir=safe))
                except Exception as e:
                    log("FAIL %s -> %s" % (p, e))
                    failed.append("%s -> %s" % (p, e))
        else:
            try:
                collected.extend(collect_file(src))
            except Exception as e:
                log("FAIL %s -> %s" % (src, e))
                failed.append("%s -> %s" % (src, e))

    generate_pages(collected)
    generate_py_json(collected)
    log("=== 完成：成功 %d / %d 条 ===" % (len(collected), len(sources)))
    notify_telegram(len(collected), len(sources), failed, repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
