# -*- coding: utf-8 -*-
"""
javrate影视 - TVBox/影视仓/OK影视 通用爬虫
目标站点: https://www.javrate.com/
适配接口: homeContent / categoryContent / detailContent / playerContent / searchContent
站点结构要点:
  - 父分类: /menu/{censored|uncensored|chinese}  +  /movie/new  /best  /movie/subtitle
  - 子分类筛选器(全局浮层 filteroverlay): 影片关键字 /keywords/movie/{kw}、女優 /actor/list/1-{type}-{page}.html、
    廠商 /issuer/{name}、專輯 /moviesets/{name}
  - 分页: menu -> /menu/{cat}/5-2-{page}; keywords -> ?page={pg}&moviesort=5; moviesets -> ?page={pg}&sort=5;
    search -> /search/{kw}/?tab=movie&page={pg}; 总页数从 data-page-info="第 X 頁 / 共 N 頁" 提取
  - 列表卡片: div.mgn-box (封面 .mgn-cover, 详情 /movie/detail/{uuid}.html, 番号 strong.fg-main)
  - 详情页 JSON-LD(VideoObject) 直接含 m3u8 直链 contentUrl、封面 thumbnailUrl、简介 description、演员 actor、日期 uploadDate
  - 播放线路名称固定为「酷鱼专线」(vod_play_from 写死)
  - 封面直接返回图床原图地址(_fix_url 仅补全 // 与 / 前缀), 不依赖 localProxy 代理
"""
import re
import json
import sys
import time
import random
import urllib.parse

try:
    from curl_cffi import requests as _curl_requests
    _USE_CURL = True
except Exception:
    _curl_requests = None
    _USE_CURL = False

import requests as _requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except ImportError:
    class BaseSpider:
        def fetch(self, url, headers=None, timeout=20, verify=False, cookies=None):
            s = _requests.Session()
            s.trust_env = False
            return s.get(url, headers=headers, timeout=timeout, verify=verify, cookies=cookies)


class Spider(BaseSpider):
    name = 'javrate影视'
    host = 'https://www.javrate.com'

    # ==================== 父分类定义 ====================
    # type_id -> (分类名, 列表路径模板 {page} 会被替换; sort 段位 5-2 为站点默认「最新更新」)
    ZONES = [
        {'type_id': 'censored',   'type_name': '日本A片', 'path': '/menu/censored/5-2-{page}'},
        {'type_id': 'uncensored', 'type_name': '無碼A片', 'path': '/menu/uncensored/5-2-{page}'},
        {'type_id': 'chinese',    'type_name': '國產AV',  'path': '/menu/chinese/5-2-{page}'},
        {'type_id': 'new',        'type_name': '最新A片', 'path': '/movie/new/5-2-{page}'},
        {'type_id': 'hot',        'type_name': '最多人看', 'path': '/best/5-2-{page}'},
        {'type_id': 'subtitle',   'type_name': '中文字幕', 'path': '/movie/subtitle/5-2-{page}'},
    ]

    # 子分类筛选器(取自站点全局筛选浮层)
    # kw: 影片关键字 -> /keywords/movie/{kw}?page={pg}&moviesort=5
    KEYWORDS = [
        ('口交', '口交'), ('中出', '中出'), ('女上位', '女上位'), ('騎乘位', '騎乘位'),
        ('後入', '後入'), ('美乳', '美乳'), ('手指插入', '手指插入'), ('美腳', '美腳'),
    ]
    # act: 女優分類 -> /actor/list/1-{type}-{page}.html (列表页为女優卡片, 作为导航分类)
    ACTORS = [
        ('知名女優', '1'), ('日本女優', '3'), ('國產女優', '4'), ('素人女優', '5'),
    ]
    # issuer: 廠商 -> /issuer/{name}?page={pg}
    ISSUERS = [
        ('麻豆傳媒', '麻豆傳媒'), ('SOD', 'SOD'), ('蚊香社', '蚊香社'), ('S1', 'S1'), ('一本道', '一本道'),
    ]
    # set: 專輯 -> /moviesets/{name}?page={pg}&sort=5
    MOVIESETS = [
        ('SOD女子社員', 'SOD女子社員'), ('女搜查官', '女搜查官'), ('出差被玩弄侵犯', '出差被玩弄侵犯'),
        ('AFTER6', 'AFTER6'), ('時間靜止系列', '時間靜止系列'),
    ]

    def __init__(self):
        super().__init__()
        self.ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        self._debug = True
        self._session = _requests.Session()
        self._session.trust_env = False
        self._last_ts = 0.0
        # 代理策略: 站点有 Cloudflare/限流, 优先走本机 SOCKS5 代理; 不可用时自动直连
        self.proxies = {'http': 'socks5h://127.0.0.1:10808', 'https': 'socks5h://127.0.0.1:10808'}
        self._proxy_ok = True
        self._proxy_fail_count = 0

    def _log(self, msg):
        if self._debug:
            print('[%s] %s' % (self.name, msg))

    # ==================== TVBox 基础接口 ====================
    def getName(self):
        return self.name

    def isVideoFormat(self, url):
        if not url:
            return False
        url = url.lower()
        return any(url.endswith(fmt) for fmt in ['.m3u8', '.mp4', '.flv', '.ts', '.mkv', '.webm', '.m3u'])

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    def init(self, extend=''):
        self._log('初始化完成, 站点: %s' % self.host)

    # ==================== 请求工具 ====================
    def _get_headers(self, referer=None):
        return {
            'User-Agent': self.ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
            'Referer': referer or (self.host + '/'),
            'Upgrade-Insecure-Requests': '1',
        }

    def _throttle(self):
        """限速: 避免触发站点风控"""
        now = time.time()
        gap = now - self._last_ts
        if gap < 0.8:
            time.sleep(0.8 - gap + random.uniform(0, 0.4))
        self._last_ts = time.time()

    def _is_blocked(self, html):
        if not html:
            return True
        if len(html) < 5000:
            return True
        low = html.lower()
        # 真正的 Cloudflare 挑战页特征 (注意: 正常页面也含 /cdn-cgi/challenge-platform/ beacon 脚本, 不能作为判据)
        if '<title>just a moment' in low or 'cf-browser-verification' in low or \
                'cf_chl' in low or 'challenge-form' in low or 'attention required' in low:
            return True
        return False

    def _fetch(self, url, referer=None, retries=2):
        """带重试抓取页面, 优先 curl_cffi(代理) 绕过 Cloudflare, 回退 requests(代理/直连). 返回 html 字符串"""
        text = ''
        for i in range(retries + 1):
            self._throttle()
            if _USE_CURL:
                try:
                    kwargs = {'headers': self._get_headers(referer), 'timeout': 30, 'verify': False, 'impersonate': 'chrome120'}
                    if self._proxy_ok:
                        kwargs['proxies'] = self.proxies
                    r = _curl_requests.get(url, **kwargs)
                    text = r.text
                    if not self._is_blocked(text):
                        return text
                except Exception as e:
                    self._log('curl 请求异常 %s: %s' % (url, e))
            try:
                kwargs = {'headers': self._get_headers(referer), 'timeout': 30, 'verify': False}
                if self._proxy_ok:
                    kwargs['proxies'] = self.proxies
                r = self._session.get(url, **kwargs)
                text = r.text
                if not self._is_blocked(text):
                    self._proxy_fail_count = 0
                    return text
                self._log('请求疑似风控页 [%s] %s' % (r.status_code, url))
            except Exception as e:
                self._log('requests 请求异常 %s: %s' % (url, e))
                self._proxy_fail_count += 1
                if self._proxy_fail_count >= 3:
                    self._proxy_ok = False
                    self._log('代理连续失败, 切换直连')
            if i < retries:
                time.sleep(4 + random.uniform(0, 3))
        return text

    def _fix_url(self, url):
        """仅补全 // 与 / 前缀, 封面/播放地址保持图床原图地址, 不经过 localProxy"""
        if not url:
            return ''
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return self.host + url
        if not url.startswith('http'):
            return self.host + '/' + url
        return url

    def _clean_text(self, text):
        if not text:
            return ''
        text = re.sub(r'<[^>]+>', '', str(text))
        return re.sub(r'\s+', ' ', text.replace('\r', '').replace('\n', ' ')).strip()

    # ==================== 子分类筛选器 ====================
    def _build_filters(self):
        filters = {}
        for z in self.ZONES:
            filters[z['type_id']] = [
                {
                    'key': 'kw',
                    'name': '影片标签',
                    'value': [{'n': '全部', 'v': ''}] + [{'n': n, 'v': v} for n, v in self.KEYWORDS],
                },
                {
                    'key': 'act',
                    'name': '女優',
                    'value': [{'n': '全部', 'v': ''}] + [{'n': n, 'v': v} for n, v in self.ACTORS],
                },
                {
                    'key': 'issuer',
                    'name': '廠商',
                    'value': [{'n': '全部', 'v': ''}] + [{'n': n, 'v': v} for n, v in self.ISSUERS],
                },
                {
                    'key': 'set',
                    'name': '專輯',
                    'value': [{'n': '全部', 'v': ''}] + [{'n': n, 'v': v} for n, v in self.MOVIESETS],
                },
            ]
        return filters

    # ==================== 首页 ====================
    def homeContent(self, filter=False):
        try:
            html = self._fetch(self.host + '/movie/new', referer=self.host + '/')
            items = self._parse_video_list(html) if html else []
        except Exception as e:
            self._log('homeContent 列表异常: %s' % e)
            items = []
        return {
            'class': [{'type_id': z['type_id'], 'type_name': z['type_name']} for z in self.ZONES],
            'list': items[:24],
            'filters': self._build_filters(),
            'parse': 0,
            'jx': 0,
        }

    def homeVideoContent(self):
        try:
            html = self._fetch(self.host + '/movie/new', referer=self.host + '/')
            items = self._parse_video_list(html) if html else []
        except Exception:
            items = []
        return {'list': items[:24], 'parse': 0, 'jx': 0}

    # ==================== 列表解析 ====================
    def _parse_video_list(self, html):
        """解析 mgn-box 影片卡片 (分类/搜索/首页通用)"""
        items = []
        if not html or len(html) < 8000:
            return items
        seen = set()
        for block in re.findall(r'<div class="mgn-box">.*?(?=<div class="mgn-box">|$)', html, re.S):
            try:
                m = re.search(r'<a[^>]+href="(/movie/detail/[^"]+)"[^>]*>', block)
                if not m:
                    continue
                href = m.group(1)
                if href in seen:
                    continue
                seen.add(href)
                # 封面
                pic = ''
                im = re.search(r'<img[^>]+src="([^"]+)"[^>]*class="mgn-cover"', block) or \
                     re.search(r'class="mgn-cover"[^>]+src="([^"]+)"', block)
                if im:
                    pic = im.group(1)
                if not pic:
                    im2 = re.search(r'<img[^>]+src="([^"]+)"', block)
                    if im2:
                        pic = im2.group(1)
                # 标题: strong 为番号, h3 全文本为番号+标题
                code = ''
                sm = re.search(r'<strong[^>]*>\s*([^<]+?)\s*</strong>', block)
                if sm:
                    code = html_unescape(sm.group(1)).strip()
                hm = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.S)
                title = html_unescape(self._clean_text(hm.group(1))) if hm else ''
                if not title:
                    title = code
                # 演员
                actor = ''
                am = re.search(r'mgn-actress[^>]*>\s*<a[^>]*>\s*([^<]+?)\s*</a>', block, re.S)
                if am:
                    actor = html_unescape(am.group(1)).strip()
                # 年份 / 类型
                year = ''
                ym = re.search(r'mgn-badge-year[^>]*>\s*([^<]+?)\s*<', block)
                if ym:
                    year = ym.group(1).strip()
                remark = year
                items.append({
                    'vod_id': href,
                    'vod_name': title,
                    'vod_pic': self._fix_url(pic),
                    'vod_remarks': remark,
                    'vod_actor': actor,
                })
            except Exception:
                continue
        return items

    def _parse_actor_list(self, html):
        """解析女優列表卡片 (actor-grid), 作为筛选器导航条目"""
        items = []
        if not html or len(html) < 8000:
            return items
        seen = set()
        for m in re.finditer(r'<div class="actor-card">(.*?)(?=<div class="actor-card">|$)', html, re.S):
            try:
                block = m.group(1)
                am = re.search(r'href="(/actor/detail/[^"]+)"', block)
                if not am:
                    continue
                href = am.group(1)
                if href in seen:
                    continue
                seen.add(href)
                nm = re.search(r'data-actress-name="([^"]+)"', block) or \
                     re.search(r'title="([^"]+)"[^>]*class="actress-card-link"', block)
                name = html_unescape(nm.group(1)).strip() if nm else ''
                pic = ''
                im = re.search(r'<img[^>]+src="([^"]+)"', block)
                if im:
                    pic = im.group(1)
                if name:
                    items.append({
                        'vod_id': 'act:' + href,
                        'vod_name': name,
                        'vod_pic': self._fix_url(pic),
                        'vod_remarks': '女優',
                    })
            except Exception:
                continue
        return items

    def _extract_pagecount(self, html):
        """从 data-page-info 或 pagination-info 提取总页数"""
        if not html:
            return 999
        m = re.search(r'data-page-info="[^"]*?共\s*(\d+)\s*頁[^"]*"', html)
        if m:
            return int(m.group(1))
        m = re.search(r'共\s*(\d+)\s*頁', html)
        if m:
            return int(m.group(1))
        # 从分页链接提取最大页码
        nums = [int(x) for x in re.findall(r'page[/=?](\d+)', html)]
        if nums:
            return max(nums)
        return 999

    # ==================== 分类 ====================
    def _build_cat_url(self, tid, pg, extend):
        extend = extend or {}
        kw = extend.get('kw') or ''
        act = extend.get('act') or ''
        issuer = extend.get('issuer') or ''
        mset = extend.get('set') or ''
        if kw:
            return '%s/keywords/movie/%s?page=%s&moviesort=5' % (self.host, urllib.parse.quote(kw), pg)
        if act:
            return '%s/actor/list/1-%s-%s.html' % (self.host, act, pg)
        if issuer:
            return '%s/issuer/%s?page=%s' % (self.host, urllib.parse.quote(issuer), pg)
        if mset:
            return '%s/moviesets/%s?page=%s&sort=5' % (self.host, urllib.parse.quote(mset), pg)
        path = ''
        for z in self.ZONES:
            if z['type_id'] == tid:
                path = z['path']
                break
        if not path:
            path = '/menu/censored/5-2-{page}'
        return self.host + path.format(page=pg)

    def categoryContent(self, tid, pg, filter, extend):
        try:
            if not pg:
                pg = '1'
            pg = str(pg)
            extend = extend or {}
            url = self._build_cat_url(tid, pg, extend)
            html = self._fetch(url, referer=self.host + '/menu/censored')
            if extend.get('act'):
                items = self._parse_actor_list(html)
            else:
                items = self._parse_video_list(html)
            pagecount = self._extract_pagecount(html)
            return {
                'list': items,
                'page': int(pg),
                'pagecount': pagecount,
                'limit': 20,
                'total': 99999,
            }
        except Exception as e:
            self._log('categoryContent 异常: %s' % e)
            return {'list': [], 'page': int(pg or 1), 'pagecount': 1, 'limit': 20, 'total': 0}

    # ==================== 详情 ====================
    def _extract_jsonld(self, html):
        """提取详情页 JSON-LD VideoObject"""
        for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
            try:
                data = json.loads(m.group(1).strip())
            except Exception:
                continue
            if isinstance(data, dict) and data.get('@type') == 'VideoObject':
                return data
        return None

    def _fmt_duration(self, duration):
        """ISO8601 时长 PT2H38M29S -> 2:38:29"""
        if not duration:
            return ''
        m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
        if not m:
            return ''
        h, mi, s = [int(x) if x else 0 for x in m.groups()]
        if h:
            return '%d:%02d:%02d' % (h, mi, s)
        if mi:
            return '%d:%02d' % (mi, s)
        return str(s)

    def detailContent(self, array):
        vid = array[0]
        try:
            # 女優详情页 (act: 前缀): 解析该女優影片列表, 取第一条用于播放
            if vid.startswith('act:'):
                return self._detail_actor(vid[4:])

            url = vid if vid.startswith('http') else self._fix_url(vid)
            html = self._fetch(url, referer=self.host + '/movie/new')
            if not html or len(html) < 8000:
                self._log('详情页抓取失败: %s' % url)
                return {'list': []}

            ld = self._extract_jsonld(html)
            name = ''
            code = ''
            hm = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
            if hm:
                name = html_unescape(self._clean_text(hm.group(1)))
            sm = re.search(r'<h1[^>]*>.*?<strong[^>]*>\s*([^<]+?)\s*</strong>', html, re.S)
            if sm:
                code = html_unescape(sm.group(1)).strip()

            desc = ''
            pic = ''
            play_url = ''
            year = ''
            actor = ''
            duration = ''
            if ld:
                name = name or ld.get('name', '')
                desc = ld.get('description', '') or ''
                pic = ld.get('thumbnailUrl', '') or ''
                year = (ld.get('uploadDate', '') or '')[:4]
                play_url = ld.get('contentUrl', '') or ''
                duration = ld.get('duration', '') or ''
                actors = ld.get('actor') or []
                actor = ' '.join([a.get('name', '') for a in actors if isinstance(a, dict) and a.get('name')])
                if not code:
                    ident = ld.get('identifier') or {}
                    if isinstance(ident, dict):
                        code = ident.get('Value', '') or ''
            if not pic:
                im = re.search(r'<iframe[^>]+poster=([^"&\s]+)', html)
                if im:
                    pic = urllib.parse.unquote(im.group(1))
            if not pic:
                im = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
                if im:
                    pic = im.group(1)
            if not actor:
                # 从 h1 尾部取「~ 演员」或演员区块
                am = re.search(r'<a[^>]*class="actor-name[^"]*"[^>]*>\s*([^<]+?)\s*</a>', html)
                if am:
                    actor = html_unescape(am.group(1)).strip()
            if not desc:
                dm = re.search(r'name="description"\s+content="([^"]*)"', html)
                if dm:
                    desc = html_unescape(dm.group(1))

            vod_name = name or code
            if code and code not in vod_name:
                vod_name = (code + ' ' + vod_name).strip()

            # 播放线路固定「酷鱼专线」, 单集正片 (时长 ISO8601 转 h:mm:ss)
            if play_url:
                ep_name = '正片'
                dur = self._fmt_duration(duration)
                if dur:
                    ep_name += ' ' + dur
                play_urls = '%s$%s' % (ep_name, play_url)
            else:
                play_urls = '播放$' + url

            detail = {
                'vod_id': vid,
                'vod_name': vod_name,
                'vod_pic': self._fix_url(pic),
                'vod_year': year,
                'vod_area': '',
                'vod_lang': '',
                'vod_actor': actor,
                'vod_director': '',
                'vod_content': desc,
                'vod_play_from': '酷鱼专线',
                'vod_play_url': play_urls,
            }
            return {'list': [detail]}
        except Exception as e:
            self._log('detailContent 异常: %s' % e)
            return {'list': []}

    def _detail_actor(self, actor_url):
        """女優详情页: 解析其影片列表, 逐部抓详情页取 m3u8 直链, 最多 5 集"""
        try:
            url = actor_url if actor_url.startswith('http') else self._fix_url(actor_url)
            html = self._fetch(url, referer=self.host + '/actor/list/1-1-1.html')
            if not html or len(html) < 8000:
                return {'list': []}
            items = self._parse_video_list(html)
            if not items:
                return {'list': []}
            # 女優名
            aname = ''
            ah = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
            if ah:
                aname = html_unescape(self._clean_text(ah.group(1)))
            vod_name = aname or ('女優: ' + items[0]['vod_name'])
            vod_pic = items[0]['vod_pic']
            # 逐部影片抓详情取 m3u8 直链 (最多 5 部)
            episodes = []
            for it in items[:5]:
                try:
                    durl = self._fix_url(it['vod_id'])
                    dhtml = self._fetch(durl, referer=url)
                    ld = self._extract_jsonld(dhtml) if dhtml else None
                    m3u8 = (ld or {}).get('contentUrl', '') or ''
                    if not m3u8:
                        continue
                    episodes.append((it['vod_name'], m3u8))
                except Exception:
                    continue
            if not episodes:
                return {'list': []}
            play_url = '#'.join(['%s$%s' % (n, u) for n, u in episodes])
            detail = {
                'vod_id': 'act:' + actor_url,
                'vod_name': vod_name,
                'vod_pic': self._fix_url(vod_pic),
                'vod_year': '',
                'vod_area': '',
                'vod_lang': '',
                'vod_actor': '',
                'vod_director': '',
                'vod_content': '',
                'vod_play_from': '酷鱼专线',
                'vod_play_url': play_url,
            }
            return {'list': [detail]}
        except Exception as e:
            self._log('女優详情异常: %s' % e)
            return {'list': []}

    # ==================== 搜索 ====================
    def searchContent(self, key, quick, pg='1'):
        try:
            if not pg:
                pg = '1'
            url = '%s/search/%s/?tab=movie&page=%s' % (self.host, urllib.parse.quote(key), pg)
            html = self._fetch(url, referer=self.host + '/')
            items = self._parse_video_list(html)
            return {'list': items}
        except Exception as e:
            self._log('searchContent 异常: %s' % e)
            return {'list': []}

    def searchContentPage(self, key, quick, pg='1'):
        return self.searchContent(key, quick, pg)

    # ==================== 播放 ====================
    def playerContent(self, flag, id, vipFlags):
        url = id
        if not url.startswith('http'):
            url = self._fix_url(url)
        return {
            'parse': 0,
            'playUrl': '',
            'url': url,
            'header': json.dumps({
                'Referer': self.host + '/',
                'User-Agent': self.ua,
            }, ensure_ascii=False),
        }

    # ==================== 封面代理(保留兼容, 实际不走) ====================
    def localProxy(self, param):
        try:
            url = ''
            if isinstance(param, str):
                url = param
            elif isinstance(param, dict):
                for k in ('url', 'pic', 'img', 'src'):
                    if param.get(k):
                        url = str(param[k])
                        break
            if not url or not url.startswith('http'):
                return [404, 'text/plain', b'', 'no url']
            r = self._session.get(url, headers={'User-Agent': self.ua, 'Referer': self.host + '/'}, timeout=15, verify=False,
                                  proxies=self.proxies if self._proxy_ok else None)
            ctype = r.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
            body = r.content or b''
            extra = 'Content-Type: %s\r\nCache-Control: public, max-age=86400\r\nContent-Length: %s\r\n' % (ctype, len(body))
            return [200, ctype, body, extra]
        except Exception as e:
            return [500, 'text/plain', b'', str(e)]


# ==================== 自测 ====================
if __name__ == '__main__':
    import importlib.util
    def load_spider(path):
        spec = importlib.util.spec_from_file_location('target_spider', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Spider()

    s = load_spider(__file__)
    print('--- homeContent ---')
    home = s.homeContent(True)
    print('class:', len(home['class']), [c['type_name'] for c in home['class']])
    print('list:', len(home['list']))
    if home['list']:
        print('  样例:', home['list'][0])
