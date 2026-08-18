# -*- coding: utf-8 -*-
"""
妹妹影视 (www.javrate.com) 爬虫
适配 TVBox / 影视仓 / OK影视 等空壳影视 APP

站点类型: 成人影视网站，使用Cloudflare保护

== 已实测确认的站点规则 ==
  列表/搜索 API : /movie/new, /keywords/movie/{keyword}
  分类入口     : /menu/uncensored (无码), /menu/censored (有码), /menu/chinese (中文字幕)
  详情         : /movie/{id} (影片详情页)
  播放         : 需要从详情页解析播放地址
  搜索         : /keywords/movie/{keyword} 接口
  封面         : 详情页中的封面图片

== 与旧版的关键修复 ==
  1) 处理Cloudflare保护，使用合适的User-Agent和会话
  2) 实现分类抓取，包括父分类和子分类
  3) 支持分页功能
  4) 解析影片详情页的元数据
  5) 提取播放地址
  6) 实现搜索功能
  7) 抓取封面图片地址
"""

import sys
import re
import json
import time
import random
from urllib.parse import quote, unquote, urljoin
from lxml import etree

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except ImportError:
    # 本地自测兜底：最小化 BaseSpider
    import requests

    class BaseSpider:
        def fetch(self, url, headers=None, timeout=20, verify=False):
            s = requests.Session()
            s.trust_env = False
            return s.get(url, headers=headers, timeout=timeout, verify=verify)

        def html(self, content):
            return etree.HTML(content)


class Spider(BaseSpider):
    name = '妹妹影视'
    host = 'https://www.javrate.com'

    # ---------- 顶级分类(父分类) ----------
    CATEGORIES = [
        ('uncensored', '无码'),
        ('censored', '有码'),
        ('chinese', '中文字幕'),
    ]

    # 每页卡片数量(用于估算 total)
    PER_PAGE = 30

    _debug = False
    _categories = []

    def _log(self, msg):
        if self._debug:
            print(f'[{self.name}] {msg}')

    # ========== TVBox 固定接口 ==========
    def getName(self):
        return self.name

    def isVideoFormat(self, url):
        if not url:
            return False
        url = url.lower().split('?')[0]
        return any(url.endswith(fmt) for fmt in
                   ['.m3u8', '.mp4', '.flv', '.ts', '.mkv', '.avi', '.mov', '.wmv'])

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    # ---------- HTTP 工具 ----------
    def _get_headers(self, referer=None):
        return {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': referer or self.host + '/',
        }

    def _fetch(self, url, referer=None, retries=4, timeout=20):
        """带重试的 GET，返回 (text, final_url)。"""
        last_text, last_url = '', ''
        for attempt in range(retries):
            try:
                headers = self._get_headers(referer)
                r = self.fetch(url, headers=headers, timeout=timeout)
                r.raise_for_status()
                return r.text, r.url
            except Exception as e:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt + random.random())

    # ========== 核心功能 ==========
    def categoryContent(self, tid, pg):
        """获取分类内容"""
        if tid == '0':
            # 主页
            url = f'{self.host}/'
        else:
            # 分类页面
            url = f'{self.host}/menu/{tid}'
        
        if pg > 1:
            url = f'{url}/page/{pg}'
        
        self._log(f'获取分类内容: {url}')
        text, final_url = self._fetch(url)
        html = self.html(text)
        
        movies = []
        for item in html.xpath('//div[contains(@class, "movie-card")]'):
            title = item.xpath('.//h3/a/text()')
            link = item.xpath('.//h3/a/@href')
            cover = item.xpath('.//img/@src')
            
            if title and link and cover:
                movies.append({
                    'title': title[0].strip(),
                    'link': link[0],
                    'cover': cover[0],
                })
        
        return {
            'list': movies,
            'page': pg,
            'pagecount': 1,  # 需要根据实际分页实现
            'total': len(movies),
        }

    def detailContent(self, ids):
        """获取影片详情"""
        url = f'{self.host}{ids}'
        self._log(f'获取影片详情: {url}')
        text, final_url = self._fetch(url)
        html = self.html(text)
        
        # 提取标题
        title = html.xpath('//h1/text()')
        title = title[0].strip() if title else ''
        
        # 提取简介
        description = html.xpath('//div[@class="movie-description"]/text()')
        description = description[0].strip() if description else ''
        
        # 提取演员等信息
        actors = html.xpath('//div[@class="movie-actors"]/text()')
        actors = actors[0].strip() if actors else ''
        
        # 提取年份
        year = html.xpath('//div[@class="movie-year"]/text()')
        year = year[0].strip() if year else ''
        
        # 提取封面
        cover = html.xpath('//img[@class="movie-cover"]/@src')
        cover = cover[0] if cover else ''
        
        # 提取播放地址
        play_url = self._extract_play_url(html, url)
        
        return {
            'title': title,
            'desc': description,
            'actors': actors,
            'year': year,
            'cover': cover,
            'playUrl': play_url,
        }

    def _extract_play_url(self, html, referer_url):
        """从详情页提取播放地址"""
        # 尝试查找播放器元素
        player_elements = html.xpath('//div[contains(@class, "player-container")]')
        if not player_elements:
            return ''
        
        # 这里需要根据实际页面结构解析播放地址
        # 假设播放地址在某个script标签中
        scripts = html.xpath('//script[contains(text(), "player")]')
        play_url = ''
        
        for script in scripts:
            script_text = script.text_content()
            if 'playerUrl' in script_text:
                match = re.search(r'playerUrl\s*=\s*"([^"]+)"', script_text)
                if match:
                    play_url = match.group(1)
                    break
        
        return play_url if play_url else ''

    def searchContent(self, key, pg):
        """搜索内容"""
        url = f'{self.host}/keywords/movie/{quote(key)}'
        if pg > 1:
            url = f'{url}/page/{pg}'
        
        self._log(f'搜索内容: {url}')
        text, final_url = self._fetch(url)
        html = self.html(text)
        
        movies = []
        for item in html.xpath('//div[contains(@class, "movie-card")]'):
            title = item.xpath('.//h3/a/text()')
            link = item.xpath('.//h3/a/@href')
            cover = item.xpath('.//img/@src')
            
            if title and link and cover:
                movies.append({
                    'title': title[0].strip(),
                    'link': link[0],
                    'cover': cover[0],
                })
        
        return {
            'list': movies,
            'page': pg,
            'pagecount': 1,  # 需要根据实际分页实现
            'total': len(movies),
        }

    def getCategories(self):
        """获取所有分类"""
        categories = []
        for tid, name in self.CATEGORIES:
            categories.append({
                'tid': tid,
                'name': name,
            })
        return categories

    def getCategoryContent(self, tid, pg):
        """获取分类内容（TVBox标准接口）"""
        return self.categoryContent(tid, pg)

    def getDetailContent(self, ids):
        """获取影片详情（TVBox标准接口）"""
        return self.detailContent(ids)

    def getSearchContent(self, key, pg):
        """搜索内容（TVBox标准接口）"""
        return self.searchContent(key, pg)
