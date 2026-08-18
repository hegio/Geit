# coding=utf-8
import re, json, requests, urllib.parse
from urllib.parse import quote
from lxml import etree
from base.spider import Spider

class Spider(Spider):
    def __init__(self):
        self.name = "javrate"
        self.host = "https://www.javrate.com"
        self.header = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': self.host + '/',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        }
        
        # 分类配置 - 可根据实际网站调整
        self.categories = [
            {"type_name": "最新", "type_id": "latest"},
            {"type_name": "热门", "type_id": "popular"},
            {"type_name": "评分", "type_id": "rated"},
            {"type_name": "日本", "type_id": "japanese"},
            {"type_name": "欧美", "type_id": "western"},
            {"type_name": "动漫", "type_id": "anime"}
        ]
        
        # 筛选器配置 - 可根据实际网站调整
        self.filters = {
            "latest": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最新", "v": "latest"}, {"n": "最热", "v": "popular"}, {"n": "评分", "v": "rated"}
                ]}
            ],
            "popular": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最热", "v": "popular"}, {"n": "最新", "v": "latest"}, {"n": "评分", "v": "rated"}
                ]}
            ],
            "rated": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "评分", "v": "rated"}, {"n": "最新", "v": "latest"}, {"n": "最热", "v": "popular"}
                ]}
            ],
            "japanese": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最新", "v": "latest"}, {"n": "最热", "v": "popular"}, {"n": "评分", "v": "rated"}
                ]}
            ],
            "western": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最新", "v": "latest"}, {"n": "最热", "v": "popular"}, {"n": "评分", "v": "rated"}
                ]}
            ],
            "anime": [
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最新", "v": "latest"}, {"n": "最热", "v": "popular"}, {"n": "评分", "v": "rated"}
                ]}
            ]
        }
        
        # 调试模式 - 设为True可输出更多调试信息
        self.debug_mode = True

    def getName(self):
        return self.name

    def init(self, extend=''):
        if self.debug_mode:
            print(f"初始化爬虫: {self.name}")
            print(f"目标站点: {self.host}")
        pass

    def _get(self, url, params=None):
        try:
            if self.debug_mode:
                print(f"请求URL: {url}")
            r = requests.get(url, headers=self.header, params=params, timeout=20, verify=False)
            r.encoding = r.apparent_encoding or 'utf-8'
            if self.debug_mode:
                print(f"响应状态码: {r.status_code}")
                print(f"响应内容长度: {len(r.text) if r.text else 0}")
            return r.text
        except Exception as e:
            if self.debug_mode:
                print(f"请求失败: {e}")
            return None

    def _post(self, url, data=None):
        try:
            if self.debug_mode:
                print(f"POST请求URL: {url}")
            r = requests.post(url, headers=self.header, data=data, timeout=20, verify=False)
            r.encoding = r.apparent_encoding or 'utf-8'
            if self.debug_mode:
                print(f"响应状态码: {r.status_code}")
            return r.text
        except Exception as e:
            if self.debug_mode:
                print(f"POST请求失败: {e}")
            return None

    def _fix_url(self, url):
        if not url:
            return ''
        if url.startswith('//'):
            return 'https:' + url
        if url.startswith('/'):
            return self.host + url
        if url.startswith('http'):
            return url
        return ''

    def _parse_text(self, elem):
        if elem is None:
            return ''
        return ''.join(elem.itertext()).strip()

    def _parse_list_item(self, a):
        try:
            href = a.get('href', '') or ''
            if self.debug_mode:
                print(f"解析列表项链接: {href}")
            
            # 提取ID，支持多种URL格式
            id_match = re.search(r'/(\d+)(?:\.html)?$', href)
            if not id_match:
                # 尝试其他可能的ID提取方式
                id_match = re.search(r'id=(\d+)', href)
                if not id_match:
                    return None
            
            vod_id = id_match.group(1)
            vod_name = a.get('title', '').strip()
            if not vod_name:
                vod_name = ''.join(a.xpath('.//h3/text()')).strip()
                if not vod_name:
                    vod_name = ''.join(a.xpath('.//div[contains(@class,"title")]//text()')).strip()
            
            if self.debug_mode:
                print(f"视频ID: {vod_id}, 标题: {vod_name}")
            
            # 提取封面图
            imgs = a.xpath('.//img')
            vod_pic = ''
            if imgs:
                for attr in ['data-original', 'data-src', 'src', 'lazy-src', 'original']:
                    vod_pic = imgs[0].get(attr, '') or ''
                    if vod_pic:
                        break
            
            # 提取备注信息
            remark = a.xpath('.//span[contains(@class,"badge") or contains(@class,"remark") or contains(@class,"tag")]/text()')
            vod_remarks = remark[0].strip() if remark else ''
            
            return {
                'vod_id': vod_id,
                'vod_name': vod_name,
                'vod_pic': self._fix_url(vod_pic),
                'vod_remarks': vod_remarks
            }
        except Exception as e:
            if self.debug_mode:
                print(f"解析列表项失败: {e}")
            return None

    def _parse_list(self, text):
        if not text:
            if self.debug_mode:
                print("文本内容为空")
            return []
        
        if self.debug_mode:
            print("开始解析列表...")
        tree = etree.HTML(text)
        res, seen = [], set()
        
        # 尝试多种可能的列表项XPath - 可根据实际网站调整
        xpath_patterns = [
            '//a[contains(@href,"/") and not(contains(@href,"javascript"))]',
            '//div[contains(@class,"item") or contains(@class,"video")]//a',
            '//div[contains(@class,"card")]//a',
            '//div[contains(@class,"thumb")]//a',
            '//div[contains(@class,"video-item")]//a',
            '//div[contains(@class,"movie-item")]//a',
            '//div[contains(@class,"post")]//a',
            '//article[contains(@class,"post")]//a'
        ]
        
        for xpath in xpath_patterns:
            items = tree.xpath(xpath)
            if self.debug_mode:
                print(f"尝试XPath: {xpath}, 找到 {len(items)} 个元素")
            
            for a in items:
                try:
                    href = a.get('href', '') or ''
                    if not href or 'javascript' in href or '#' in href or 'mailto' in href:
                        continue
                    
                    # 提取ID
                    id_match = re.search(r'/(\d+)(?:\.html)?$', href)
                    if not id_match:
                        id_match = re.search(r'id=(\d+)', href)
                        if not id_match:
                            continue
                    
                    vod_id = id_match.group(1)
                    if vod_id in seen:
                        continue
                    seen.add(vod_id)
                    
                    video = self._parse_list_item(a)
                    if video:
                        res.append(video)
                except Exception as e:
                    if self.debug_mode:
                        print(f"解析列表项失败: {e}")
                    continue
        
        if self.debug_mode:
            print(f"解析完成，共 {len(res)} 个视频")
        return res

    def homeContent(self, filter):
        if self.debug_mode:
            print("获取首页内容...")
        return {"class": self.categories, "filters": self.filters}

    def homeVideoContent(self):
        if self.debug_mode:
            print("获取首页视频列表...")
        videos = []
        try:
            # 尝试多个可能的首页URL - 可根据实际网站调整
            urls = [
                self.host + '/',
                self.host + '/latest',
                self.host + '/popular',
                self.host + '/home',
                self.host + '/index'
            ]
            
            for url in urls:
                if self.debug_mode:
                    print(f"尝试获取首页: {url}")
                html = self._get(url)
                if html:
                    videos = self._parse_list(html)
                    if videos:
                        if self.debug_mode:
                            print(f"成功获取 {len(videos)} 个视频")
                        break
            
            return {'list': videos}
        except Exception as e:
            if self.debug_mode:
                print(f"首页视频获取失败: {e}")
            return {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        if self.debug_mode:
            print(f"获取分类内容: {tid}, 页码: {pg}")
        videos = []
        try:
            if isinstance(extend, str) and extend:
                try:
                    extend = json.loads(extend)
                except Exception:
                    extend = {}
            elif not extend:
                extend = {}
            
            # 构建分类URL - 可根据实际网站调整
            url_map = {
                'latest': '/latest',
                'popular': '/popular',
                'rated': '/rated',
                'japanese': '/japanese',
                'western': '/western',
                'anime': '/anime'
            }
            
            base_path = url_map.get(tid, tid)
            url = f"{self.host}{base_path}/page/{pg}.html"
            
            if self.debug_mode:
                print(f"分类URL: {url}")
            html = self._get(url)
            if not html:
                if self.debug_mode:
                    print("获取分类页面失败")
                return {'list': [], 'page': int(pg), 'pagecount': 0, 'limit': 0, 'total': 0}
            
            videos = self._parse_list(html)
            
            # 尝试获取总页数 - 可根据实际网站调整
            tree = etree.HTML(html)
            pc = 1
            xpath_patterns = [
                '//div[contains(@class,"pagination")]//a[last()]/text()',
                '//div[contains(@class,"page")]//a[last()]/text()',
                '//div[contains(@class,"pages")]//a[last()]/text()',
                '//a[contains(@class,"last")]/text()',
                '//span[contains(@class,"total-pages")]/text()',
                '//div[contains(@class,"page-info")]/text()',
                '//ul[contains(@class,"pagination")]//li[last()]/a/text()'
            ]
            
            for xpath in xpath_patterns:
                for x in tree.xpath(xpath):
                    try:
                        page_text = x.strip()
                        if '页' in page_text:
                            pc = int(re.search(r'(\d+)', page_text).group(1))
                        else:
                            pc = int(page_text)
                        break
                    except Exception:
                        continue
                if pc > 1:
                    break
            
            if self.debug_mode:
                print(f"总页数: {pc}, 当前页: {pg}, 视频数: {len(videos)}")
            
            return {
                'list': videos,
                'page': int(pg),
                'pagecount': pc,
                'limit': len(videos),
                'total': pc * len(videos)
            }
        except Exception as e:
            if self.debug_mode:
                print(f"分类内容获取失败: {e}")
            return {'list': [], 'page': 1, 'pagecount': 0, 'limit': 0, 'total': 0}

    def detailContent(self, ids):
        if self.debug_mode:
            print(f"获取详情页: {ids}")
        try:
            vod_id = ids[0]
            url = f'{self.host}/{vod_id}.html'
            if self.debug_mode:
                print(f"详情页URL: {url}")
            html = self._get(url)
            if not html:
                if self.debug_mode:
                    print("获取详情页失败")
                return {'list': []}
            
            root = etree.HTML(html)
            
            # 提取标题 - 可根据实际网站调整
            vod_name = ''.join(root.xpath('//h1/text()')).strip()
            if not vod_name:
                title = root.xpath('//title/text()')
                if title:
                    vod_name = title[0].split('-')[0].strip()
            
            # 提取封面 - 可根据实际网站调整
            vod_pic = self._fix_url(''.join(root.xpath('//img[@class="cover"]/@src | //div[@class="cover"]//img/@src | //div[contains(@class,"poster")]//img/@src | //div[contains(@class,"thumbnail")]//img/@src')))
            
            # 提取年份 - 可根据实际网站调整
            vod_year = ''
            year_patterns = [
                '//span[contains(text(),"年")]//text()',
                '//div[contains(text(),"年份")]//text()',
                '//text()[contains(.,"20")]',
                '//span[contains(@class,"year")]//text()',
                '//div[contains(@class,"info")]//text()'
            ]
            
            for xpath in year_patterns:
                for txt in root.xpath(xpath):
                    m = re.search(r'(\d{4})', txt)
                    if m and 1900 < int(m.group(1)) < 2100:
                        vod_year = m.group(1)
                        break
                if vod_year:
                    break
            
            # 提取简介 - 可根据实际网站调整
            vod_content = ''
            content_patterns = [
                '//div[contains(@class,"desc") or contains(@class,"summary") or contains(@class,"intro")]//text()',
                '//p[contains(@class,"description")]//text()',
                '//div[contains(@class,"content")]//text()',
                '//div[contains(@class,"synopsis")]//text()',
                '//div[contains(@class,"info")]//text()'
            ]
            
            for xpath in content_patterns:
                for elem in root.xpath(xpath):
                    text = self._parse_text(elem)
                    if text and len(text) > 10:
                        vod_content = text
                        break
                if vod_content:
                    break
            
            # 提取播放地址 - 可根据实际网站调整
            vod_play_from = []
            vod_play_url = []
            
            # 尝试多种可能的播放列表格式
            play_patterns = [
                '//div[contains(@class,"playlist")]//a',
                '//div[contains(@class,"episodes")]//a',
                '//div[contains(@class,"videos")]//a',
                '//ul[contains(@class,"ep-list")]//a',
                '//div[contains(@class,"player")]//a',
                '//div[contains(@class,"episode-list")]//a',
                '//div[contains(@class,"video-list")]//a',
                '//div[contains(@class,"stream-list")]//a'
            ]
            
            for idx, pattern in enumerate(play_patterns):
                items = root.xpath(pattern)
                if items:
                    play_list = []
                    for a in items:
                        ep_name = ''.join(a.xpath('.//text()')).strip() or a.get('title', '')
                        href = a.get('href', '')
                        if not ep_name or not href:
                            continue
                        play_list.append(f'{ep_name}${self._fix_url(href)}')
                    
                    if play_list:
                        source_name = f'节点{idx + 1}' if len(play_patterns) > 1 else '播放'
                        vod_play_from.append(source_name)
                        vod_play_url.append('#'.join(play_list))
                    break
            
            if not vod_play_from:
                # 尝试直接查找播放链接
                play_items = root.xpath('//a[contains(@href,"/play/") or contains(@href,"/video/") or contains(@href,"/watch/") or contains(@href,"/stream/") or contains(@href,"/player/")]')
                play_list = []
                seen_url = set()
                
                for a in play_items:
                    ep_name = ''.join(a.xpath('.//text()')).strip() or a.get('title', '')
                    href = a.get('href', '')
                    if not ep_name or not href or href in seen_url:
                        continue
                    seen_url.add(href)
                    play_list.append(f'{ep_name}${self._fix_url(href)})
                
                if play_list:
                    vod_play_from.append('播放')
                    vod_play_url.append('#'.join(play_list))
            
            detail = {
                'vod_id': vod_id,
                'vod_name': vod_name,
                'vod_pic': vod_pic,
                'vod_year': vod_year,
                'vod_content': vod_content,
                'vod_play_from': '$$$'.join(vod_play_from) if vod_play_from else '默认',
                'vod_play_url': '$$$'.join(vod_play_url) if vod_play_url else ''
            }
            
            if self.debug_mode:
                print(f"详情页解析完成: {vod_name}")
            return {'list': [detail]}
        except Exception as e:
            if self.debug_mode:
                print(f"详情页获取失败: {e}")
            return {'list': []}

    def searchContent(self, key, quick, pg='1'):
        if self.debug_mode:
            print(f"搜索关键词: {key}, 页码: {pg}")
        videos = []
        try:
            # 尝试多个可能的搜索URL格式 - 可根据实际网站调整
            search_urls = [
                self.host + '/search/' + quote(key) + '/page/' + pg + '.html',
                self.host + '/search?q=' + quote(key) + '&page=' + pg,
                self.host + '/search/' + quote(key) + '?page=' + pg,
                self.host + '/find/' + quote(key) + '/page/' + pg + '.html',
                self.host + '/search/' + quote(key) + '/index-' + pg + '.html'
            ]
            
            for url in search_urls:
                if self.debug_mode:
                    print(f"尝试搜索URL: {url}")
                html = self._get(url)
                if html:
                    videos = self._parse_list(html)
                    if videos:
                        if self.debug_mode:
                            print(f"搜索成功，找到 {len(videos)} 个视频")
                        break
            
            return {
                'list': videos,
                'page': int(pg),
                'pagecount': 1,
                'limit': len(videos),
                'total': len(videos)
            }
        except Exception as e:
            if self.debug_mode:
                print(f"搜索失败: {e}")
            return {'list': [], 'page': 1, 'pagecount': 0, 'limit': 0, 'total': 0}

    def _extract_player_url(self, html):
        try:
            # 尝试多种可能的播放器URL提取方式 - 可根据实际网站调整
            patterns = [
                r'var\s+url\s*=\s*["\']([^"\']+)["\']',
                r'video_url\s*=\s*["\']([^"\']+)["\']',
                r'play_url\s*=\s*["\']([^"\']+)["\']',
                r'src\s*=\s*["\']([^"\']+(?:\.m3u8|\.mp4|\.flv))["\']',
                r'file\s*=\s*["\']([^"\']+(?:\.m3u8|\.mp4|\.flv))["\']',
                r'video\s*=\s*["\']([^"\']+(?:\.m3u8|\.mp4|\.flv))["\']',
                r'player\s*=\s*["\']([^"\']+(?:\.m3u8|\.mp4|\.flv))["\']',
                r'href\s*=\s*["\']([^"\']+(?:\.m3u8|\.mp4|\.flv))["\']'
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, html, re.IGNORECASE)
                if matches:
                    if self.debug_mode:
                        print(f"提取到播放URL: {matches[0]}")
                    return matches[0]
            
            return None
        except Exception as e:
            if self.debug_mode:
                print(f"提取播放URL失败: {e}")
            return None

    def playerContent(self, flag, id, vipFlags):
        if self.debug_mode:
            print(f"解析播放地址: {id}")
        try:
            url = id if id.startswith('http') else self._fix_url(id)
            if self.debug_mode:
                print(f"播放地址URL: {url}")
            html = self._get(url)
            
            if html:
                real_url = self._extract_player_url(html)
                if real_url:
                    if real_url.startswith('//'):
                        real_url = 'https:' + real_url
                    if self.isVideoFormat(real_url):
                        if self.debug_mode:
                            print(f"直接播放URL: {real_url}")
                        return {'parse': 0, 'playUrl': '', 'url': real_url, 'header': json.dumps(self.header)}
                    if self.debug_mode:
                        print(f"需要解析的URL: {real_url}")
                    return {'parse': 1, 'playUrl': '', 'url': real_url, 'header': json.dumps(self.header)}
            
            return {'parse': 1, 'playUrl': '', 'url': url, 'header': json.dumps(self.header)}
        except Exception as e:
            if self.debug_mode:
                print(f"播放地址解析失败: {e}")
            return {'parse': 0, 'playUrl': '', 'url': ''}

    def isVideoFormat(self, url):
        video_formats = ['.m3u8', '.mp4', '.flv', '.ts', '.avi', '.mkv', '.mov', '.wmv', '.webm']
        return any(url.lower().endswith(fmt) for fmt in video_formats)

    def manualVideoCheck(self):
        if self.debug_mode:
            print("手动视频检查")
        pass

    def localProxy(self, params):
        if self.debug_mode:
            print("本地代理请求")
        return None

    def destroy(self):
        if self.debug_mode:
            print("销毁爬虫实例")
        pass
