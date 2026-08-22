# -*- coding: utf-8 -*-
"""
7mmtv.sx 爬虫 (适配 影视仓/OK影视/TVBox 空壳影视APP)
站点: https://7mmtv.sx
功能: 首页 / 分类(含子分类完整筛选器) / 分页 / 详情(标题/简介/年份/演员等) / 播放地址及播放源 / 关键词搜索 / 封面图片
代理: 全局通过 ?url= 前缀代理访问目标站点
"""

import re
import json
import sys
import time
import random
import base64
import urllib.parse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# AES 解密 (pycryptodome)
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except ImportError:
    class BaseSpider:
        """requests 兜底, 脱离 TVBox 框架独立运行"""

        def fetch(self, url, headers=None, timeout=20, verify=False, cookies=None):
            s = requests.Session()
            s.trust_env = False
            return s.get(url, headers=headers, timeout=timeout, verify=verify, cookies=cookies)

        def post(self, url, headers=None, data=None, timeout=20, verify=False, cookies=None, allow_redirects=True):
            s = requests.Session()
            s.trust_env = False
            return s.post(url, headers=headers, data=data, timeout=timeout, verify=verify,
                          cookies=cookies, allow_redirects=allow_redirects)


# ============================================================
# 站点数据
# ============================================================

# 父分类
TYPES = [
    ('censored',         '有碼AV'),
    ('uncensored',       '無碼AV'),
    ('reducing-mosaic',  '無碼破解'),
    ('chinese',          '中字AV'),
    ('amateurjav',       '素人AV'),
    ('amateur',          '國產影片'),
    ('klive',            '韓國直播'),
]

# 有碼AV 子分类 (216) — censored / chinese / reducing-mosaic 共用
CENSORED_CATS = [
    ('1', '企畫'), ('2', '女同性戀'), ('3', '獵豔'), ('4', '野外・露出'),
    ('5', '偶像藝人'), ('6', '其他戀物癖'), ('7', '近親相姦'), ('8', '戀乳癖'),
    ('9', '情侶'), ('10', '男同性恋'), ('11', '花癡'), ('12', '偷窥'),
    ('13', '戀腿癖'), ('14', '其他'), ('15', '性騷擾'), ('16', '倒追'),
    ('17', '性奴'), ('18', '跳舞'), ('19', '雙性人'), ('20', '姐妹'),
    ('21', '通姦'), ('22', '粗暴'), ('23', '學校作品'), ('24', '惡作劇'),
    ('25', '妄想'), ('26', '殘忍畫面'), ('27', '爛醉如泥的'), ('28', '處女'),
    ('29', '美容院'), ('30', '性感的'), ('31', '女同接吻'), ('32', '運動'),
    ('33', '瘙癢'), ('34', '出軌'), ('35', '正太控'), ('36', '處男'),
    ('37', '蠻橫嬌羞'), ('38', '觸手'), ('39', '嘔吐'), ('40', '折磨'),
    ('42', '女優ベスト・総集編'), ('43', '温泉'), ('44', 'M男'),
    ('45', '原作コラボ'), ('46', '16時間以上作品'), ('47', 'デカチン・巨根'),
    ('48', 'ファン感謝・訪問'), ('50', '巨尻'), ('51', 'ハーレム'),
    ('52', '日焼け'), ('53', '早漏'), ('54', 'キス・接吻'), ('55', '汗だく'),
    ('56', '服務生'), ('57', '高中女生'), ('58', '女主播'), ('59', '女生'),
    ('60', '黑人演員'), ('61', '蕩婦'), ('62', '護士'), ('63', '家教'),
    ('64', '母親'), ('65', '女教師'), ('66', '展場女孩'), ('67', '女大學生'),
    ('68', '賽車女郎'), ('69', '妓女'), ('70', '各種職業'), ('71', '女醫生'),
    ('72', '已婚婦女'), ('73', '白人'), ('74', '千金小姐'), ('75', '寡婦'),
    ('76', '車掌小姐'), ('77', '格鬥家'), ('78', '姐姐'), ('79', '新娘、嫩妻'),
    ('80', '美少女'), ('81', '秘書'), ('82', '模特兒'), ('83', '女主人、女老板'),
    ('84', '明星臉'), ('85', '女檢察官'), ('86', '格鬥家'), ('87', '義母'),
    ('88', '講師'), ('89', '伴侶'), ('90', '亞洲女演員'), ('91', '公主'),
    ('92', '童年朋友'), ('93', '黑幫成員'), ('94', '角色扮演'), ('95', '眼鏡'),
    ('96', '泳裝'), ('97', '校服'), ('98', '迷你裙'), ('99', '女樸'),
    ('100', '旗袍'), ('101', '學校泳裝'), ('102', '水手服'), ('103', '內衣'),
    ('104', '體育服'), ('105', '和服・喪服'), ('106', 'OL'),
    ('107', '身體意識'), ('108', '連褲襪'), ('109', '空中小姐'),
    ('110', '迷你裙警察'), ('111', '裸體圍裙'), ('112', '緊身衣'),
    ('113', '制服'), ('114', '泡泡襪'), ('115', '女祭司'), ('116', '貓耳女'),
    ('117', '兔女郎'), ('118', '猥褻穿著'), ('119', '內衣'),
    ('120', '女裝人妖'), ('121', '女忍者'), ('122', '娃娃'),
    ('123', '女僕'), ('124', '巫女'), ('125', '架空の人物'),
    ('127', '巨乳'), ('128', '貧乳・微乳'), ('129', '美腳'),
    ('130', '熟女'), ('131', '苗條'), ('132', '童顔'), ('133', '母乳'),
    ('134', '無毛'), ('135', '腋毛'), ('136', '爆乳'), ('137', '口内発射'),
    ('138', '中出'), ('139', '自慰'), ('140', '顏射'), ('141', '潮吹'),
    ('142', '亂交'), ('143', '深喉'), ('144', '顏面騎乘'), ('145', '足交'),
    ('146', '乳交'), ('147', '肛交'), ('148', '放尿・失禁'),
    ('149', '羞恥・辱め'), ('150', '拘束'), ('151', '按摩棒'),
    ('152', 'SM'), ('153', '立即口交'), ('154', '多P'), ('155', '輪姦'),
    ('156', '乳液・油'), ('157', '藥物・壯陽藥'), ('158', '飲尿・放尿'),
    ('159', '食糞'), ('160', '食精'), ('161', '婦人科'),
    ('162', '嗅覺'), ('163', '獣姦'), ('164', 'スカトロ'),
    ('165', '排泄物'), ('166', 'ロリ系'), ('167', ' glaring'),
    ('168', '美尻'), ('169', 'ぽっちゃり'), ('170', '脇'),
    ('171', '脇毛'), ('172', 'おなら'), ('173', '鼻フック'),
    ('174', 'くすぐり'), ('175', '約束'), ('176', 'ごっくん'),
    ('177', 'パンチラ'), ('178', 'おっぱい'),
    ('179', '勃起'), ('180', '勃起乳首'), ('181', '局部'),
    ('182', '乳首責め'), ('183', '陰核責め'),
    ('184', '強制'), ('185', 'オナニー'),
    ('186', 'カーセックス'), ('187', '車内'),
    ('188', '野外'), ('189', '公衆トイレ'),
    ('190', '混浴'), ('191', '温泉'), ('192', '露天風呂'),
    ('193', '健康ランド'), ('194', 'リゾート'),
    ('195', '海'), ('196', 'プール'),
    ('197', '島'), ('198', '山'), ('199', '森'),
    ('200', 'キャンプ場'), ('201', '公園'),
    ('202', '学校'), ('203', '教室'),
    ('204', '体育館'), ('205', 'プール更衣室'),
    ('206', '保健室'), ('207', '図書室'),
    ('208', '廊下'), ('209', '屋上'),
    ('210', 'トイレ'), ('211', '階段'),
    ('212', '職場'), ('213', '会議室'),
    ('214', '社長室'), ('215', '店'),
    ('216', '倉庫'), ('217', '駐車場'),
    ('218', 'エレベーター'), ('219', '階段'),
    ('220', 'ベッド'), ('221', 'ソファ'),
    ('222', '風呂'), ('223', 'シャワー'),
    ('224', 'トイレ'), ('225', 'キッチン'),
    ('226', 'バルコニー'), ('227', '庭'),
    ('228', 'プール'), ('229', '海'),
    ('230', '山'), ('231', '森'),
    ('232', 'キャンプ場'), ('233', '公園'),
]

# 素人AV 子分类 (117) — amateurjav 专用
AMATEURJAV_CATS = [
    ('3', '花癡'), ('4', '獵豔、搭訕'), ('19', '美容按摩'), ('21', '情侶'),
    ('55', '風俗'), ('24', '企畫'), ('27', '偷窥'), ('32', '通姦'),
    ('36', '女同性戀'), ('43', '處男'), ('48', '近親相姦'), ('58', '汗だく'),
    ('80', '奴隷'), ('81', 'デカチン・巨根'), ('86', '野外・露出'),
    ('102', '鬼畜'), ('115', '巨尻'), ('121', '倒追'), ('127', '運動'),
    ('128', '温泉'), ('130', '姐妹'), ('133', '處女'),
    ('143', '爛醉如泥的'), ('160', '偶像藝人'), ('169', '出軌'),
    ('187', '日焼け'), ('73', '金髪・ブロンド'), ('147', '淫亂'),
    ('5', '人妻'), ('6', '女生'), ('13', '高中女生'), ('23', '痴女'),
    ('29', '女大學生'), ('31', '新娘、嫩妻'), ('35', '姐姐'),
    ('39', '各種職業'), ('46', '妓女'), ('53', '美少女'),
    ('59', '女教師'), ('75', '護士'), ('76', '千金小姐'),
    ('175', '模特兒'), ('183', '女主播'), ('186', '教練'),
    ('195', '伴侶'), ('93', '空姐・CA'), ('9', '角色扮演'),
    ('68', '和服・浴衣'), ('26', 'OL'), ('60', '制服'),
    ('65', '迷你裙'), ('69', '女僕'), ('70', '內衣'),
    ('101', '泳裝'), ('113', '及膝襪'), ('120', '眼鏡'),
    ('126', '學校泳裝'), ('8', '乳房'), ('11', '苗條'), ('17', '熟女'),
    ('33', '巨乳'), ('85', '爆乳'), ('54', '屁股'),
    ('89', '無毛'), ('90', '貧乳・微乳'), ('119', '高'),
    ('129', '胖女人'), ('49', '童顔'), ('231', 'D杯'),
    ('226', 'E杯'), ('229', 'F杯'), ('230', 'G杯'), ('222', 'H杯'),
    ('125', '口内発射'), ('15', '中出'), ('16', '自慰'),
    ('18', '打手槍'), ('25', '顏射'), ('34', '潮吹'), ('40', '乳交'),
    ('57', '亂交'), ('62', '深喉'), ('79', '肛交'), ('88', '顏射'),
    ('95', '顏面騎乘'), ('104', '足交'), ('161', '母乳'),
    ('181', '吞精'), ('37', '放尿・失禁'), ('42', '羞恥・辱め'),
    ('7', '女優按摩棒'), ('14', '拘束'), ('38', '按摩棒'),
    ('50', 'SM'), ('51', '車震'), ('64', '立即口交'),
    ('108', '多P'), ('123', '輪姦'), ('56', '乳液・油'),
    ('72', '藥物・壯陽藥'), ('1', '素人'), ('106', '配信専用素人'),
    ('228', '短髮'), ('145', '清楚'), ('66', '美腳'), ('67', '美尻'),
    ('103', '手マン'), ('78', '初撮り'), ('2', '纪录片'),
    ('61', '高清(HD)'), ('10', '第一人稱攝影'), ('44', '單體作品'),
    ('52', '主觀視角'), ('74', '國外進口'), ('132', '投稿'),
    ('216', '獨家'), ('246', '新登場メーカー'),
]

# 無碼AV 厂商 (13) — uncensored 专用
UNCENSORED_MAKERS = [
    ('37', 'FC2'), ('17', 'HEYZO'), ('29', '東京熱'),
    ('32', '一本道'), ('30', 'カリビアンコム'),
    ('40', 'カリビアンコムPPV'), ('31', '天然むすめ'),
    ('36', 'パコパコママ'), ('35', 'ガチん娘！'),
    ('34', 'エッチな4610'), ('38', '人妻斬り0930'),
    ('39', 'エッチな0930'), ('126', 'XXX-AV'),
]

# 素人AV 厂商 (6) — amateurjav 专用
AMATEURJAV_MAKERS = [
    ('1752', 'シロウトTV(SIRO)'), ('1586', 'ラグジュTV(LUXU)'),
    ('1751', 'ナンパTV(200GANA)'), ('1318', 'PRESTIGE PREMIUM(300MAAN)'),
    ('1069', 'S-CUTE'), ('1585', 'ARA'),
]

# 搜索类型枚举
SEARCH_TYPES = [
    ('searchall', '全部'), ('censored', '有碼AV'),
    ('amateurjav', '素人AV'), ('chinese', '中字AV'),
    ('uncensored', '無碼AV'), ('reducing-mosaic', '無碼破解'),
    ('amateur', '國產影片'), ('clive', '中國直播'),
    ('klive', '韓國直播'), ('hcomic', '成人漫畫'),
]


class Spider(BaseSpider):
    """7mmtv.sx 爬虫 — 适配 影视仓/OK影视/TVBox"""

    # ====== 配置 ======
    Host = 'https://7mmtv.sx'
    Lang = 'zh'
    UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')

    # ====== TVBox 基础接口 ======

    def init(self, extend=''):
        """初始化"""
        return

    def getName(self):
        return '7mmtv'

    def isVideoFormat(self, url):
        url = str(url or '').lower()
        for ext in ('.m3u8', '.mp4', '.flv', '.avi', '.mkv', '.wmv', '.mov', '.ts'):
            if ext in url:
                return True
        return False

    def manualVideoCheck(self):
        return False

    def destroy(self):
        return

    # ====== HTTP 层 ======

    def _wrap_proxy(self, url):
        """直连模式: 原样返回 URL"""
        return url or ''

    def _unwrap_url(self, url):
        """直连模式: 原样返回 URL"""
        return url or ''

    def _full_url(self, path):
        """拼接站点完整 URL"""
        if not path:
            return ''
        if path.startswith('http'):
            return path
        if path.startswith('//'):
            return 'https:' + path
        if not path.startswith('/'):
            path = '/' + path
        return f'{self.Host}/{self.Lang}{path}'

    def _get_headers(self, referer=None):
        h = {
            'User-Agent': self.UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        if referer:
            h['Referer'] = self._wrap_proxy(referer)
        return h

    def _fetch(self, target_url, referer=None, retries=3, timeout=25):
        """直连 GET 请求"""
        for attempt in range(retries):
            try:
                if attempt > 0:
                    time.sleep(random.uniform(0.5, 1.5))
                r = self.fetch(target_url, headers=self._get_headers(referer),
                               timeout=timeout, verify=False)
                if r.status_code == 200:
                    if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'latin-1'):
                        r.encoding = r.apparent_encoding or 'utf-8'
                    return r.text or ''
                print(f'[7mmtv] GET {r.status_code}: {target_url}')
                return ''
            except Exception as e:
                print(f'[7mmtv] GET 异常 [{attempt+1}/{retries}] {target_url}: {e}')
                continue
        return ''

    def _post(self, target_url, data, referer=None, retries=3, timeout=25):
        """直连 POST 请求"""
        for attempt in range(retries):
            try:
                if attempt > 0:
                    time.sleep(random.uniform(0.5, 1.5))
                headers = self._get_headers(referer)
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                r = self.post(target_url, headers=headers, data=data,
                              timeout=timeout, verify=False)
                if r.status_code == 200:
                    if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'latin-1'):
                        r.encoding = r.apparent_encoding or 'utf-8'
                    return r.text or ''
                print(f'[7mmtv] POST {r.status_code}: {target_url}')
                return ''
            except Exception as e:
                print(f'[7mmtv] POST 异常 [{attempt+1}/{retries}] {target_url}: {e}')
                continue
        return ''

    @staticmethod
    def _clean_text(text):
        """清理 HTML 标签与空白"""
        if not text:
            return ''
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&quot;', '"', text)
        text = re.sub(r'&#39;', "'", text)
        return text.strip()

    # ====== 分类筛选器构建 ======

    def _build_filters(self):
        """构建 TVBox 筛选器: {type_id: [{key, name, value:[{n, v}]}]}"""
        filters = {}

        # 有碼AV / 中字AV — 子分类 (reducing-mosaic 不支持 _category URL, 仅列表浏览)
        for t in ('censored', 'chinese'):
            cats = [{'n': name, 'v': cid} for cid, name in CENSORED_CATS]
            filters[t] = [{'key': 'cat', 'name': '分类', 'value': [{'n': '全部', 'v': ''}] + cats}]

        # 無碼AV — 厂商
        makers = [{'n': name, 'v': 'm' + mid} for mid, name in UNCENSORED_MAKERS]
        filters['uncensored'] = [{'key': 'cat', 'name': '厂商', 'value': [{'n': '全部', 'v': ''}] + makers}]

        # 素人AV — 子分类 + 厂商
        cats = [{'n': name, 'v': cid} for cid, name in AMATEURJAV_CATS]
        mk = [{'n': name, 'v': 'm' + mid} for mid, name in AMATEURJAV_MAKERS]
        filters['amateurjav'] = [
            {'key': 'cat', 'name': '分类', 'value': [{'n': '全部', 'v': ''}] + cats},
            {'key': 'maker', 'name': '厂商', 'value': [{'n': '全部', 'v': ''}] + mk},
        ]

        return filters

    def _lookup_cat_name(self, cat_id):
        """通过 cat_id 查找分类名"""
        for cid, name in CENSORED_CATS + AMATEURJAV_CATS:
            if cid == cat_id:
                return name
        return ''

    def _lookup_maker_name(self, maker_id):
        """通过 maker_id 查找厂商名"""
        for mid, name in UNCENSORED_MAKERS + AMATEURJAV_MAKERS:
            if mid == maker_id:
                return name
        return ''

    # ====== 列表解析 ======

    def _parse_video_list(self, html):
        """解析视频卡片列表, 返回 TVBox vod 列表"""
        items = []
        seen = set()
        if not html:
            return items

        # 匹配所有包含 _content/ 的 <a> 标签
        a_pattern = r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
        matches = re.findall(a_pattern, html, re.DOTALL)

        for href, inner in matches:
            original = self._unwrap_url(href)
            # 解析: /{type}_content/{id}/{fanhao}.html  (type 可含连字符, 如 reducing-mosaic)
            m = re.search(r'/([\w-]+)_content/(\d+)/([^/]+)\.html', original)
            if not m:
                continue
            vod_type, content_id, fanhao = m.group(1), m.group(2), m.group(3)

            if content_id in seen:
                continue
            seen.add(content_id)

            # 标题: 优先 img alt, 其次文本
            title = ''
            alt_m = re.search(r"alt=['\"]([^'\"]*)['\"]", inner)
            if alt_m:
                title = alt_m.group(1).strip()
            if not title:
                title = self._clean_text(inner)
            if not title:
                title = fanhao

            # 封面: 优先 data-src, 其次 data-original, 最后 src
            cover = ''
            src_m = re.search(r"(?:data-src|data-original|src)=['\"]([^'\"]*)['\"]", inner)
            if src_m:
                cover = src_m.group(1).strip()
                if cover and not cover.startswith('http'):
                    cover = 'https:' + cover if cover.startswith('//') else cover

            vod_id = f'{vod_type}|{content_id}|{fanhao}'

            items.append({
                'vod_id': vod_id,
                'vod_name': title[:200],
                'vod_pic': cover,
                'vod_remarks': '',
            })

        return items

    def _parse_total_pages(self, html, current_page=1):
        """从分页区域解析总页数"""
        total = int(current_page) if current_page else 1
        if not html:
            return total
        try:
            # 分页在 <nav class='pagination-row'> 内, 闭合标签是 </nav>
            page_section = re.search(r"class=['\"]pagination-row['\"].*?</nav>", html, re.DOTALL)
            if not page_section:
                page_section = re.search(r"class=['\"]pagination-row['\"].*?</ul>\s*</div>", html, re.DOTALL)
            if not page_section:
                page_section = re.search(r"class=['\"]pagination['\"].*?</ul>", html, re.DOTALL)
            if page_section:
                text = page_section.group(0)
                nums = re.findall(r'>(\d+)</a>', text)
                if nums:
                    total = max(total, max(int(n) for n in nums))
                # 也查找末页链接中的页码
                last_m = re.search(r'href=[^>]*?(\d{2,5})\.html["\'][^>]*>[^<]*?(?:最後|末页|尾页|Last)', text, re.IGNORECASE)
                if last_m:
                    total = max(total, int(last_m.group(1)))
        except Exception:
            pass
        return max(total, 1)

    # ====== 首页 ======

    def homeContent(self, filter=1):
        """首页: 返回分类列表 + 筛选器 + 推荐视频"""
        classes = [{'type_id': tid, 'type_name': tname, 'type_flag': '1'}
                   for tid, tname in TYPES]
        filters = self._build_filters() if filter else {}

        # 首页推荐: 抓取有碼AV 第一页
        url = f'{self.Host}/{self.Lang}/censored_list/all/1.html'
        html = self._fetch(url, referer=self.Host)
        items = self._parse_video_list(html) if html else []

        return {
            'class': classes,
            'filters': filters,
            'list': items[:30],
        }

    def homeVideoContent(self):
        """首页推荐视频 (TVBox 首屏)"""
        url = f'{self.Host}/{self.Lang}/censored_list/all/1.html'
        html = self._fetch(url, referer=self.Host)
        items = self._parse_video_list(html) if html else []
        return {'list': items[:30]}

    # ====== 分类列表 ======

    def categoryContent(self, tid, pg, filter=False, extend=''):
        """分类列表: 支持分页 + 子分类筛选"""
        page = int(pg) if pg and str(pg).isdigit() else 1
        extend = extend or {}
        if isinstance(extend, str):
            try:
                extend = json.loads(extend)
            except (json.JSONDecodeError, TypeError):
                extend = {}

        cat_val = extend.get('cat', '') if isinstance(extend, dict) else ''
        maker_val = extend.get('maker', '') if isinstance(extend, dict) else ''

        # 优先使用 maker 筛选
        if maker_val and maker_val.startswith('m'):
            maker_id = maker_val[1:]
            maker_name = self._lookup_maker_name(maker_id)
            if maker_name:
                path = f'/{tid}_makersr/{maker_id}/{urllib.parse.quote(maker_name, safe="")}/{page}.html'
            else:
                path = f'/{tid}_makersr/{maker_id}/{page}.html'
        elif cat_val and cat_val.startswith('m'):
            maker_id = cat_val[1:]
            maker_name = self._lookup_maker_name(maker_id)
            if maker_name:
                path = f'/{tid}_makersr/{maker_id}/{urllib.parse.quote(maker_name, safe="")}/{page}.html'
            else:
                path = f'/{tid}_makersr/{maker_id}/{page}.html'
        elif cat_val:
            cat_name = self._lookup_cat_name(cat_val)
            if cat_name:
                path = f'/{tid}_category/{cat_val}/{urllib.parse.quote(cat_name, safe="")}/{page}.html'
            else:
                path = f'/{tid}_category/{cat_val}/{page}.html'
        else:
            path = f'/{tid}_list/all/{page}.html'

        target_url = f'{self.Host}/{self.Lang}{path}'
        html = self._fetch(target_url, referer=self.Host)
        items = self._parse_video_list(html) if html else []
        total_pages = self._parse_total_pages(html, page)

        return {
            'list': items,
            'page': page,
            'pagecount': max(total_pages, page),
            'limit': len(items) if items else 20,
            'total': max(total_pages, page) * 20,
            'parse': 0,
            'jx': 0,
        }

    # ====== mvarr 播放源解码 ======

    # 播放源键名 → 线路名映射
    _SOURCE_NAMES = {
        '40_1': 'SP',
        '38_1': 'VH',
        '42_1': 'TV',
        '37_1': 'SW',
    }

    @staticmethod
    def _split_str(encoded, base_val, xor_val):
        """
        JavaScript hsdfdg252 函数的 Python 移植:
        1. separator = chr(base_val + 97)
        2. 按 separator 分割编码字符串
        3. 每段按 base_val 进制解析为整数
        4. 与 xor_val 异或
        5. 转为字符并拼接 → Base64 密文
        """
        b = base_val if base_val <= 25 else base_val % 25
        separator = chr(b + 97)
        parts = encoded.split(separator)
        result = []
        for part in parts:
            if not part:
                continue
            try:
                num = int(part, b)
            except ValueError:
                continue
            num ^= xor_val
            result.append(chr(num))
        return ''.join(result)

    @staticmethod
    def _aes_decrypt(ciphertext_b64, key_str, iv_str):
        """
        AES-CBC/Pkcs7 解密 (对应 CryptoJS.AES.decrypt)
        key = Utf8 bytes, iv = Utf8 bytes
        """
        if not _HAS_CRYPTO:
            return ''
        try:
            key_bytes = key_str.encode('utf-8')
            iv_bytes = iv_str.encode('utf-8')
            ciphertext = base64.b64decode(ciphertext_b64)
            cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
            decrypted = cipher.decrypt(ciphertext)
            decrypted = unpad(decrypted, AES.block_size)
            return decrypted.decode('utf-8', errors='replace')
        except Exception as e:
            print(f'[7mmtv] AES decrypt failed: {e}')
            return ''

    @staticmethod
    def _extract_page_vars(html):
        """从详情页 HTML 提取加密变量 (每次加载动态变化)"""
        vars_dict = {}
        for name in ['hcdeedg252', 'hadeedg252']:
            m = re.search(name + r'\s*=\s*(\d+)', html)
            if m:
                vars_dict[name] = int(m.group(1))
        for name in ['hdddedd252', 'argdeqweqweqwe', 'hdddedg252']:
            m = re.search(r"var\s+" + name + r"\s*=\s*'([^']+)'", html)
            if m:
                vars_dict[name] = m.group(1)
        return vars_dict

    @staticmethod
    def _extract_mvarr(html):
        """从详情页 HTML 提取 mvarr 播放源数组"""
        results = {}
        for m in re.finditer(r"mvarr\['(\d+_\d+)'\]\s*=\s*\[\[(.*?)\],?\];", html, re.DOTALL):
            key = m.group(1)
            raw = m.group(2)
            parts = re.findall(r"'([^']*)'", raw)
            if len(parts) >= 5:
                results[key] = {
                    'iframe_id': parts[0],
                    'encoded': parts[1],
                    'iframe_html': parts[2],
                    'base_url': parts[3],
                    'extra': parts[4] if len(parts) > 4 else '',
                }
        return results

    def _decode_mvarr_sources(self, html):
        """
        解码 mvarr 播放源, 返回 [(source_name, play_url), ...]
        每组播放源: splitStr 解码 → AES-CBC 解密 → 拼接最终 URL
        """
        if not _HAS_CRYPTO:
            return []

        pv = self._extract_page_vars(html)
        mvarr = self._extract_mvarr(html)

        if not mvarr or not pv:
            return []

        key_str = pv.get('argdeqweqweqwe', pv.get('hdddedd252', ''))
        iv_str = pv.get('hdddedg252', '')
        hcdeedg252 = pv.get('hcdeedg252', 13)
        hadeedg252 = pv.get('hadeedg252', 22)

        if not key_str or not iv_str:
            return []

        sources = []
        for key in ['40_1', '38_1', '42_1', '37_1']:
            entry = mvarr.get(key)
            if not entry:
                continue

            ciphertext_b64 = self._split_str(entry['encoded'], hcdeedg252, hadeedg252)
            decrypted = self._aes_decrypt(ciphertext_b64, key_str, iv_str)

            if not decrypted:
                continue

            base_url = entry['base_url']
            extra = entry.get('extra', '')

            # emturbovid 源: 直接使用解密后的完整 URL
            if base_url == "https://emturbovid.com/t/":
                final_url = decrypted
            else:
                final_url = base_url + decrypted + extra

            final_url = final_url.strip()
            name = self._SOURCE_NAMES.get(key, key)
            sources.append((name, final_url))

        return sources

    # ====== 详情 ======

    def detailContent(self, ids):
        """解析影片详情页: 标题/简介/年份/演员/播放地址"""
        vod_id = str(ids[0] if isinstance(ids, list) else ids)
        parts = vod_id.split('|')

        if len(parts) >= 3:
            vod_type, content_id, fanhao = parts[0], parts[1], parts[2]
            detail_url = f'{self.Host}/{self.Lang}/{vod_type}_content/{content_id}/{fanhao}.html'
        else:
            detail_url = f'{self.Host}/{self.Lang}/{vod_id}'

        html = self._fetch(detail_url, referer=self.Host)
        if not html:
            return {'list': [{'vod_id': vod_id, 'vod_name': vod_id}], 'parse': 0, 'jx': 0}

        detail = self._parse_detail(vod_id, html, detail_url)
        return {'list': [detail], 'parse': 0, 'jx': 0}

    def _parse_detail(self, vod_id, html, detail_url):
        """从详情页 HTML 解析影片信息, 优先使用 JSON-LD 结构化数据"""
        vod_name = ''
        vod_pic = ''
        vod_content = ''
        vod_year = ''
        vod_actor = ''
        vod_director = ''
        vod_area = ''
        type_name = ''
        vod_remarks = ''
        play_from = ''
        play_url = ''
        content_url = ''

        # ---- 优先: JSON-LD 结构化数据 ----
        ld_match = re.search(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL)
        if ld_match:
            try:
                data = json.loads(ld_match.group(1).strip())

                vod_name = data.get('name', '')
                vod_pic = data.get('image', '') or data.get('thumbnailUrl', '')
                vod_content = data.get('description', '')
                vod_content = self._clean_text(vod_content)

                # 年份
                upload_date = data.get('uploadDate', '')
                if upload_date:
                    vod_year = str(upload_date)[:4]

                # 演员
                actors = data.get('actor', [])
                if isinstance(actors, list):
                    actor_names = [a.get('name', '') for a in actors if isinstance(a, dict)]
                elif isinstance(actors, dict):
                    actor_names = [actors.get('name', '')]
                else:
                    actor_names = []
                vod_actor = ', '.join(n for n in actor_names if n)

                # 导演
                director = data.get('director', {})
                if isinstance(director, dict):
                    vod_director = director.get('name', '')

                # 发行商
                studio = data.get('productionCompany', {})
                if isinstance(studio, dict):
                    vod_area = studio.get('name', '')

                # 分类标签
                genres = data.get('genre', [])
                if isinstance(genres, list):
                    type_name = ', '.join(genres)
                else:
                    type_name = str(genres)

                # 播放地址 — 保留 contentUrl 作为兜底 (mvarr 解码失败时使用)
                content_url = data.get('contentUrl', '')
                if not content_url:
                    video_data = data.get('video', [])
                    if isinstance(video_data, list) and video_data:
                        content_url = video_data[0].get('contentUrl', '') if isinstance(video_data[0], dict) else ''

                # 番号
                identifier = data.get('identifier', '')

            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f'[7mmtv] JSON-LD 解析失败: {e}')

        # ---- 补充: 从 HTML 元素提取额外信息 ----

        # 标题兜底
        if not vod_name:
            h_m = re.search(r'<h1[^>]*class=["\'][^"\']*fullvideo-details[^"\']*["\'][^>]*>(.*?)</h1>', html, re.DOTALL)
            if h_m:
                vod_name = self._clean_text(h_m.group(1))
            if not vod_name:
                title_m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
                if title_m:
                    vod_name = self._clean_text(title_m.group(1)).split('|')[0].strip()

        # 时长
        if not vod_remarks:
            dur_m = re.search(r"class=['\"]text-muted me-3['\"]>(\d+)分", html)
            if dur_m:
                vod_remarks = f'{dur_m.group(1)}分钟'

        # 日期兜底
        if not vod_year:
            date_m = re.search(r"class=['\"]text-muted me-3['\"]>(\d{4}-\d{2}-\d{2})", html)
            if date_m:
                vod_year = date_m.group(1)[:4]

        # 封面兜底
        if not vod_pic:
            img_m = re.search(r'<img[^>]*class=["\'][^"\']*img-fluid[^"\']*["\'][^>]*(?:data-src|src)=["\']([^"\']+)["\']', html)
            if not img_m:
                img_m = re.search(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html)
            if img_m:
                vod_pic = img_m.group(1).strip()

        # 简介兜底
        if not vod_content:
            desc_m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html)
            if desc_m:
                vod_content = self._clean_text(desc_m.group(1))

        # 演员兜底 — 从详情区域提取
        if not vod_actor:
            # 查找 "女優" 区域
            actor_m = re.search(r'女優[^<]*</[^>]+>\s*<[^>]*>(.*?)</', html, re.DOTALL)
            if actor_m:
                vod_actor = self._clean_text(actor_m.group(1))

        # 分类标签兜底
        if not type_name:
            cat_m = re.search(r'影片類別[^<]*</[^>]+>\s*<[^>]*>(.*?)</', html, re.DOTALL)
            if cat_m:
                type_name = self._clean_text(cat_m.group(1))

        # ---- 播放地址: 从 mvarr 提取全部播放源 (SP/VH/TV/SW 四线路) ----
        mvarr_sources = self._decode_mvarr_sources(html)
        if mvarr_sources:
            play_from = '$$$'.join(s[0] for s in mvarr_sources)
            play_url = '$$$'.join(f'正片${s[1]}' for s in mvarr_sources)
        elif content_url:
            # 兜底: mvarr 解码失败时使用 JSON-LD contentUrl
            play_from = 'SP'
            play_url = f'正片${content_url}'

        detail = {
            'vod_id': vod_id,
            'vod_name': vod_name or vod_id,
            'vod_pic': vod_pic,
            'type_name': type_name,
            'vod_year': vod_year,
            'vod_area': vod_area,
            'vod_actor': vod_actor,
            'vod_director': vod_director,
            'vod_content': vod_content,
            'vod_remarks': vod_remarks,
            'vod_play_from': play_from,
            'vod_play_url': play_url,
        }
        return detail

    # ====== 播放 ======

    @staticmethod
    def _unpack_eval(html):
        """解码 eval 打包的 JS, 返回解包后的代码"""
        m = re.search(
            r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.+?)',(\d+),(\d+),'(.+?)'\.split\('\|'\)\)\)",
            html, re.DOTALL)
        if not m:
            return ''

        packed_code = m.group(1)
        base = int(m.group(2))
        keys = m.group(4).split('|')

        def _int_to_base(n, b):
            if n == 0:
                return '0'
            digits = '0123456789abcdefghijklmnopqrstuvwxyz'
            r = ''
            while n > 0:
                r = digits[n % b] + r
                n //= b
            return r

        result = packed_code
        for i in range(len(keys) - 1, -1, -1):
            if keys[i]:
                token = _int_to_base(i, base)
                result = re.sub(r'\b' + re.escape(token) + r'\b', keys[i], result)
        return result

    def _extract_m3u8_from_embed(self, embed_url):
        """从嵌入页 (mmvh02.com 等) 提取 m3u8 地址"""
        html = self._fetch(embed_url, referer=self.Host)
        if not html:
            return ''

        # 1. 尝试从 eval 打包的 JS 中提取
        unpacked = self._unpack_eval(html)
        if unpacked:
            m3u8_urls = re.findall(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', unpacked)
            if m3u8_urls:
                return m3u8_urls[0]

        # 2. 尝试直接从 HTML 中查找 m3u8
        m3u8_urls = re.findall(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', html)
        if m3u8_urls:
            return m3u8_urls[0]

        # 3. 尝试从 sources/file 变量中提取
        src_m = re.search(r'["\'](?:file|src)["\']\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', html)
        if src_m:
            return src_m.group(1)

        return ''

    def _extract_embed_from_wrapper(self, wrapper_url, depth=0):
        """
        从 7mmtv.sx 加密包装页提取内嵌视频地址 (emturbovid 等)
        包装页可能多层嵌套: iframeencrypteda → iframeencryptedb → 播放器页
        """
        if depth > 3:
            return ''

        html = self._fetch(wrapper_url, referer=self.Host)
        if not html:
            return ''

        # 0. 优先: 页面本身可能就是播放器页, 直接提取 m3u8
        # 检查 data-hash 属性
        hash_m = re.search(r'data-hash=["\']([^"\']+\.m3u8[^"\']*)["\']', html)
        if hash_m:
            return hash_m.group(1)
        # 检查 urlPlay / file / src 变量
        for var_name in ['urlPlay', 'file', 'src', 'source']:
            var_m = re.search(rf"""var\s+{var_name}\s*=\s*["']([^"']+\.m3u8[^"']*)["']""", html)
            if var_m:
                return var_m.group(1)
        # 通用 m3u8 搜索
        m3u8_urls = re.findall(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', html)
        if m3u8_urls:
            return m3u8_urls[0]

        # 1. 查找 iframe src
        iframe_m = re.search(r"""<iframe[^>]*src=["']([^"']+)["']""", html)
        if iframe_m:
            src = iframe_m.group(1).strip()
            # 解包代理 URL
            src = self._unwrap_url(src)
            if src.startswith('//'):
                src = 'https:' + src

            # 如果是 7mmtv.sx 的另一层加密页 → 递归跟进
            if 'iframeencrypted' in src and '7mmtv.sx' in src:
                return self._extract_embed_from_wrapper(src, depth + 1)

            # 如果是视频主机地址 → 直接返回
            if any(d in src for d in ('emturbovid', 'turbovi', 'mmvh', 'mmsi', 'vidhide')):
                return src

            # 其他 iframe → 返回
            return src

        # 2. 查找 JS 重定向 (location.href / window.location)
        loc_m = re.search(r'(?:location\.href|window\.location)\s*=\s*["\']([^"\']+)["\']', html)
        if loc_m:
            url = self._unwrap_url(loc_m.group(1).strip())
            if url.startswith('//'):
                url = 'https:' + url
            if 'iframeencrypted' in url and '7mmtv.sx' in url:
                return self._extract_embed_from_wrapper(url, depth + 1)
            return url

        # 3. 查找视频主机地址 (排除 /sandbox 等非视频页面)
        for pattern in [
            r"""["']([^"']*(?:emturbovid|turbovi)[^"']*/t/[^"']+)["']""",
            r"""["']([^"']*(?:mmvh|mmsi|vidhide)[^"']*/[ve]/[^"']+)["']""",
        ]:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                url = m.group(1).strip()
                if url.startswith('//'):
                    url = 'https:' + url
                return url

        return ''

    def playerContent(self, flag, id, vipFlags=None):
        """返回播放地址及 header, 按播放源域名分流处理"""
        play_url = str(id or '')
        if not play_url:
            return {'parse': 0, 'playUrl': '', 'url': '', 'header': '', 'jx': 0}

        # 协议补全
        if play_url.startswith('//'):
            play_url = 'https:' + play_url

        header = {
            'User-Agent': self.UA,
            'Referer': self.Host + '/',
            'Origin': self.Host,
        }

        # 直接视频格式 → parse: 0
        if self.isVideoFormat(play_url):
            return {
                'parse': 0,
                'playUrl': '',
                'url': play_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        # 7mmtv.sx 加密包装页 (SP 源) → 递归提取内嵌视频地址或 m3u8
        if 'iframeencrypted' in play_url or ('7mmtv.sx' in play_url and 'content' not in play_url):
            embed_url = self._extract_embed_from_wrapper(play_url)
            if embed_url:
                # 如果直接返回 m3u8 → 直连播放
                if self.isVideoFormat(embed_url):
                    play_header = {
                        'User-Agent': self.UA,
                        'Referer': self.Host + '/',
                    }
                    return {
                        'parse': 0,
                        'playUrl': '',
                        'url': embed_url,
                        'header': json.dumps(play_header, ensure_ascii=False),
                        'jx': 0,
                    }
                # 如果是 emturbovid/turbovi → 尝试提取 m3u8
                if 'emturbo' in embed_url or 'turbovi' in embed_url:
                    m3u8_url = self._extract_m3u8_from_embed(embed_url)
                    if m3u8_url:
                        play_header = {
                            'User-Agent': self.UA,
                            'Referer': embed_url,
                        }
                        return {
                            'parse': 0,
                            'playUrl': '',
                            'url': m3u8_url,
                            'header': json.dumps(play_header, ensure_ascii=False),
                            'jx': 0,
                        }
                # 其他嵌入地址 → 嗅探
                return {
                    'parse': 1,
                    'playUrl': '',
                    'url': embed_url,
                    'header': json.dumps(header, ensure_ascii=False),
                    'jx': 0,
                }
            # 提取失败 → 直接嗅探包装页
            return {
                'parse': 1,
                'playUrl': '',
                'url': play_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        # emturbovid / turbovi → m3u8 公开可访问, 提取后直连播放
        if 'emturbo' in play_url or 'turbovi' in play_url:
            m3u8_url = self._extract_m3u8_from_embed(play_url)
            if m3u8_url:
                play_header = {
                    'User-Agent': self.UA,
                    'Referer': play_url,
                }
                return {
                    'parse': 0,
                    'playUrl': '',
                    'url': m3u8_url,
                    'header': json.dumps(play_header, ensure_ascii=False),
                    'jx': 0,
                }
            # 提取失败 → 嗅探
            return {
                'parse': 1,
                'playUrl': '',
                'url': play_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        # mmvh02 / mmsi02 / vidhide → m3u8 token 与 IP/ASN 绑定, 无法跨设备传递
        # 直接返回嵌入页让 APP webview 嗅探 (parse: 1)
        if any(d in play_url for d in ('mmvh', 'mmsi', 'vidhide', '/v/', '/e/')):
            return {
                'parse': 1,
                'playUrl': '',
                'url': play_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        # play.php 重定向页 (TV 源) → 嗅探
        if 'play.php' in play_url:
            return {
                'parse': 1,
                'playUrl': '',
                'url': play_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        # 其他 URL → 尝试提取 m3u8, 失败则嗅探
        m3u8_url = self._extract_m3u8_from_embed(play_url)
        if m3u8_url:
            return {
                'parse': 0,
                'playUrl': '',
                'url': m3u8_url,
                'header': json.dumps(header, ensure_ascii=False),
                'jx': 0,
            }

        return {
            'parse': 1,
            'playUrl': '',
            'url': play_url,
            'header': json.dumps(header, ensure_ascii=False),
            'jx': 0,
        }

    # ====== 搜索 ======

    def searchContent(self, key, quick, pg='1'):
        """关键词搜索 (POST)"""
        page = int(pg) if str(pg).isdigit() else 1
        keyword = str(key or '').strip()
        if not keyword:
            return {'list': [], 'parse': 0, 'jx': 0}

        # POST 搜索
        search_url = f'{self.Host}/{self.Lang}/searchform_search/all/index.html'
        post_data = {
            'search_keyword': keyword,
            'search_type': 'searchall',
            'op': 'search',
        }
        html = self._post(search_url, post_data, referer=self.Host)
        if not html:
            return {'list': [], 'parse': 0, 'jx': 0}

        items = self._parse_video_list(html)

        # 搜索结果页可能没有分页, 默认返回一页
        result = {
            'list': items[:40],
            'parse': 0,
            'jx': 0,
        }
        if page > 1:
            # 尝试翻页 URL: searchform_search/all/{keyword}/{page}.html (GET)
            page_url = f'{self.Host}/{self.Lang}/searchform_search/all/{urllib.parse.quote(keyword, safe="")}/{page}.html'
            page_html = self._fetch(page_url, referer=self.Host)
            if page_html:
                items = self._parse_video_list(page_html)
                result['list'] = items[:40]

        return result

    def searchContentPage(self, key, quick, page='1'):
        """搜索分页接口 (TVBox 兼容)"""
        return self.searchContent(key, quick, page)

    # ====== 本地代理 (图片) ======

    def localProxy(self, param):
        """代理图片请求, 解决封面图跨域/防盗链"""
        if not param:
            return None
        url = str(param)
        if not url.startswith('http'):
            return None

        try:
            headers = {
                'User-Agent': self.UA,
                'Referer': self.Host + '/',
            }
            r = self.fetch(url, headers=headers, timeout=15, verify=False)
            if r.status_code == 200:
                content_type = r.headers.get('Content-Type', 'image/jpeg')
                return [200, content_type, r.content]
        except Exception as e:
            print(f'[7mmtv] localProxy 异常: {e}')

        return None


# ============================================================
# 本地测试
# ============================================================

def _test():
    """独立运行测试各接口"""
    sp = Spider()
    sp.init()

    print('=' * 60)
    print('[1] 测试 homeContent (首页)')
    print('=' * 60)
    home = sp.homeContent()
    print(f'  分类数: {len(home.get("class", []))}')
    print(f'  筛选器数: {len(home.get("filters", {}))}')
    print(f'  推荐视频数: {len(home.get("list", []))}')
    sample_id = ''
    if home.get('list'):
        v = home['list'][0]
        print(f'  示例: {v.get("vod_name", "")[:50]}')
        print(f'    vod_id: {v.get("vod_id", "")}')
        print(f'    vod_pic: {v.get("vod_pic", "")[:80]}')
        sample_id = v['vod_id']

    print()
    print('=' * 60)
    print('[2] 测试 categoryContent (分类列表)')
    print('=' * 60)
    cat = sp.categoryContent('censored', '1', False, {})
    print(f'  视频数: {len(cat.get("list", []))}')
    print(f'  总页数: {cat.get("pagecount", 0)}')
    if cat.get('list'):
        print(f'  示例: {cat["list"][0].get("vod_name", "")[:50]}')

    print()
    print('=' * 60)
    print('[3] 测试 categoryContent (子分类筛选)')
    print('=' * 60)
    cat2 = sp.categoryContent('censored', '1', False, {'cat': '127'})
    print(f'  巨乳分类视频数: {len(cat2.get("list", []))}')

    print()
    print('=' * 60)
    print('[4] 测试 detailContent (详情页)')
    print('=' * 60)
    detail_data = None
    if home.get('list'):
        detail = sp.detailContent([sample_id])
        d = detail['list'][0]
        detail_data = d
        print(f'  标题: {d.get("vod_name", "")[:60]}')
        print(f'  封面: {d.get("vod_pic", "")[:80]}')
        print(f'  年份: {d.get("vod_year", "")}')
        print(f'  演员: {d.get("vod_actor", "")[:60]}')
        print(f'  简介: {d.get("vod_content", "")[:80]}...')
        print(f'  备注: {d.get("vod_remarks", "")}')
        print(f'  播放源: {d.get("vod_play_from", "")}')
        # 显示所有播放线路
        play_from = d.get('vod_play_from', '')
        play_url = d.get('vod_play_url', '')
        sources = play_from.split('$$$') if play_from else []
        urls = play_url.split('$$$') if play_url else []
        for i, (src, url_part) in enumerate(zip(sources, urls)):
            url = url_part.split('$', 1)[1].split('#')[0] if '$' in url_part else ''
            print(f'  线路{i+1} [{src}]: {url[:100]}')

    print()
    print('=' * 60)
    print('[5] 测试 searchContent (搜索)')
    print('=' * 60)
    search = sp.searchContent('JUR', 0, '1')
    print(f'  搜索结果数: {len(search.get("list", []))}')
    if search.get('list'):
        print(f'  示例: {search["list"][0].get("vod_name", "")[:50]}')

    print()
    print('=' * 60)
    print('[6] 测试 playerContent (播放)')
    print('=' * 60)
    if detail_data:
        play_from = detail_data.get('vod_play_from', '')
        play_url = detail_data.get('vod_play_url', '')
        sources = play_from.split('$$$') if play_from else []
        urls = play_url.split('$$$') if play_url else []
        for i, (src, url_part) in enumerate(zip(sources, urls)):
            url = url_part.split('$', 1)[1].split('#')[0] if '$' in url_part else ''
            if not url:
                continue
            print(f'  线路{i+1} [{src}]:')
            print(f'    原始: {url[:100]}')
            player = sp.playerContent(src, url)
            print(f'    parse: {player.get("parse", 0)} (0=直链, 1=嗅探)')
            print(f'    url: {player.get("url", "")[:120]}')

    print()
    print('测试完成!')


if __name__ == '__main__':
    _test()
