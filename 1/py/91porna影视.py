# -*- coding: utf-8 -*-
# coding=utf-8
"""
91Porna影视 (91porna.com)
适配 TVBox / 影视仓 / OK影视 等空壳影视 APP 的 Python 源

站点模板: 自建模板 (非苹果CMS, 基于 Tailwind CSS)
层级结构: 父分类(导航菜单) -> 子分类(筛选器) -> 视频列表
接口覆盖: 分类 / 子分类(筛选器) / 分页 / 详情 / 播放 / 搜索 / 封面

URL 规则:
  父分类   /comic/index/video?category=<cat>        (91视频系)
           /comic/index/av                           (日本AV)
           /comic/av/relvideo?model=N&type=T&order=O (AV子分类)
  详情页   /comic/index/detail?video_key=<key>
  搜索     /comic/index/search?keyword=<kw>&page=N
  分页     ?page=N
  封面     pic.xmbvxj.cn / expose.eisees.com
  播放     /index/detail_play?img=...&ads=...&u=...&t=...
           → 返回 eval JS, 解码后含 m3u8 直链

播放地址提取流程:
  1. 从详情页 HTML 提取第一个 eval (含 detail_play 的参数)
  2. 解码 eval 得到 img(相对路径)、ads(广告域名)、u(加密token)
  3. 请求 /index/detail_play 接口, 获取第二个 eval
  4. 解码第二个 eval, 从中提取 m3u8 直链

依赖: 仅 Python 标准库 (urllib / re / json), 适配 TVBox py 引擎。

TVBox 配置示例 (源配置 .json):
  {
    "name": "91Porna影视",
    "type": 1,
    "api": "https://example.tvbox/api.php",
    "searchable": 1,
    "filterable": 1,
    "jar": "/path/to/91porna影视.py"
  }
"""

import re
import sys
import json
import time
import random
import urllib.request
import urllib.parse
import ssl

# 静默 SSL
try:
    _ssl_context = ssl.create_default_context()
    _ssl_context.check_hostname = False
    _ssl_context.verify_mode = ssl.CERT_NONE
    try:
        _ssl_context.set_ciphers('DEFAULT:@SECLEVEL=0')
    except Exception:
        pass
except Exception:
    _ssl_context = None

# 兼容 base.spider
try:
    sys.path.append('..')
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider:
        def fetch(self, *a, **k):
            raise NotImplementedError
        def post(self, *a, **k):
            raise NotImplementedError


class Spider(BaseSpider):
    # ============ 基本信息 ============
    name = '91Porna影视'
    host = 'https://91porna.com'

    # ============ 父分类 (主菜单入口) ============
    # 导航栏的每个顶级菜单, tid 用于 categoryContent 构造 URL
    PARENTS = [
        # tid              显示名           URL路径
        ('play',           '91视频',       '/comic/index/video?category=play'),
        ('now_month_hot',  '热门排行',     '/comic/index/video?category=now_month_hot'),
        ('original',       '国产原创',     '/comic/index/video?category=original'),
        ('new_update',     '最新更新',     '/comic/index/video?category=new_update'),
        ('av',             '日本AV',       '/comic/av/cate?type=hot'),
    ]

    # ============ 子分类 (筛选器) ============
    # 91视频下的排序选项 (对应 category 参数)
    CAT_OPTIONS = [
        ('play',           '最新',         'play'),
        ('now_month_hot',  '热门排行',     'now_month_hot'),
        ('original',       '国产原创',     'original'),
        ('new_update',     '最新更新',     'new_update'),
    ]

    # 日本AV下的子分类 (model/type/order 参数)
    AV_SUBCATS = [
        ('all',   '全部',         ''),
        ('1',     '多P群交',      '/comic/av/relvideo?model=1&type=theme&order=week'),
        ('12',    '无码解放',      '/comic/av/relvideo?model=12&type=theme&order=week'),
        ('5',     '中文字幕',      '/comic/av/relvideo?model=5&type=theme&order=week'),
        ('6',     '制服诱惑',      '/comic/av/relvideo?model=6&type=theme&order=week'),
        ('107',   '黑人专区',      '/comic/av/relvideo?model=107&type=tag&order=week'),
        ('7',     'SM调教',       '/comic/av/relvideo?model=7&type=theme&order=week'),
    ]

    VIDEO_EXT = ('.m3u8', '.mp4', '.flv', '.mkv', '.avi', '.ts', '.m3u', '.mpd')

    # ============================ 初始化 ============================
    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self._debug = True
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
                      'image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': self.host + '/',
        }

    # ============================ TVBox 基础接口 ============================
    def getName(self):
        return self.name

    def init(self, extend=''):
        self._log(f'初始化完成, host={self.host}')
        return {}

    def isVideoFormat(self, url):
        if not url:
            return False
        u = str(url).split('?')[0].lower()
        return u.endswith(self.VIDEO_EXT)

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    def _log(self, msg):
        if self._debug:
            print(f'[{self.name}] {msg}', file=sys.stderr)

    # ============================ 请求层 ============================
    def fetch(self, url, headers=None, timeout=25, retries=3):
        """直连抓取 HTML, 返回解码后的 str。"""
        hh = headers or self.headers
        last_err = None
        for i in range(retries):
            try:
                if i > 0:
                    time.sleep(random.uniform(0.5, 1.2))
                req = urllib.request.Request(url, headers=hh)
                ctx = _ssl_context or ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    raw = r.read()
                    return self._decode(raw)
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise RuntimeError('fetch failed: ' + url)

    @staticmethod
    def _decode(raw):
        for enc in ('utf-8', 'utf-8-sig', 'gb18030', 'gbk'):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode('utf-8', 'ignore')

    def _fix_cover(self, url):
        """封面走 localProxy 中转, 修正 CDN 返回的 binary/octet-stream 为 image/jpeg。"""
        if not url or not url.startswith('http'):
            return url
        qs = urllib.parse.urlencode({'do': 'py', 'name': self.getName(), 'url': url})
        return f'http://127.0.0.1:9978/proxy?{qs}'

    def _abs(self, url):
        """补全相对 URL，并把封面域名映射到可用域名。"""
        if not url:
            return ''
        url = str(url).strip()
        # 封面域名映射: expose.eisees.com SSL 失败, 映射到 pic.xmbvxj.cn
        url = url.replace('expose.eisees.com', 'pic.xmbvxj.cn')
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('http'):
            return url
        if url.startswith('/'):
            return self.host + url
        return urllib.parse.urljoin(self.host + '/', url)

    @staticmethod
    def _txt(s):
        if s is None:
            return ''
        s = re.sub(r'<[^>]+>', ' ', str(s))
        s = s.replace('\xa0', ' ').replace('&nbsp;', ' ')
        for a, b in (('&quot;', '"'), ('&#39;', "'"),
                     ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>')):
            s = s.replace(a, b)
        return re.sub(r'\s+', ' ', s).strip()

    # ============================ Dean Edwards Packer 解码 ============================
    @staticmethod
    def _unpack_eval(html):
        """
        解码 Dean Edwards eval(function(p,a,c,k,e,d){...}) 格式。
        返回解码后的 JS 源码字符串, 失败返回 ''。
        """
        # 匹配 eval(function(p,a,c,k,e,d){...}('payload',a,c,'k1|k2|k3',0,{}))
        m = re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('((?:[^'\\]|\\.)*)',\s*(\d+),\s*(\d+),\s*'((?:[^'\\]|\\.)*)'\.split\('\|'\)",
            html, re.S
        )
        if not m:
            return ''
        payload = m.group(1).replace("\\'", "'")
        base = int(m.group(2))
        count = int(m.group(3))
        keys = m.group(4).split('|')

        def to_base(n, b):
            """将数字 n 转为 b 进制字符串。"""
            chars = '0123456789abcdefghijklmnopqrstuvwxyz'
            if b > 36:
                chars += 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            if n == 0:
                return '0'
            r = ''
            while n > 0:
                r = chars[n % b] + r
                n //= b
            return r

        # 从 count-1 到 0, 用 keys 替换 payload 中的 base 进制编码
        result = payload
        for ci in range(count - 1, -1, -1):
            key = keys[ci] if ci < len(keys) else ''
            if not key:
                continue
            token = to_base(ci, base)
            if token:
                result = re.sub(r'\b' + re.escape(token) + r'\b', key, result)

        return result

    # ============================ 父分类 + 子分类筛选器 ============================
    def homeContent(self, filter=True):
        """
        返回:
          class:  父分类列表
          filters: 每个父分类下的子分类筛选器
          list:    首页推荐视频
        """
        classes = [{'type_id': t, 'type_name': n} for t, n, _ in self.PARENTS]
        filters = {}
        for tid, _, _ in self.PARENTS:
            filters[tid] = self._build_filters(tid)
        result = {
            'class': classes,
            'filters': filters if filter else {},
            'parse': 0,
            'jx': 0,
        }
        try:
            html = self.fetch(self.host + '/', timeout=20)
            result['list'] = self._parse_list(html)[:30]
        except Exception as e:
            self._log(f'homeContent 首页拉取异常: {e}')
            result['list'] = []
        return result

    def homeVideoContent(self):
        try:
            html = self.fetch(self.host + '/', timeout=20)
            return {'list': self._parse_list(html)[:30], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log(f'homeVideoContent 异常: {e}')
            return {'list': [], 'parse': 0, 'jx': 0}

    def _build_filters(self, tid):
        """每个父分类下的子分类(筛选器)。"""
        if tid == 'av':
            # 日本AV: 子分类是 model 选择
            cat_values = [{'n': n, 'v': v} for _, n, v in self.AV_SUBCATS]
            return [
                {'key': 'model', 'name': '类型', 'value': cat_values},
            ]
        else:
            # 91视频系: 排序选择
            cat_values = [{'n': n, 'v': v} for _, n, v in self.CAT_OPTIONS]
            return [
                {'key': 'category', 'name': '排序', 'value': cat_values},
            ]

    # ============================ URL 构造 ============================
    def _build_url(self, tid, page, extend):
        """根据父分类 + 子分类筛选 + 分页, 拼出最终 URL。"""
        # 归一化 extend
        if isinstance(extend, dict):
            ext = dict(extend)
        elif isinstance(extend, str):
            if extend.strip():
                try:
                    parsed = json.loads(extend)
                    ext = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    ext = {}
            else:
                ext = {}
        elif isinstance(extend, (list, tuple)):
            ext = {}
            keys = ['category', 'model']
            for i, v in enumerate(extend):
                if 0 <= i < len(keys) and v not in (None, '', 'all'):
                    ext[keys[i]] = str(v)
        else:
            ext = {}

        if tid == 'av':
            # 日本AV: 如果有 model 筛选, 用子分类 URL
            model = ext.get('model', '')
            if model and model != 'all':
                # 找到对应的子分类 URL
                for mid, _, url_path in self.AV_SUBCATS:
                    if mid == model:
                        base_url = url_path
                        break
                else:
                    base_url = '/comic/av/cate?type=hot'
            else:
                base_url = '/comic/av/cate?type=hot'
        else:
            # 91视频系: category 参数
            category = ext.get('category', '') or tid
            base_url = f'/comic/index/video?category={category}'

        # 分页
        try:
            pg = int(page or 1)
        except Exception:
            pg = 1
        if pg > 1:
            sep = '&' if '?' in base_url else '?'
            base_url += f'{sep}page={pg}'

        return self.host + base_url

    # ============================ 列表解析 ============================
    def _parse_list(self, html):
        """
        解析视频卡片, 通用 (首页/分类/搜索 全部共用)。
        结构:
          <li><div class="video-item">
            <a href="/comic/index/detail?video_key=394379">
              <div class="poster ...">
                <img data-src="https://pic.xmbvxj.cn/.../xxx.jpeg" alt="标题"/>
                <div>50:24</div>  (时长)
              </div>
            </a>
            <div>
              <a href="/comic/index/detail?video_key=394379">
                <div class="line-clamp-2...">标题文字</div>
              </a>
            </div>
            <div class="dx-subtitle...">
              <a href="/comic/index/search?keyword=xxx"><strong>标签</strong></a>
              ...
            </div>
          </div></li>
        """
        items, seen = [], set()
        if not html:
            return items

        # 匹配所有 video-item 块
        for m in re.finditer(r'<div\s+class="video-item"[^>]*>(.*?)</div>\s*</li>', html, re.S):
            block = m.group(0)
            self._extract_card(block, items, seen)

        # 兜底: 有些页面 video-item 外层结构略不同
        if not items:
            for m in re.finditer(r'<li[^>]*>(.*?)</li>', html, re.S):
                block = m.group(1)
                if 'video_key=' in block:
                    self._extract_card(block, items, seen)

        return items

    def _extract_card(self, block, items, seen):
        """从单个卡片 HTML 块提取视频信息。"""
        try:
            # video_key (detail 和 avdetail 两种路径)
            mv = re.search(r'href="/comic/index/(?:detail|avdetail)\?video_key=([^"&]+)"', block)
            if not mv:
                return
            vkey = mv.group(1)
            if vkey in seen:
                return
            seen.add(vkey)

            # 标题
            name = ''
            for sp in (r'<div\s+class="line-clamp[^"]*"[^>]*>\s*(.*?)\s*</div>',
                       r'alt="([^"]{4,})"',
                       r'title="([^"]{4,})"'):
                m1 = re.search(sp, block, re.S)
                if m1:
                    cand = self._txt(m1.group(1))
                    if cand and len(cand) >= 2:
                        name = cand
                        break
            if not name:
                name = f'91porna_{vkey}'

            # 封面 (data-src 优先, src 兜底)
            pic = ''
            for sp in (r'data-src="([^"]+)"',
                       r'src="([^"]+)"'):
                m1 = re.search(sp, block)
                if m1 and 'data:image' not in m1.group(1) and 'poster_loading' not in m1.group(1):
                    pic = m1.group(1)
                    break
            if pic and not pic.startswith('http'):
                pic = self._abs(pic)

            # 时长
            dur = ''
            md = re.search(r'bg-black[^>]*>\s*([\d:]+)\s*</div>', block)
            if md:
                dur = md.group(1).strip()

            # 标签 (从搜索链接提取)
            tags = re.findall(r'href="/comic/index/search\?keyword=([^"]+)"[^>]*>\s*<strong[^>]*>([^<]+)</strong>', block)
            tag_str = ' '.join(self._txt(t[1]) for t in tags[:4]) if tags else ''

            remarks_bits = []
            if dur:
                remarks_bits.append(dur)
            if tag_str:
                remarks_bits.append(tag_str)
            remarks = ' · '.join(remarks_bits) if remarks_bits else ''

            items.append({
                'vod_id': vkey,
                'vod_name': name[:200],
                'vod_pic': self._fix_cover(pic),
                'vod_remarks': remarks[:80],
            })
        except Exception as ex:
            self._log(f'卡片解析异常: {ex}')

    @staticmethod
    def _pagecount(html, cur):
        """从 dx-pager 的 data-rec-total 计算总页数。"""
        if not html:
            return 9999
        # data-rec-total="174460" data-rec-per-page="24"
        mt = re.search(r'data-rec-total="(\d+)"', html)
        mp = re.search(r'data-rec-per-page="(\d+)"', html)
        if mt and mp:
            try:
                total = int(mt.group(1))
                per = int(mp.group(1)) or 24
                pc = (total + per - 1) // per
                return max(pc, cur)
            except Exception:
                pass
        # 兜底: 从分页链接提取
        nums = []
        for m in re.finditer(r'[?&]page=(\d+)', html):
            try:
                nums.append(int(m.group(1)))
            except Exception:
                pass
        if nums:
            return max(cur, max(nums))
        return 9999

    # ============================ 分类内容 ============================
    def categoryContent(self, tid, pg, filter=True, extend=''):
        """分类/子分类筛选/分页 入口。"""
        try:
            page = int(pg) if pg else 1
        except Exception:
            page = 1
        try:
            url = self._build_url(tid, page, extend)
            self._log(f'categoryContent tid={tid} pg={page} ext={extend} -> {url}')
            html = self.fetch(url, timeout=30)
            items = self._parse_list(html)
            pc = self._pagecount(html, page)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or 24,
                'total': pc * (len(items) or 24),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log(f'categoryContent 异常: {e}')
            return {'list': [], 'page': page, 'pagecount': 9999,
                    'limit': 24, 'total': 999999, 'parse': 0, 'jx': 0}

    # ============================ 详情 ============================
    def _detail_path(self, vkey):
        """根据 video_key 格式判断详情页路径: 纯数字用 detail, 含字母用 avdetail。"""
        if str(vkey or '').isdigit():
            return '/comic/index/detail'
        return '/comic/index/avdetail'

    def detailContent(self, ids):
        """详情: 返回 vod_play_url 让 playerContent 懒解析 m3u8。"""
        vid = ids[0] if isinstance(ids, (list, tuple)) else ids
        vid = str(vid or '')

        path = self._detail_path(vid)
        url = f'{self.host}{path}?video_key={vid}'
        try:
            html = self.fetch(url, timeout=30)
            return {'list': [self._parse_detail(vid, html, url)], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log(f'detailContent 异常 [{vid}]: {e}')
            return {'list': [self._empty_detail(vid)], 'parse': 0, 'jx': 0}

    def _empty_detail(self, vid):
        return {
            'vod_id': vid,
            'vod_name': f'91porna_{vid}',
            'vod_pic': '',
            'vod_play_from': '91Porna',
            'vod_play_url': f'默认${vid}',
        }


    def _parse_detail(self, vid, html, page_url):
        # --- 标题 ---
        title = ''
        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
        if mt:
            title = self._txt(mt.group(1))
        if not title:
            mt = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
            if mt:
                title = self._txt(mt.group(1))
        if not title:
            mt = re.search(r'<title>([^<]+?)</title>', html)
            if mt:
                title = self._txt(mt.group(1)).split(' - ')[0].split(' | ')[0]
        if not title:
            title = f'91porna_{vid}'

        # --- 封面 ---
        pic = ''
        for sp in (r'<meta\s+property="og:image"\s+content="([^"]+)"',
                   r'data-video_tag_name="[^"]*"\s*data-video_tag_key="[^"]*"\s*>.*?data-src="([^"]+)"'):
            m1 = re.search(sp, html, re.S)
            if m1:
                pic = self._abs(m1.group(1))
                break
        # 兜底: 从 player-container 的 eval 中提取封面 (poster)
        if not pic:
            decoded = self._unpack_eval(html)
            if decoded:
                m1 = re.search(r"poster:\s*'(https?://[^']+)'", decoded)
                if m1:
                    pic = m1.group(1)

        # --- 时长 / 浏览 / 收藏 / 点赞 -> vod_remarks ---
        bits = []
        mt = re.search(r'<time[^>]*datetime="[^"]*"[^>]*>\s*(.*?)\s*</time>', html, re.S)
        if mt:
            bits.append(self._txt(mt.group(1)))
        # 浏览量 (fire.png alt 后面跟数字)
        mv = re.search(r'src="/static/web/images/fire\.png"\s+alt="(\d+)"', html)
        if not mv:
            mv = re.search(r'alt="fire"[^>]*>.*?(\d+)', html, re.S)
        if not mv:
            mv = re.search(r'images/fire\.png"\s*alt="(\d+)"', html)
        if mv:
            bits.append('浏览' + mv.group(1))
        # 收藏数
        mc = re.search(r'class="collect-div"[^>]*>.*?<span>(\d+)</span>', html, re.S)
        if mc:
            bits.append('收藏' + mc.group(1))
        remarks = ' · '.join(bits)

        # --- 标签 / tags ---
        tags = []
        # 从详情页标签区域提取
        for tm in re.finditer(r'href="/comic/index/search\?keyword=([^"]+)"[^>]*>\s*([^<]{2,20})\s*</a>', html):
            t = self._txt(tm.group(2))
            if t and t not in tags and t != '更多':
                tags.append(t)
        type_name = ' '.join(tags[:10])

        # --- 上传者 ---
        actor = ''
        mu = re.search(r'href="/comic/index/publicvideo\?user_id=\d+"[^>]*title="([^"]*)"', html)
        if not mu:
            mu = re.search(r'href="/comic/index/publicvideo\?user_id=\d+"[^>]*>.*?<div[^>]*>\s*([^<]{2,40})\s*</div>', html, re.S)
        if mu:
            actor = self._txt(mu.group(1))

        # --- 简介 ---
        desc = ''
        md = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
        if md:
            desc = self._txt(md.group(1))
        if not desc:
            md = re.search(r'class="video-desc[^"]*"[^>]*>(.*?)</div>', html, re.S)
            if md:
                desc = self._txt(md.group(1))
        if not desc and actor:
            desc = f'上传者: {actor}'

        # --- 发布时间 -> vod_year ---
        year = ''
        mt = re.search(r'<time\s+datetime="(\d{4}-\d{2}-\d{2})', html)
        if mt:
            year = mt.group(1)

        # --- 播放源: 透传 video_key, 由 playerContent 懒解析 ---
        return {
            'vod_id': vid,
            'vod_name': title[:200],
            'vod_pic': self._fix_cover(pic),
            'type_name': type_name[:80],
            'vod_year': year,
            'vod_area': '',
            'vod_actor': actor[:100],
            'vod_director': '',
            'vod_remarks': remarks[:80],
            'vod_content': desc[:2000],
            'vod_play_from': '91Porna',
            'vod_play_url': f'默认${vid}',
        }

    # ============================ 播放 ============================
    def playerContent(self, flag, pid, vipFlags=None):
        """
        懒解析:
          1) 已是 m3u8/mp4 直链 → 直接返回
          2) 否则把 pid 当 video_key, 拉详情页, 解码 eval 获取 detail_play 参数,
             请求 detail_play 接口, 从返回的 eval JS 中提取 m3u8 直链。

        修正 m3u8 URL: 去掉 poster=ad_config, 加 v=3 (CDN 鉴权要求)。
        """
        pid = str(pid or '').strip()
        if '$' in pid:
            pid = pid.split('$')[-1]
        headers = {'User-Agent': self.headers['User-Agent']}

        # 直链
        if pid.startswith('http') and self.isVideoFormat(pid):
            return self._play_result(pid, headers)

        # 视为 video_key
        vkey = re.sub(r'[^A-Za-z0-9]', '', pid)
        if not vkey:
            return self._play_result('', headers, parse=1)
        try:
            path = self._detail_path(vkey)
            url = f'{self.host}{path}?video_key={vkey}'

            html = self.fetch(url, timeout=30)

            # Step 1: 解码详情页中的第一个 eval, 获取 detail_play 参数
            decoded = self._unpack_eval(html)
            if not decoded:
                self._log(f'playerContent 无法解码详情页 eval: {vkey}')
                return self._play_result('', headers, parse=1)

            # 从解码后的 JS 中提取 detail_play 的参数
            img_val = ''
            ads_val = ''
            u_val = ''

            m_img = re.search(r'img=([^&"]+)', decoded)
            if m_img:
                img_val = urllib.parse.unquote(m_img.group(1))

            m_ads = re.search(r'ads=([^&"]+)', decoded)
            if m_ads:
                ads_val = urllib.parse.unquote(m_ads.group(1))

            m_u = re.search(r'encodeURIComponent\("([^"]+)"\)', decoded)
            if m_u:
                u_val = m_u.group(1)
            else:
                m_u2 = re.search(r'[?&]u=([^&"]+)', decoded)
                if m_u2:
                    u_val = m_u2.group(1)

            if not img_val or not u_val:
                self._log(f'playerContent 无法提取 detail_play 参数: img={img_val}, u={u_val}')
                return self._play_result('', headers, parse=1)

            # Step 2: 直连请求 detail_play 接口
            t = int(time.time() / 2100)
            dp_params = urllib.parse.urlencode({
                'img': img_val,
                'ads': ads_val,
                'u': u_val,
                't': t,
            })
            dp_url = f'{self.host}/index/detail_play?{dp_params}'
            self._log(f'playerContent detail_play: {dp_url[:120]}...')

            dp_html = self.fetch(dp_url, timeout=20)

            # Step 3: 解码 detail_play 返回的第二个 eval, 提取 m3u8
            dp_decoded = self._unpack_eval(dp_html)
            if not dp_decoded:
                dp_decoded = dp_html

            # 从解码后的 JS 中提取 m3u8 URL
            m3u8 = ''
            for pat in (r'url:\s*[\'"]([^\'"]+\.m3u8[^\'"]*)[\'"]',
                        r'[\'"]([^\'"]+\.m3u8[^\'"]*)[\'"]',
                        r'(https?://[^\s\'"]+\.m3u8[^\s\'"]*)'):
                m1 = re.search(pat, dp_decoded)
                if m1:
                    m3u8 = m1.group(1)
                    break

            if not m3u8:
                self._log(f'playerContent 无 m3u8: {vkey}')
                return self._play_result('', headers, parse=1)

            # Step 4: 修正 m3u8 URL 参数 (CDN 鉴权要求)
            m3u8 = self._fix_m3u8_url(m3u8)

            return self._play_result(m3u8, headers)
        except Exception as e:
            self._log(f'playerContent 异常: {e}')
            return self._play_result('', headers, parse=1)

    @staticmethod
    def _fix_m3u8_url(url):
        """修正 m3u8 URL: 去掉 poster=ad_config, 加 v=3。
        CDN 鉴权规则要求 URL 含 v=3 且不含 poster 参数, 否则返回 403。
        """
        if not url or '?' not in url:
            return url
        base, query = url.split('?', 1)
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        # 去掉 poster 参数
        params.pop('poster', None)
        # 加 v=3 (如果不存在)
        if 'v' not in params:
            params['v'] = ['3']
        # 重新构造 query (保持参数顺序: auth_key 优先)
        ordered = []
        for k in ['auth_key', 'v', 'time', 'via', 'via_bm']:
            if k in params:
                ordered.append(f'{k}={params[k][0]}')
        # 补上其他参数
        for k, vals in params.items():
            if k not in ['auth_key', 'v', 'time', 'via', 'via_bm']:
                for v in vals:
                    ordered.append(f'{k}={v}')
        return f'{base}?{"&".join(ordered)}'

    def _play_result(self, url, headers, parse=0):
        return {
            'parse': parse,
            'playUrl': '',
            'url': url,
            'header': json.dumps(headers, ensure_ascii=False),
            'jx': 0,
            'contentType': 'application/vnd.apple.mpegurl' if (url and '.m3u8' in url.lower()) else '',
        }

    # ============================ 搜索 ============================
    def searchContent(self, key, quick=False, pg='1'):
        try:
            page = int(pg) if pg else 1
        except Exception:
            page = 1
        try:
            q = urllib.parse.quote(str(key or ''), safe='')
            url = f'{self.host}/comic/index/search?keyword={q}'
            if page > 1:
                url += f'&page={page}'
            html = self.fetch(url, timeout=30)
            items = self._parse_list(html)
            pc = self._pagecount(html, page)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or 24,
                'total': pc * (len(items) or 24),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log(f'searchContent 异常: {e}')
            return {'list': [], 'page': page, 'pagecount': 1,
                    'limit': 24, 'total': 0, 'parse': 0, 'jx': 0}

    def searchContentPage(self, key, quick=False, pg='1'):
        return self.searchContent(key, quick, pg)

    # ============================ 本地代理 (封面回源) ============================
    def localProxy(self, param):
        """
        TVBox 客户端通过 /proxy?do=py&name=xxx&url=xxx 调用此方法回源封面/海报。
        CDN 返回 binary/octet-stream, 需修正为正确的 image MIME 类型。
        返回 [status_code, content_type, body_bytes]。
        """
        try:
            url = self._extract_proxy_url(param)
            if not url or not url.startswith('http'):
                return None
            req = urllib.request.Request(url, headers={
                'User-Agent': self.headers['User-Agent'],
            })
            ctx = _ssl_context or ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                ct = r.headers.get('Content-Type', '')
                body = r.read()
            # 修正 Content-Type: CDN 返回 binary/octet-stream, TVBox 不认
            if not ct or 'octet-stream' in ct or not ct.startswith('image/'):
                ct = self._guess_image_mime(url)
            return [200, ct, body]
        except Exception as e:
            self._log(f'localProxy 失败: {e}')
            return None

    @staticmethod
    def _extract_proxy_url(param):
        """从 localProxy 参数中提取目标 url。"""
        if isinstance(param, dict):
            for k in ('url', 'pic', 'img', 'src', 'image', 'href', 'link', 'path', 'uri', 'u'):
                v = param.get(k)
                if v:
                    if isinstance(v, list) and v:
                        v = v[0]
                    return str(v)
            for vv in param.values():
                if isinstance(vv, str) and vv.startswith('http'):
                    return vv
        elif isinstance(param, str):
            if 'url=' in param:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(param).query)
                if q.get('url'):
                    return q['url'][0]
                m = re.search(r'[?&]url=([^&]+)', param)
                if m:
                    return urllib.parse.unquote(m.group(1))
            if param.startswith('http'):
                return param
        return ''

    @staticmethod
    def _guess_image_mime(url):
        """根据 URL 扩展名推断 image MIME 类型。"""
        low = url.lower().split('?')[0]
        ext_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp',
        }
        for ext, mt in ext_map.items():
            if low.endswith(ext):
                return mt
        return 'image/jpeg'


# ======================================================================
# 本地自测
# ======================================================================
if __name__ == '__main__':
    sp = Spider()
    print('===== 1. getName =====')
    print('源名:', sp.getName())
    print('===== init =====')
    print('初始化:', sp.init())

    print('\n===== 2. 首页 + 父分类 + 子分类筛选器 =====')
    home = sp.homeContent(filter=True)
    print(f"父分类 {len(home['class'])} 个: " + ', '.join(c['type_name'] + '(' + c['type_id'] + ')' for c in home['class']))
    print(f"首页视频 {len(home['list'])} 条")
    for v in home['list'][:5]:
        print(f"   {v['vod_name'][:30]:<30}  id={v['vod_id']:<12}  cover={'OK' if v['vod_pic'].startswith('http') else 'X'}")
    if home['filters']:
        sample_tid = home['class'][0]['type_id']
        f = home['filters'].get(sample_tid, [])
        print(f'筛选器 (以 {sample_tid} 为例):')
        for g in f:
            print(f"   - {g['name']} (key={g['key']})  共 {len(g['value'])} 项")

    print('\n===== 3. 父分类分页 =====')
    r = sp.categoryContent('play', 1, True, {})
    print(f"   play(p1)  {len(r['list'])} 条 / {r['pagecount']} 页")
    if r['list']:
        print('   首条:', r['list'][0]['vod_name'][:40], '|', r['list'][0]['vod_id'])

    print('\n===== 4. 子分类筛选 =====')
    r = sp.categoryContent('play', 1, True, {'category': 'now_month_hot'})
    print(f"   热门排行  {len(r['list'])} 条 / {r['pagecount']} 页")
    r = sp.categoryContent('play', 1, True, {'category': 'original'})
    print(f"   国产原创  {len(r['list'])} 条 / {r['pagecount']} 页")

    print('\n===== 5. 日本AV分类 =====')
    r = sp.categoryContent('av', 1, True, {})
    print(f"   av(p1)  {len(r['list'])} 条 / {r['pagecount']} 页")
    r = sp.categoryContent('av', 1, True, {'model': '5'})
    print(f"   av+中文字幕  {len(r['list'])} 条 / {r['pagecount']} 页")

    print('\n===== 6. 详情 =====')
    vid = home['list'][0]['vod_id'] if home['list'] else '394379'
    det = sp.detailContent([vid])['list'][0]
    print(f"   名称: {det['vod_name'][:60]}")
    print(f"   封面: {det['vod_pic'][:90]}")
    print(f"   标签: {det['type_name'][:80]}")
    print(f"   上传者: {det['vod_actor'][:60]}")
    print(f"   备注: {det['vod_remarks'][:60]}")
    print(f"   简介: {det['vod_content'][:80]}")
    print(f"   线路: {det['vod_play_from']} | URL: {det['vod_play_url'][:50]}")

    print('\n===== 7. 播放器 (懒解析 m3u8) =====')
    play = sp.playerContent('91Porna', det['vod_play_url'])
    print(f"   parse={play['parse']}")
    print(f"   url={play.get('url', '')[:120]}")
    print(f"   contentType={play.get('contentType', '')}")

    print('\n===== 8. 搜索 =====')
    for kw in ['女', '国产', 'massage']:
        sr = sp.searchContent(kw, False, '1')
        print(f"   搜索[{kw}]  {len(sr['list'])} 条 / {sr['pagecount']} 页")

    print('\n===== 9. 分页 (play p2) =====')
    r = sp.categoryContent('play', 2, True, {})
    print(f"   第 2 页: {len(r['list'])} 条, 首条 = {r['list'][0]['vod_name'][:30] if r['list'] else '-'}")

    print('\n===== 10. searchContentPage =====')
    r = sp.searchContentPage('女', False, '2')
    print(f"   搜索[女] 第 2 页: {len(r['list'])} 条")

    print('\n完成。')
