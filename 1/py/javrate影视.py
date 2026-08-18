# -*- coding: utf-8 -*-
"""
javrate影视 (javrate.com)  适配 TVBox / 影视仓 / OK影视 py 源

站点模板: 自建模板 (非苹果CMS)
层级结构: 6个父分类 -> 排序筛选器(5种排序, 仅 /menu/ 分类) -> 视频
接口覆盖: 分类 / 排序筛选器 / 分页 / 详情 / 播放 / 搜索 / 封面

URL 规则:
  父分类    /menu/censored | /menu/uncensored | /menu/chinese | /movie/new | /best | /movie/subtitle
  排序+分页  /menu/{cat}/{sort_id}-2-{page}/   sort_id 1~5
  详情页    /movie/detail/{uuid}.html
  播放      详情页 JSON-LD contentUrl -> m3u8 直链 (videocdn.avking.xyz)
  搜索      /search/{encodeURIComponent(keyword)}
  封面      picture.avking.xyz CDN
"""

import re
import sys
import json
import time
import random
import html
import urllib.parse

import requests

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except ImportError:
    class BaseSpider(object):
        def fetch(self, url, headers=None, timeout=20, verify=False, cookies=None):
            s = requests.Session()
            s.trust_env = False
            return s.get(url, headers=headers, timeout=timeout,
                         verify=False, cookies=cookies)


class Spider(BaseSpider):
    name = 'javrate影视'
    host = 'https://www.javrate.com'

    UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

    # 翻墙代理候选: javrate 主站被墙, 需代理访问
    # 依次探测可用代理; 全部失败则直连(手机开 VPN 全局模式时由系统接管)
    # 也支持在源配置扩展参数里指定: proxy=http://host:port 或 代理=http://host:port
    PROXY_CANDIDATES = [
        'http://127.0.0.1:10808',   # 电脑 V2RayN 默认
        'http://127.0.0.1:7890',    # Clash 默认 (手机/电脑)
        'http://10.0.2.2:10808',    # 安卓模拟器 -> 宿主机
        'http://127.0.0.1:10809',
        'http://127.0.0.1:1080',
    ]
    PROXY = ''

    # ==================================================================
    # 请求层 (自包含 fetch, 支持代理, 兼容 base.spider 与 TVBox)
    # ==================================================================
    def _detect_proxy(self):
        """从候选代理列表中探测第一个可用的 (2秒超时/个, 命中即返回; 全失败返回''=直连)"""
        try:
            for p in self.PROXY_CANDIDATES:
                try:
                    r = requests.get(self.host + '/', headers=self._headers(),
                                     timeout=2, verify=False,
                                     proxies={'http': p, 'https': p})
                    if getattr(r, 'status_code', 0) == 200:
                        self._log('代理探测成功: %s' % p)
                        return p
                except Exception:
                    continue
        except Exception:
            pass
        return ''

    def _proxies(self):
        return {'http': self.PROXY, 'https': self.PROXY} if self.PROXY else None

    def fetch(self, url, headers=None, timeout=20, verify=False, cookies=None):
        s = requests.Session()
        s.trust_env = False
        return s.get(url, headers=headers, timeout=timeout, verify=verify,
                     cookies=cookies, proxies=self._proxies())

    # ==================================================================
    # 一、父分类
    # ==================================================================
    CATEGORIES = [
        ('censored',    '有碼'),
        ('uncensored',  '無碼'),
        ('chinese',     '國產'),
        ('new',         '最新'),
        ('best',        '熱門'),
        ('subtitle',    '中文字幕'),
    ]

    # 分类 URL 路径映射
    CAT_PATHS = {
        'censored':   '/menu/censored',
        'uncensored': '/menu/uncensored',
        'chinese':    '/menu/chinese',
        'new':        '/movie/new',
        'best':       '/best',
        'subtitle':   '/movie/subtitle',
    }

    # 哪些分类支持排序筛选器 (仅 /menu/ 开头的分类)
    SORT_CATS = ('censored', 'uncensored', 'chinese')

    # ==================================================================
    # 二、排序筛选器 (子分类)
    #   sort_id: 1=按新片發行  2=按觀看次數  3=大家都喜歡  4=做多點贚  5=最新更新(默认)
    # ==================================================================
    SORT_FILTERS = [
        {"key": "sort", "name": "排序", "value": [
            {"n": "最新更新", "v": "5"},
            {"n": "按新片發行", "v": "1"},
            {"n": "按觀看次數", "v": "2"},
            {"n": "大家都喜歡", "v": "3"},
            {"n": "做多點贚", "v": "4"},
        ]},
    ]

    FILTERS = {}
    for _tid in SORT_CATS:
        FILTERS[_tid] = SORT_FILTERS

    PER_PAGE = 24
    VIDEO_EXT = ('.m3u8', '.mp4', '.flv', '.mkv', '.avi', '.ts', '.m3u', '.mpd')

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self._debug = True
        self._last_ts = {}

    # ==================================================================
    # 三、TVBox 基础接口
    # ==================================================================
    def getName(self):
        return self.name

    def init(self, extend=''):
        # 1) 优先使用扩展参数指定的代理: proxy=http://host:port 或 代理=http://host:port
        proxy = ''
        if extend:
            try:
                if isinstance(extend, str):
                    for part in re.split(r'[&;\n]', extend):
                        if '=' in part:
                            k, v = part.split('=', 1)
                            if k.strip().lower() in ('proxy', '代理'):
                                proxy = v.strip()
            except Exception:
                pass
        if proxy:
            self.PROXY = proxy if proxy.startswith('http') else 'http://' + proxy
            self._log('使用扩展参数代理: %s' % self.PROXY)
        else:
            # 2) 自动探测可用代理 (本机 / 模拟器宿主)
            self.PROXY = self._detect_proxy()
        self._log('初始化完成: %s | 代理: %s' % (self.host, self.PROXY or '未探测到(将直连)'))
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

    def localProxy(self, param):
        """下载带 token 的 m3u8, 若为 master playlist 则解析到 sub playlist,
        改写所有 TS 相对路径为带 token 的绝对 URL.

        播放链路:
          master m3u8 (带 token) → 包含 720p/video.m3u8 等相对路径
          sub m3u8 (带 token) → 包含 video0.ts 等相对路径
          TS 分片 → 需带 token 才能访问 (否则 403)

        localProxy 统一处理:
          1. 下载 m3u8 (传入的 URL 带 token)
          2. 若为 master playlist (#EXT-X-STREAM-INF), 提取 sub m3u8 路径,
             构造带 token 的绝对 URL, 下载 sub m3u8
          3. 改写 sub m3u8 内所有相对路径 (TS 分片, KEY URI) 为带 token 的绝对 URL
          4. 返回改写后的 sub m3u8 内容 (播放器直接拿 TS 绝对直链, 无需再走 localProxy)
        """
        try:
            url = param if isinstance(param, str) else ''
            if not url or not url.startswith('http'):
                return None
            headers = self._headers(referer=self.host + '/')
            r = self.fetch(url, headers=headers, timeout=20, verify=False)
            if r.status_code != 200:
                self._log('localProxy HTTP %d: %s' % (r.status_code, url[:100]))
                return None
            text = r.text or ''
            if not text:
                return None

            # 提取 token query string、base URL 和 origin
            token_qs = url.split('?', 1)[1] if '?' in url else ''
            base = url.rsplit('/', 1)[0] + '/'
            parsed = urllib.parse.urlparse(url)
            origin = '%s://%s' % (parsed.scheme, parsed.netloc)

            # 若为 master playlist, 解析到 sub playlist
            if '#EXT-X-STREAM-INF' in text:
                sub_url = self._extract_sub_m3u8(text, base, token_qs, origin)
                if sub_url:
                    self._log('localProxy: master -> sub playlist')
                    r2 = self.fetch(sub_url, headers=headers, timeout=20, verify=False)
                    if r2.status_code == 200 and r2.text:
                        text = r2.text
                        # 更新 base、token、origin 为 sub m3u8 的
                        token_qs = sub_url.split('?', 1)[1] if '?' in sub_url else token_qs
                        base = sub_url.rsplit('/', 1)[0] + '/'
                        parsed2 = urllib.parse.urlparse(sub_url)
                        origin = '%s://%s' % (parsed2.scheme, parsed2.netloc)
                    else:
                        self._log('localProxy: sub fetch HTTP %d, fallback to master' % getattr(r2, 'status_code', 0))
                else:
                    self._log('localProxy: STREAM-INF found but no sub URL extracted')

            # 改写所有相对路径为带 token 的绝对 URL
            if token_qs:
                text = self._rewrite_m3u8(text, base, token_qs, origin)

            return [200, 'application/vnd.apple.mpegurl', text.encode('utf-8')]
        except Exception as e:
            self._log('localProxy 异常: %s' % e)
            return None

    @staticmethod
    def _rewrite_m3u8(text, base_url, token_qs, origin=''):
        """改写 m3u8 内的相对路径为带 token 的绝对 URL

        master m3u8:  720p/video.m3u8 -> {base}720p/video.m3u8?{token}
        子 m3u8:      video0.ts -> {base}720p/video0.ts?{token}
        """
        cdn = origin or 'https://videocdn.avking.xyz'
        lines = text.split('\n')
        out = []
        for line in lines:
            stripped = line.strip()
            # 跳过空行和注释行 (但 #EXT-X-KEY 的 URI= 需处理)
            if not stripped or stripped.startswith('#'):
                # #EXT-X-KEY: URI="key" 也需要补 token
                if stripped.startswith('#EXT-X-KEY') and 'URI=' in stripped:
                    uri_m = re.search(r'URI="([^"]+)"', stripped)
                    if uri_m:
                        uri = uri_m.group(1)
                        if not uri.startswith('http'):
                            if uri.startswith('/'):
                                new_uri = cdn + uri + '?' + token_qs
                            else:
                                new_uri = base_url + uri + '?' + token_qs
                            stripped = stripped.replace('URI="%s"' % uri, 'URI="%s"' % new_uri)
                out.append(line)
                continue
            # 非注释行 = 路径行 (子 m3u8 或 TS 分片)
            path = stripped
            if path.startswith('http'):
                out.append(line)
                continue
            # 相对路径 -> 绝对 URL + token
            if path.startswith('/'):
                abs_url = cdn + path
            else:
                abs_url = base_url + path
            if '?' in abs_url:
                abs_url += '&' + token_qs
            else:
                abs_url += '?' + token_qs
            # 保持原始缩进
            indent = line[:len(line) - len(line.lstrip())]
            out.append(indent + abs_url)
        return '\n'.join(out)

    @staticmethod
    def _extract_sub_m3u8(text, base_url, token_qs, origin=''):
        """从 master playlist 提取第一个 sub m3u8 URL (带 token)

        master playlist 格式:
          #EXTM3U
          #EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=...
          720p/video.m3u8
          #EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=...
          1080p/video.m3u8
        """
        cdn = origin or 'https://videocdn.avking.xyz'
        lines = text.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 跳过 I-frame only 流
            if stripped.startswith('#EXT-X-I-FRAME'):
                continue
            if stripped.startswith('#EXT-X-STREAM-INF'):
                # 下一非空非注释行是 sub m3u8 路径
                for j in range(i + 1, min(i + 3, len(lines))):
                    path = lines[j].strip()
                    if not path or path.startswith('#'):
                        continue
                    # 构造绝对 URL + token
                    if path.startswith('http'):
                        abs_url = path
                    elif path.startswith('/'):
                        abs_url = cdn + path
                    else:
                        abs_url = base_url + path
                    if '?' in abs_url:
                        abs_url += '&' + token_qs
                    else:
                        abs_url += '?' + token_qs
                    return abs_url
        return ''

    # ==================================================================
    # 四、请求工具 (节流 / 重试)
    # ==================================================================
    def _log(self, msg):
        if self._debug:
            print('[%s] %s' % (self.name, msg))

    def _headers(self, referer=None):
        return {
            'User-Agent': self.UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
            'Referer': referer or (self.host + '/'),
        }

    def _throttle(self, key, gap=1.0):
        now = time.time()
        last = self._last_ts.get(key)
        if last is not None:
            wait = gap - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_ts[key] = time.time()

    def _fetch(self, url, referer=None, retries=3, timeout=25):
        last = None
        for i in range(retries):
            try:
                if i > 0:
                    time.sleep(random.uniform(0.6, 1.4))
                hh = self._headers(referer)
                last = self.fetch(url, headers=hh, timeout=timeout, verify=False)
                if getattr(last, 'status_code', 0) == 200:
                    if not last.encoding or last.encoding.lower() in ('iso-8859-1', 'latin-1'):
                        last.encoding = 'utf-8'
                    return last
                self._log('HTTP %d: %s' % (getattr(last, 'status_code', 0), url[:120]))
            except Exception as e:
                self._log('请求异常 [%s]: %s (重试 %d/%d)' % (url[:120], e, i + 1, retries))
        return last

    def _fetch_text(self, url, referer=None):
        r = self._fetch(url, referer=referer)
        if r is None:
            return ''
        return r.text or ''

    # ==================================================================
    # 五、通用小工具
    # ==================================================================
    def _fix(self, url):
        """补全相对 URL 为绝对地址"""
        if not url:
            return ''
        url = str(url).strip()
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return self.host + url
        if not url.startswith('http'):
            return urllib.parse.urljoin(self.host + '/', url)
        return url

    @staticmethod
    def _strip(s):
        if not s:
            return ''
        s = re.sub(r'<[^>]+>', '', str(s))
        s = s.replace('\xa0', ' ')
        # 解码全部 HTML 实体 (含 &#xHHHH; / &#DDDD; 数字实体)
        s = html.unescape(s)
        return re.sub(r'\s+', ' ', s).strip()

    @staticmethod
    def _attr(tag, name):
        m = re.search(r'%s="([^"]*)"' % re.escape(name), tag or '')
        return m.group(1) if m else ''

    # ==================================================================
    # 六、URL 构造
    # ==================================================================
    def _ctg_url(self, tid, page=1, extend=None):
        """分类/排序 URL
        /menu/ 分类:  /menu/{cat}/{sort_id}-2-{page}/
        /movie/ 分类: /movie/{path} 或 /movie/{path}/{page}/  (猜测分页)
        /best:        /best 或 /best/{page}/  (猜测分页)
        """
        ext = extend if isinstance(extend, dict) else {}
        sort_id = str(ext.get('sort', '5') or '5')
        p = int(page or 1)

        base_path = self.CAT_PATHS.get(tid, '/menu/' + tid)

        if tid in self.SORT_CATS:
            # /menu/ 分类: 支持排序+分页
            if p <= 1:
                return '%s%s/%s-2-1/' % (self.host, base_path, sort_id)
            return '%s%s/%s-2-%d/' % (self.host, base_path, sort_id, p)
        else:
            # /movie/ 或 /best: 无排序, 用 ?page=N 分页
            if p <= 1:
                return '%s%s' % (self.host, base_path)
            return '%s%s?page=%d' % (self.host, base_path, p)

    # ==================================================================
    # 七、列表解析 (首页/分类/搜索通用)
    # ==================================================================
    def _parse_list(self, html_text):
        """解析卡片列表: a.movie-card-link 含 href/title/data-movie-code, img.mgn-cover src"""
        items, seen = [], set()
        if not html_text:
            return items

        # 方式 A: 精确匹配 movie-card-link
        for m in re.finditer(
            r'<a\s[^>]*class="[^"]*movie-card-link[^"]*"[^>]*>.*?</a>',
            html_text, re.S
        ):
            try:
                tag = m.group(0)
                href = self._attr(tag, 'href')
                if not href:
                    continue
                vm = re.search(r'/movie/detail/([^"/]+)\.html?', href)
                if not vm:
                    continue
                vid = vm.group(1).strip()
                if vid in seen:
                    continue
                seen.add(vid)

                # 标题: title 属性优先
                name = self._attr(tag, 'title')
                if not name:
                    am = re.search(r'<img[^>]*alt="([^"]+)"', tag)
                    name = am.group(1) if am else ''
                if not name:
                    continue

                # 番号
                code = self._attr(tag, 'data-movie-code')

                # 封面: img.mgn-cover src 优先, 兜底任意 img src
                pic = ''
                pm = re.search(r'<img[^>]*class="[^"]*mgn-cover[^"]*"[^>]*src="([^"]+)"', tag)
                if not pm:
                    pm = re.search(r'<img[^>]*src="([^"]+)"', tag)
                if pm:
                    pic = pm.group(1)

                # 年份
                year = ''
                ym = re.search(r'mgn-badge-year[^>]*>\s*([^<]+?)\s*<', tag)
                if ym:
                    year = self._strip(ym.group(1))

                items.append({
                    'vod_id': vid,
                    'vod_name': self._strip(name)[:200],
                    'vod_pic': self._fix(pic),
                    'vod_remarks': code or year,
                })
            except Exception:
                continue

        if items:
            return items

        # 方式 B: 兜底 - 找所有 /movie/detail/{uuid}.html 链接
        for m in re.finditer(
            r'<a\s[^>]*href="/movie/detail/([^"/]+)\.html?"[^>]*>(.*?)</a>',
            html_text, re.S
        ):
            try:
                vid = m.group(1).strip()
                if vid in seen:
                    continue
                seen.add(vid)
                tag = m.group(0)
                inner = m.group(2)

                name = self._attr(tag, 'title')
                if not name:
                    nm = re.search(r'alt="([^"]+)"', inner)
                    name = nm.group(1) if nm else ''
                if not name:
                    name = self._strip(inner)[:80]
                if not name:
                    continue

                pic = ''
                pm = re.search(r'src="([^"]+\.(?:jpg|jpeg|png|webp))"', inner, re.I)
                if pm:
                    pic = pm.group(1)

                items.append({
                    'vod_id': vid,
                    'vod_name': self._strip(name)[:200],
                    'vod_pic': self._fix(pic),
                    'vod_remarks': '',
                })
            except Exception:
                continue

        return items

    # ==================================================================
    # 八、分页解析
    # ==================================================================
    @staticmethod
    def _pagecount(html_text, page):
        page = int(page or 1)
        if not html_text:
            return page

        # 方式 A: data-page-info="第 1 頁 / 共 1328 頁" (含实体形式 &#x5171;共 &#x9801;頁)
        m = re.search(r'data-page-info="[^"]*(?:共|&#x5171;)\s*(\d+)\s*(?:[頁页]|&#x9801;)', html_text)
        if m:
            return max(page, int(m.group(1)))

        # 方式 B: pagination-wrapper 内找最大页码
        pag = re.search(r'pagination-wrapper[^>]*>(.*?)</div>\s*</div>', html_text, re.S)
        if pag:
            nums = re.findall(r'>(\d{1,5})<', pag.group(1))
            if nums:
                filtered = [int(x) for x in nums if 1 <= int(x) <= 99999]
                if filtered:
                    return max(page, max(filtered))

        # 方式 C: 找 /sort-2-N/ 格式的页码
        nums = re.findall(r'/\d+-2-(\d+)/?', html_text)
        if nums:
            return max(page, max(int(x) for x in nums))

        # 方式 D: 找 ?page=N 格式的页码
        nums = re.findall(r'[?&]page=(\d+)', html_text)
        if nums:
            return max(page, max(int(x) for x in nums))

        return page

    # ==================================================================
    # 九、首页
    # ==================================================================
    def homeContent(self, filter=True):
        classes = [{'type_id': t, 'type_name': n} for t, n in self.CATEGORIES]
        result = {
            'class': classes,
            'filters': self.FILTERS,
            'parse': 0,
            'jx': 0,
        }
        try:
            html = self._fetch_text(self.host + '/')
            result['list'] = self._parse_list(html)[:40]
        except Exception as e:
            self._log('homeContent 异常: %s' % e)
            result['list'] = []
        return result

    def homeVideoContent(self):
        try:
            html = self._fetch_text(self.host + '/')
            return {'list': self._parse_list(html)[:40], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log('homeVideoContent 异常: %s' % e)
            return {'list': [], 'parse': 0, 'jx': 0}

    # ==================================================================
    # 十、分类内容 (父分类 + 排序筛选器 + 分页)
    # ==================================================================
    def categoryContent(self, tid, pg, filter=True, extend=None):
        page = int(pg) if pg else 1
        try:
            url = self._ctg_url(tid, page, extend)
            html = self._fetch_text(url)
            items = self._parse_list(html)
            pc = self._pagecount(html, page)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or self.PER_PAGE,
                'total': pc * (len(items) or self.PER_PAGE),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log('categoryContent 异常: %s' % e)
            return {'list': [], 'page': page, 'pagecount': page,
                    'limit': self.PER_PAGE, 'total': 0, 'parse': 0, 'jx': 0}

    # ==================================================================
    # 十一、详情内容 (JSON-LD 解析)
    # ==================================================================
    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, (list, tuple)) else ids).strip()
        if not vid:
            return {'list': [], 'parse': 0, 'jx': 0}
        url = '%s/movie/detail/%s.html' % (self.host, vid)
        try:
            html = self._fetch_text(url)
            if not html:
                return self._empty_detail(vid)
            return {'list': [self._parse_detail(vid, html)], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log('detailContent 异常: %s' % e)
            return self._empty_detail(vid)

    def _empty_detail(self, vid):
        return {'list': [{
            'vod_id': vid, 'vod_name': '获取失败', 'vod_pic': '',
            'vod_play_from': '默認線路', 'vod_play_url': '',
        }], 'parse': 0, 'jx': 0}

    def _parse_jsonld(self, html):
        """解析 JSON-LD script 标签, 返回 VideoObject dict"""
        scripts = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S)
        for raw in scripts:
            try:
                data = json.loads(raw.strip())
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get('@type') == 'VideoObject':
                            return item
                        # @type 可能是 list
                        t = item.get('@type', '') if isinstance(item, dict) else ''
                        if isinstance(t, list) and 'VideoObject' in t:
                            return item
                    if data and isinstance(data[0], dict):
                        if data[0].get('contentUrl') or data[0].get('thumbnailUrl'):
                            return data[0]
                elif isinstance(data, dict):
                    if data.get('@type') == 'VideoObject':
                        return data
                    t = data.get('@type', '')
                    if isinstance(t, list) and 'VideoObject' in t:
                        return data
                    # 兜底: 有 contentUrl 就用
                    if data.get('contentUrl') or data.get('thumbnailUrl'):
                        return data
            except Exception:
                continue
        return {}

    def _parse_detail(self, vid, html):
        ld = self._parse_jsonld(html)

        # --- 标题 ---
        vod_name = self._strip(ld.get('name', ''))
        if not vod_name:
            m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
            if m:
                vod_name = self._strip(m.group(1))
        if not vod_name:
            m = re.search(r'<title>([^<]*?)(?:\s*-\s*[^<]*)?</title>', html)
            vod_name = self._strip(m.group(1)) if m else vid

        # --- 封面 ---
        pic = ''
        thumb = ld.get('thumbnailUrl', '')
        if isinstance(thumb, list):
            # 优先取中等尺寸 _m.webp
            for t in thumb:
                if '_m.' in str(t):
                    thumb = t
                    break
            if isinstance(thumb, list):
                thumb = thumb[0] if thumb else ''
        if thumb:
            pic = str(thumb)
        if not pic:
            m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
            if m:
                pic = m.group(1)
        if not pic:
            m = re.search(r'<img[^>]*class="[^"]*mgn-cover[^"]*"[^>]*src="([^"]+)"', html)
            if m:
                pic = m.group(1)

        # --- 简介 ---
        content = self._strip(ld.get('description', ''))
        if not content:
            m = re.search(r'property="og:description"\s+content="([^"]+)"', html)
            if m:
                content = self._strip(m.group(1))
        if not content:
            m = re.search(r'<meta name="description" content="([^"]*)"', html)
            if m:
                content = self._strip(m.group(1))

        # --- 番号 ---
        code = ''
        ident = ld.get('identifier', {})
        if isinstance(ident, dict):
            code = self._strip(ident.get('Value', ''))
        elif isinstance(ident, str):
            code = self._strip(ident)
        if not code:
            m = re.search(r'data-movie-code="([^"]+)"', html)
            if m:
                code = self._strip(m.group(1))

        # --- 演员 ---
        actors = ld.get('actor', [])
        if isinstance(actors, dict):
            actors = [actors]
        actor_names = []
        for a in actors:
            if isinstance(a, dict):
                n = self._strip(a.get('name', ''))
                if n:
                    actor_names.append(n)
            elif isinstance(a, str):
                n = self._strip(a)
                if n:
                    actor_names.append(n)
        actor = ' '.join(actor_names)

        # --- 发布日期 ---
        upload_date = self._strip(ld.get('uploadDate', ''))

        # --- 时长 ---
        duration_raw = self._strip(ld.get('duration', ''))
        duration = self._fmt_duration(duration_raw)

        # --- 类型 ---
        type_name = ''
        m = re.search(r'movie-type-badge[^>]*>\s*([^<]+?)\s*<', html)
        if m:
            type_name = self._strip(m.group(1))

        # --- 标签 ---
        tags = re.findall(
            r'class="[^"]*tag-item[^"]*keyword-tag-link[^"]*"[^>]*>\s*([^<]+?)\s*<',
            html)
        tag_str = ' '.join(self._strip(t) for t in tags[:10])

        # --- 观看次数 ---
        view_count = ''
        stats = ld.get('interactionStatistic', {})
        if isinstance(stats, dict):
            view_count = str(stats.get('userInteractionCount', ''))
        elif isinstance(stats, list):
            for s in stats:
                if isinstance(s, dict):
                    view_count = str(s.get('userInteractionCount', ''))
                    if view_count:
                        break

        # --- 播放: embedUrl 优先 (/Player?url=xxx -> 带 token 的 m3u8)
        #     contentUrl 是裸 m3u8 (无 token, 返回 403)
        #     embedUrl 指向 /Player?url=xxx, 其内有带 token 的 m3u8
        #     playerContent 抓 embed 页解析带 token 的直链
        play_url = self._fix(self._strip(ld.get('embedUrl', '')))
        if not play_url:
            # 兜底: contentUrl (裸 m3u8, 可能 403, 但仍尝试)
            play_url = self._fix(self._strip(ld.get('contentUrl', '')))

        # --- 播放线路 ---
        vod_play_from = '酷鱼专线'
        vod_play_url = ''
        if play_url:
            vod_play_url = '正片$%s' % play_url

        return {
            'vod_id': vid,
            'vod_name': vod_name[:200],
            'vod_pic': self._fix(pic),
            'type_name': (type_name + ' ' + tag_str).strip()[:60],
            'vod_remarks': (code or duration or '')[:60],
            'vod_year': upload_date[:4] if upload_date else '',
            'vod_actor': actor[:500],
            'vod_content': content[:2000],
            'vod_play_from': vod_play_from,
            'vod_play_url': vod_play_url,
        }

    @staticmethod
    def _fmt_duration(iso):
        """ISO 8601 时长 PT2H38M29S -> 2:38:29"""
        if not iso:
            return ''
        m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso)
        if not m:
            return iso
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        if h:
            return '%d:%02d:%02d' % (h, mi, s)
        return '%d:%02d' % (mi, s)

    # ==================================================================
    # 十二、播放
    # ==================================================================
    def playerContent(self, flag, id, vipFlags=None):
        url = str(id or '').strip()
        headers = {
            'User-Agent': self.UA,
            'Referer': self.host + '/',
        }
        try:
            # /Player?url=xxx 或绝对 URL embed 页: 抓取带 token 的 m3u8
            if url.startswith('http') or url.startswith('/Player'):
                real = self._resolve_embed(self._fix(url))
                if real and self.isVideoFormat(real):
                    # parse:1 让播放器走 localProxy, localProxy 改写 m3u8 相对路径补 token
                    return self._play(real, headers, parse=1)
                # 解析失败, 交 APP 处理
                return self._play(self._fix(url), headers, parse=1)

            # 纯 id 或 id$xxx: 回详情页拿 embedUrl
            vid = url
            if '$' in url:
                vid = url.split('$')[-1]
            # 去掉可能的 "正片$" 前缀
            vid = re.sub(r'^[^$]+\$', '', vid) if '$' in url else vid
            html = self._fetch_text('%s/movie/detail/%s.html' % (self.host, vid))
            ld = self._parse_jsonld(html)
            # 优先 embedUrl (带 token)
            embed = self._fix(self._strip(ld.get('embedUrl', '')))
            if embed:
                real = self._resolve_embed(embed)
                if real and self.isVideoFormat(real):
                    return self._play(real, headers, parse=1)
                return self._play(embed, headers, parse=1)
            # 兜底: contentUrl
            real = self._fix(self._strip(ld.get('contentUrl', '')))
            if real and self.isVideoFormat(real):
                return self._play(real, headers, parse=1)
            return self._play(url, headers, parse=1)
        except Exception as e:
            self._log('playerContent 异常: %s' % e)
            return self._play(url, headers, parse=1)

    def _resolve_embed(self, embed_url):
        """抓 /Player?url=xxx embed 页, 提取带 token 的 m3u8 直链

        embed 页内有两个 m3u8:
          1. 裸 m3u8 (无 token, 403)
          2. 带 ?token=HS256-xxx&expires=xxx 的 m3u8 (200 OK)
        优先返回带 token 的。

        embed 页源码格式:
          var source = "https://videocdn.avking.xyz/.../playlist.m3u8?token=...&expires=...&token_path=...";
          var originalM3u8Url = 'https://videocdn.avking.xyz/.../playlist.m3u8';
        """
        try:
            if not embed_url.startswith('http'):
                embed_url = self._fix(embed_url)
            html = self._fetch_text(embed_url, referer=self.host + '/')
            if not html:
                return ''
            # 优先: 带 token 的 m3u8 (匹配 "..." 或 '...' 内的 URL)
            m = re.search(r'["\']\s*(https?://[^\s"\']+\.m3u8\?token=[^\s"\']+)\s*["\']', html)
            if m:
                return m.group(1).replace('\\/', '/')
            # 兜底: var source = "xxx.m3u8" (无引号也兜底)
            m = re.search(r'(?:source|src|url|file)\s*[:=]\s*["\']([^"\']+\.m3u8[^"\']*)["\']', html, re.I)
            if m:
                return m.group(1).replace('\\/', '/')
            # 兜底2: 任意引号包裹的 m3u8
            m = re.search(r'["\']\s*(https?://[^\s"\']+\.m3u8[^\s"\']*)\s*["\']', html)
            if m:
                return m.group(1).replace('\\/', '/')
            # 兜底3: mp4
            m = re.search(r'["\']\s*(https?://[^\s"\']+\.mp4[^\s"\']*)\s*["\']', html)
            if m:
                return m.group(1).replace('\\/', '/')
            return ''
        except Exception as e:
            self._log('embed 直链提取失败: %s' % e)
            return ''

    def _extract_direct(self, embed_url):
        """通用 embed 页直链提取 (保留兼容)"""
        return self._resolve_embed(embed_url)

    @staticmethod
    def _play(url, headers, parse=0):
        return {
            'parse': parse,
            'playUrl': '',
            'url': url,
            'header': json.dumps(headers),
            'jx': 0,
            'contentType': 'application/vnd.apple.mpegurl' if '.m3u8' in str(url) else '',
        }

    # ==================================================================
    # 十三、搜索
    # ==================================================================
    def searchContent(self, key, quick=False, pg='1'):
        page = int(pg) if pg else 1
        try:
            kw = urllib.parse.quote(str(key), safe='')
            if page <= 1:
                url = '%s/search/%s' % (self.host, kw)
            else:
                url = '%s/search/%s/%d' % (self.host, kw, page)
            html = self._fetch_text(url)
            items = self._parse_list(html)
            pc = self._pagecount(html, page)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or self.PER_PAGE,
                'total': pc * (len(items) or self.PER_PAGE),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log('searchContent 异常: %s' % e)
            return {'list': [], 'page': page, 'pagecount': page,
                    'limit': self.PER_PAGE, 'total': 0, 'parse': 0, 'jx': 0}

    def searchContentPage(self, key, quick=False, pg='1'):
        return self.searchContent(key, quick, pg)


# ======================================================================
# 本地自测
# ======================================================================
if __name__ == '__main__':
    sp = Spider()
    sp.init()

    print('\n================ 1. 首页 / 分类 / 筛选器 ================')
    home = sp.homeContent(True)
    print('父分类 %d 个: %s' % (
        len(home['class']),
        ', '.join('%s(%s)' % (c['type_name'], c['type_id']) for c in home['class'])
    ))
    print('首页推荐 %d 条' % len(home['list']))
    for v in home['list'][:5]:
        print('   %-28s id=%-22s 封面=%s' % (
            v['vod_name'][:26], v['vod_id'][:20],
            'OK' if v['vod_pic'].startswith('http') else '缺失'
        ))
    print('筛选器:')
    for tid, groups in home['filters'].items():
        tn = dict(sp.CATEGORIES).get(tid, tid)
        print('   %s(%s): %s' % (
            tn, tid,
            ' | '.join('%s×%d' % (g['name'], len(g['value'])) for g in groups)
        ))

    print('\n================ 2. 分类 + 分页 ================')
    for tid, pg in [('censored', 1), ('censored', 3), ('uncensored', 1), ('new', 1)]:
        r = sp.categoryContent(tid, pg, True, {})
        tn = dict(sp.CATEGORIES).get(tid, tid)
        first = r['list'][0]['vod_name'][:20] if r['list'] else '-'
        print('   %s(%s) 第%d页: %2d 条 / 共 %d 页  首条=%s' % (
            tn, tid, pg, len(r['list']), r['pagecount'], first
        ))

    print('\n================ 3. 排序筛选器抽查 ================')
    for sort_v, sort_n in [('1', '按新片發行'), ('2', '按觀看次數'), ('5', '最新更新')]:
        r = sp.categoryContent('censored', 1, True, {'sort': sort_v})
        first = r['list'][0]['vod_name'][:18] if r['list'] else '-'
        print('   有碼>%s: %d 条 / %d 页  首条=%s' % (
            sort_n, len(r['list']), r['pagecount'], first
        ))

    print('\n================ 4. 详情 ================')
    target = home['list'][0] if home['list'] else None
    det = None
    if target:
        det = sp.detailContent([target['vod_id']])['list'][0]
        print('   名称: %s' % det['vod_name'][:50])
        print('   封面: %s %s' % (
            'OK' if det['vod_pic'].startswith('http') else '缺失',
            det['vod_pic'][:70]
        ))
        print('   类型: %s' % det.get('type_name', '')[:50])
        print('   备注: %s' % det.get('vod_remarks', ''))
        print('   演员: %s' % det.get('vod_actor', '')[:60])
        print('   简介: %s...' % det.get('vod_content', '')[:70])
        fl = det['vod_play_from'].split('$$$')
        ul = det['vod_play_url'].split('$$$')
        print('   线路 %d 条:' % len(fl))
        for f, u in zip(fl, ul):
            print('      %s: %s' % (f, u[:70]))

    print('\n================ 5. 播放解析 ================')
    if det and det['vod_play_url']:
        play_id = det['vod_play_url'].split('$')[-1]
        p = sp.playerContent(det['vod_play_from'], play_id)
        print('   [%s] parse=%d  url=%s' % (
            det['vod_play_from'], p['parse'], p['url'][:100]
        ))
        # localProxy 验证
        if p['url'] and p['url'].startswith('http'):
            lp = sp.localProxy(p['url'])
            if lp and lp[0] == 200:
                body = lp[2]
                if isinstance(body, bytes):
                    body = body.decode('utf-8', 'ignore')
                lines = body.strip().split('\n')
                print('   localProxy: %d 字节, %d 行' % (len(body), len(lines)))
                # 检查是否已从 master 解析到 sub (不应有 STREAM-INF)
                has_stream_inf = any('#EXT-X-STREAM-INF' in l for l in lines)
                print('   master->sub 解析: %s' % ('已解析' if not has_stream_inf else '未解析(仍有 STREAM-INF)'))
                # 检查相对路径是否被改写
                rel = [l for l in lines if l.strip() and not l.strip().startswith('#') and not l.strip().startswith('http')]
                print('   改写后非 http 路径行数: %d (期望 0)' % len(rel))
                # 检查 TS 是否带 token
                ts_lines = [l for l in lines if l.strip() and not l.strip().startswith('#') and '.ts' in l.lower()]
                ts_with_token = [l for l in ts_lines if 'token=' in l]
                print('   TS 分片: %d 条, 带 token: %d 条' % (len(ts_lines), len(ts_with_token)))
                for l in ts_lines[:3]:
                    print('   样本 TS: %s' % l.strip()[:130])
                # 验证第一个 TS 可访问
                if ts_with_token:
                    ts_url = ts_with_token[0].strip()
                    try:
                        ts_r = sp.fetch(ts_url, headers=sp._headers(referer=sp.host + '/'),
                                        timeout=15, verify=False)
                        ts_status = getattr(ts_r, 'status_code', 0)
                        ts_len = len(getattr(ts_r, 'content', b''))
                        print('   TS 直连验证: HTTP %d, %d 字节' % (ts_status, ts_len))
                    except Exception as e:
                        print('   TS 直连验证: 异常 %s' % e)
            else:
                print('   localProxy: 失败')

    print('\n================ 6. 搜索 + 搜索分页 ================')
    for kw in ['福原美奈', '中出', '人妻']:
        r = sp.searchContent(kw, False, '1')
        first = r['list'][0]['vod_name'][:22] if r['list'] else '-'
        print('   搜索[%s]: %d 条 / %d 页  首条=%s' % (
            kw, len(r['list']), r['pagecount'], first
        ))

    print('\n完成。')
