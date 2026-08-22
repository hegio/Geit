# -*- coding: utf-8 -*-
"""
javrate (javrate.com, TVBox Python 源)
适配 TVBox / 影视仓 / OK影视 等空壳影视 APP 的 Python 源

站点结构:
  视频卡片: .mgn-item > .movie-card-link[href,title,data-movie-code] + .mgn-cover[src]
  评分/日期: .score-label / .mgn-date
  分类页: /movie/new | /menu/uncensored | /keywords/movie/{keyword}?page=N&moviesort=5
  搜索页: /Search/{keyword}.html
  详情页: iframe#v2-player[src="/Player/V2?payload=...&poster=..."]
  播放器页: script 内含 m3u8 直链 (https://videocdn.avking.xyz/.../playlist.m3u8?token=...)
  封面CDN: https://picture.avking.xyz/

转换自 XYQHiker JSON 规则「鉴黄师」
"""

import re
import sys
import json
import requests
import urllib3
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.append('..')
try:
    from base.spider import Spider as BaseSpider
except ImportError:
    class BaseSpider:
        def fetch(self, url, headers=None, timeout=20, verify=False, cookies=None):
            s = requests.Session()
            s.trust_env = False
            return s.get(url, headers=headers, timeout=timeout, verify=verify, cookies=cookies)

        def post(self, url, headers=None, data=None, timeout=20, verify=False, cookies=None):
            s = requests.Session()
            s.trust_env = False
            return s.post(url, headers=headers, data=data, timeout=timeout, verify=verify, cookies=cookies)


class Spider(BaseSpider):
    name = 'javrate'
    host = 'https://www.javrate.com'

    # 本地翻墙代理 (留空则直连)
    PROXY = ''

    VIDEO_EXT = ('.m3u8', '.mp4', '.flv', '.mkv', '.avi', '.ts', '.m3u', '.mpd')

    # ==================== 分类配置 ====================

    CATEGORIES = [
        {'type_name': '最新更新', 'type_id': '/movie/new'},
        {'type_name': '無碼A片', 'type_id': '/menu/uncensored'},
        {'type_name': '日本A片', 'type_id': '/menu/censored'},
        {'type_name': '國產AV', 'type_id': '/menu/chinese'},
        {'type_name': '最多人看', 'type_id': '/movie/hot'},
        {'type_name': '最佳A片', 'type_id': '/movie/top'},
        {'type_name': '类型', 'type_id': '/keywords/movie/劇情'},
        {'type_name': '剧情', 'type_id': '/keywords/movie/出軌'},
        {'type_name': '职业', 'type_id': '/keywords/movie/女學生'},
        {'type_name': '关系', 'type_id': '/keywords/movie/姐姐・妹妹'},
        {'type_name': '衣着', 'type_id': '/keywords/movie/黑絲'},
        {'type_name': '特征', 'type_id': '/keywords/movie/美少女'},
        {'type_name': '主题', 'type_id': '/keywords/movie/淫亂'},
        {'type_name': '角色状态', 'type_id': '/keywords/movie/絕頂高潮'},
        {'type_name': '做爱玩法', 'type_id': '/keywords/movie/中出'},
        {'type_name': '习惯癖好', 'type_id': '/keywords/movie/胸控・戀乳癖'},
        {'type_name': '颜值身材', 'type_id': '/keywords/movie/美乳'},
        {'type_name': '场景地点', 'type_id': '/keywords/movie/酒店'},
    ]

    # ==================== 筛选器 ====================

    @staticmethod
    def _kv(n, v):
        return {'n': n, 'v': v}

    FILTERS = {}

    # ==================== 初始化 ====================

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self._debug = True
        self._build_filters()

    def _build_filters(self):
        """构建筛选器 (从 XYQHiker JSON 规则转换)"""
        kv = self._kv
        self.FILTERS = {
            '/keywords/movie/劇情': [
                {'key': 'cateId', 'name': '类型', 'value': [
                    kv('劇情', '/keywords/movie/劇情'), kv('美少女電影', '/keywords/movie/美少女電影'),
                    kv('國產', '/keywords/movie/國產'), kv('企畫', '/keywords/movie/企畫'),
                    kv('數位馬賽克', '/keywords/movie/數位馬賽克'), kv('紀錄片', '/keywords/movie/紀錄片'),
                    kv('出道作品', '/keywords/movie/出道作品'), kv('形象俱樂部', '/keywords/movie/形象俱樂部'),
                    kv('自拍', '/keywords/movie/自拍'), kv('無碼流出', '/keywords/movie/無碼流出'),
                    kv('素人作品', '/keywords/movie/素人作品'), kv('主觀視角', '/keywords/movie/主觀視角'),
                    kv('奇異的', '/keywords/movie/奇異的'), kv('高顔值', '/keywords/movie/高顔值'),
                    kv('奇聞趣事', '/keywords/movie/奇聞趣事'), kv('薄碼', '/keywords/movie/薄碼'),
                    kv('感官作品', '/keywords/movie/感官作品'), kv('4K', '/keywords/movie/4K'),
                    kv('無碼破解', '/keywords/movie/無碼破解'), kv('魔鬼系', '/keywords/movie/魔鬼系'),
                    kv('原作改編', '/keywords/movie/原作改編'), kv('暗黑系', '/keywords/movie/暗黑系'),
                    kv('真實拍攝', '/keywords/movie/真實拍攝'), kv('精選綜合', '/keywords/movie/精選綜合'),
                    kv('業餘', '/keywords/movie/業餘'), kv('4小時以上作品', '/keywords/movie/4小時以上作品'),
                    kv('綜藝', '/keywords/movie/綜藝'), kv('無做愛場面', '/keywords/movie/無做愛場面'),
                    kv('感謝祭', '/keywords/movie/感謝祭'), kv('重口味', '/keywords/movie/重口味'),
                    kv('惡搞', '/keywords/movie/惡搞'), kv('ASMR顱内高潮', '/keywords/movie/ASMR顱内高潮'),
                    kv('心理驚悚', '/keywords/movie/心理驚悚'), kv('局部特寫', '/keywords/movie/局部特寫'),
                    kv('二次元', '/keywords/movie/二次元'), kv('寫真偶像', '/keywords/movie/寫真偶像'),
                    kv('中日合作', '/keywords/movie/中日合作'), kv('紀念作', '/keywords/movie/紀念作'),
                    kv('漫畫改編', '/keywords/movie/漫畫改編'), kv('引退作品', '/keywords/movie/引退作品'),
                    kv('女性向', '/keywords/movie/女性向'), kv('教學', '/keywords/movie/教學'),
                    kv('故事集', '/keywords/movie/故事集'), kv('節日限定', '/keywords/movie/節日限定'),
                    kv('特效', '/keywords/movie/特效'), kv('偷拍・盜撮', '/keywords/movie/偷拍・盜撮'),
                    kv('搞笑・模仿', '/keywords/movie/搞笑・模仿'), kv('介紹影片', '/keywords/movie/介紹影片'),
                    kv('真人秀', '/keywords/movie/真人秀'), kv('合集', '/keywords/movie/合集'),
                    kv('民國', '/keywords/movie/民國'), kv('不露臉', '/keywords/movie/不露臉'),
                    kv('熱點改編', '/keywords/movie/熱點改編'), kv('中文字幕', '/keywords/movie/中文字幕'),
                ]}
            ],
            '/keywords/movie/出軌': [
                {'key': 'cateId', 'name': '剧情', 'value': [
                    kv('出軌', '/keywords/movie/出軌'), kv('NTR', '/keywords/movie/NTR'),
                    kv('強姦', '/keywords/movie/強姦'), kv('勾引', '/keywords/movie/勾引'),
                    kv('亂倫', '/keywords/movie/亂倫'), kv('輪姦', '/keywords/movie/輪姦'),
                    kv('按摩・物理治療・美容', '/keywords/movie/按摩・物理治療・美容'), kv('艷遇', '/keywords/movie/艷遇'),
                    kv('偷情', '/keywords/movie/偷情'), kv('脅迫做愛', '/keywords/movie/脅迫做愛'),
                    kv('女優訪談', '/keywords/movie/女優訪談'), kv('不倫', '/keywords/movie/不倫'),
                    kv('媚藥・迷藥', '/keywords/movie/媚藥・迷藥'), kv('純欲', '/keywords/movie/純欲'),
                    kv('迷姦', '/keywords/movie/迷姦'), kv('出差', '/keywords/movie/出差'),
                    kv('監禁', '/keywords/movie/監禁'), kv('純愛・戀愛', '/keywords/movie/純愛・戀愛'),
                    kv('水療・泡泡浴', '/keywords/movie/水療・泡泡浴'), kv('約炮', '/keywords/movie/約炮'),
                    kv('殘忍畫面', '/keywords/movie/殘忍畫面'), kv('欠債肉償', '/keywords/movie/欠債肉償'),
                    kv('報復', '/keywords/movie/報復'), kv('粉絲福利', '/keywords/movie/粉絲福利'),
                    kv('旅行', '/keywords/movie/旅行'), kv('上門福利', '/keywords/movie/上門福利'),
                    kv('身體換業務', '/keywords/movie/身體換業務'), kv('應召・援交', '/keywords/movie/應召・援交'),
                    kv('酒後亂性', '/keywords/movie/酒後亂性'), kv('綁架', '/keywords/movie/綁架'),
                    kv('獵豔', '/keywords/movie/獵豔'), kv('新人面試', '/keywords/movie/新人面試'),
                    kv('瑜伽·健身', '/keywords/movie/瑜伽·健身'), kv('女優面試', '/keywords/movie/女優面試'),
                    kv('街頭福利', '/keywords/movie/街頭福利'), kv('游戲COSPLAY', '/keywords/movie/游戲COSPLAY'),
                    kv('夫妻交換', '/keywords/movie/夫妻交換'), kv('私房攝影', '/keywords/movie/私房攝影'),
                    kv('加班', '/keywords/movie/加班'), kv('運動', '/keywords/movie/運動'),
                    kv('看病・住院', '/keywords/movie/看病・住院'), kv('搭訕', '/keywords/movie/搭訕'),
                    kv('同學聚會', '/keywords/movie/同學聚會'), kv('招待', '/keywords/movie/招待'),
                    kv('尾行', '/keywords/movie/尾行'), kv('上門推銷', '/keywords/movie/上門推銷'),
                    kv('校園生活', '/keywords/movie/校園生活'), kv('野戰', '/keywords/movie/野戰'),
                    kv('同住一屋・相部屋', '/keywords/movie/同住一屋・相部屋'), kv('直播', '/keywords/movie/直播'),
                    kv('撿尸', '/keywords/movie/撿尸'), kv('戰鬥行動', '/keywords/movie/戰鬥行動'),
                    kv('跳舞', '/keywords/movie/跳舞'), kv('男女互換', '/keywords/movie/男女互換'),
                    kv('上門家訪', '/keywords/movie/上門家訪'), kv('仙人跳', '/keywords/movie/仙人跳'),
                    kv('聚会・PARTY', '/keywords/movie/聚会・PARTY'), kv('新聞采訪', '/keywords/movie/新聞采訪'),
                    kv('迷奸', '/keywords/movie/迷奸'), kv('看房', '/keywords/movie/看房'),
                    kv('神話故事', '/keywords/movie/神話故事'), kv('裸體素描', '/keywords/movie/裸體素描'),
                    kv('時間靜止', '/keywords/movie/時間靜止'), kv('賭博', '/keywords/movie/賭博'),
                    kv('打麻將', '/keywords/movie/打麻將'), kv('喪夫', '/keywords/movie/喪夫'),
                    kv('相親', '/keywords/movie/相親'), kv('探親', '/keywords/movie/探親'),
                    kv('女體盛', '/keywords/movie/女體盛'), kv('疫情', '/keywords/movie/疫情'),
                    kv('網貸', '/keywords/movie/網貸'), kv('瑜伽', '/keywords/movie/瑜伽'),
                ]}
            ],
            '/keywords/movie/女學生': [
                {'key': 'cateId', 'name': '职业', 'value': [
                    kv('女學生', '/keywords/movie/女學生'), kv('女優', '/keywords/movie/女優'),
                    kv('OL', '/keywords/movie/OL'), kv('風俗娘', '/keywords/movie/風俗娘'),
                    kv('女教師', '/keywords/movie/女教師'), kv('按摩女郎', '/keywords/movie/按摩女郎'),
                    kv('護士', '/keywords/movie/護士'), kv('角色扮演', '/keywords/movie/角色扮演'),
                    kv('偶像', '/keywords/movie/偶像'), kv('女僕', '/keywords/movie/女僕'),
                    kv('酒店小姐・援交妹', '/keywords/movie/酒店小姐・援交妹'), kv('家庭教師', '/keywords/movie/家庭教師'),
                    kv('女奴', '/keywords/movie/女奴'), kv('女藝人・女星', '/keywords/movie/女藝人・女星'),
                    kv('其他職業', '/keywords/movie/其他職業'), kv('女主持・主播', '/keywords/movie/女主持・主播'),
                    kv('女搜查官', '/keywords/movie/女搜查官'), kv('空中小姐', '/keywords/movie/空中小姐'),
                    kv('老闆娘，女主人', '/keywords/movie/老闆娘，女主人'), kv('女秘書', '/keywords/movie/女秘書'),
                    kv('服務生', '/keywords/movie/服務生'), kv('家政婦', '/keywords/movie/家政婦'),
                    kv('店員', '/keywords/movie/店員'), kv('運動少女', '/keywords/movie/運動少女'),
                    kv('白人女優', '/keywords/movie/白人女優'), kv('黑帮', '/keywords/movie/黑帮'),
                    kv('寫真女郎', '/keywords/movie/寫真女郎'), kv('健身教練', '/keywords/movie/健身教練'),
                    kv('模特', '/keywords/movie/模特'), kv('女醫生', '/keywords/movie/女醫生'),
                    kv('女業務', '/keywords/movie/女業務'), kv('網路紅人', '/keywords/movie/網路紅人'),
                    kv('荷官', '/keywords/movie/荷官'), kv('女賊', '/keywords/movie/女賊'),
                    kv('賽車女郎', '/keywords/movie/賽車女郎'), kv('宅男・宅女', '/keywords/movie/宅男・宅女'),
                    kv('房產中介', '/keywords/movie/房產中介'), kv('女警察', '/keywords/movie/女警察'),
                    kv('女間諜・特工', '/keywords/movie/女間諜・特工'), kv('游泳教練', '/keywords/movie/游泳教練'),
                    kv('國產女優', '/keywords/movie/國產女優'), kv('修理工', '/keywords/movie/修理工'),
                    kv('黑人男優', '/keywords/movie/黑人男優'), kv('女鬼・女妖', '/keywords/movie/女鬼・女妖'),
                    kv('傳播妹', '/keywords/movie/傳播妹'), kv('女看護', '/keywords/movie/女看護'),
                    kv('DJ', '/keywords/movie/DJ'), kv('足球寶貝', '/keywords/movie/足球寶貝'),
                    kv('AI女優', '/keywords/movie/AI女優'), kv('台灣女優', '/keywords/movie/台灣女優'),
                    kv('舞女', '/keywords/movie/舞女'), kv('實習生', '/keywords/movie/實習生'),
                    kv('妓女', '/keywords/movie/妓女'), kv('性愛娃娃', '/keywords/movie/性愛娃娃'),
                    kv('女記者', '/keywords/movie/女記者'), kv('客服小姐', '/keywords/movie/客服小姐'),
                    kv('罪犯・逃犯', '/keywords/movie/罪犯・逃犯'), kv('女祭司', '/keywords/movie/女祭司'),
                    kv('泰國女優', '/keywords/movie/泰國女優'), kv('仙女', '/keywords/movie/仙女'),
                    kv('女戰士', '/keywords/movie/女戰士'), kv('女忍者', '/keywords/movie/女忍者'),
                    kv('老板', '/keywords/movie/老板'), kv('女格鬥家', '/keywords/movie/女格鬥家'),
                    kv('舞蹈老師', '/keywords/movie/舞蹈老師'), kv('女導游', '/keywords/movie/女導游'),
                    kv('家庭主妇', '/keywords/movie/家庭主妇'), kv('拉拉隊', '/keywords/movie/拉拉隊'),
                    kv('練習生', '/keywords/movie/練習生'), kv('女機器人', '/keywords/movie/女機器人'),
                    kv('殺手', '/keywords/movie/殺手'), kv('留學生', '/keywords/movie/留學生'),
                    kv('女律師', '/keywords/movie/女律師'), kv('道士', '/keywords/movie/道士'),
                    kv('名人妻子', '/keywords/movie/名人妻子'), kv('檳榔西施', '/keywords/movie/檳榔西施'),
                ]}
            ],
            '/keywords/movie/姐姐・妹妹': [
                {'key': 'cateId', 'name': '关系', 'value': [
                    kv('姐姐・妹妹', '/keywords/movie/姐姐・妹妹'), kv('女同事', '/keywords/movie/女同事'),
                    kv('女友・妻子', '/keywords/movie/女友・妻子'), kv('女上司', '/keywords/movie/女上司'),
                    kv('公公', '/keywords/movie/公公'), kv('姐弟・兄妹', '/keywords/movie/姐弟・兄妹'),
                    kv('鄰居', '/keywords/movie/鄰居'), kv('嫂子', '/keywords/movie/嫂子'),
                    kv('兒媳', '/keywords/movie/兒媳'), kv('父女', '/keywords/movie/父女'),
                    kv('青梅竹馬', '/keywords/movie/青梅竹馬'), kv('女友姐姐', '/keywords/movie/女友姐姐'),
                    kv('女友閨蜜', '/keywords/movie/女友閨蜜'), kv('女兒', '/keywords/movie/女兒'),
                    kv('繼母', '/keywords/movie/繼母'), kv('母親', '/keywords/movie/母親'),
                    kv('繼父', '/keywords/movie/繼父'), kv('同学', '/keywords/movie/同学'),
                    kv('小姨子', '/keywords/movie/小姨子'), kv('表姐・表妹', '/keywords/movie/表姐・表妹'),
                    kv('母子', '/keywords/movie/母子'), kv('叔叔・侄女', '/keywords/movie/叔叔・侄女'),
                    kv('朋友女友・妻子', '/keywords/movie/朋友女友・妻子'), kv('小三・情人', '/keywords/movie/小三・情人'),
                    kv('學姐・學妹', '/keywords/movie/學姐・學妹'), kv('岳母', '/keywords/movie/岳母'),
                    kv('小姨・姑姑', '/keywords/movie/小姨・姑姑'), kv('女租客', '/keywords/movie/女租客'),
                    kv('未亡人・寡婦', '/keywords/movie/未亡人・寡婦'), kv('朋友母親', '/keywords/movie/朋友母親'),
                    kv('嬸嬸', '/keywords/movie/嬸嬸'), kv('母女', '/keywords/movie/母女'),
                    kv('邻居', '/keywords/movie/邻居'), kv('上司女友・妻子', '/keywords/movie/上司女友・妻子'),
                    kv('下屬女友・妻子', '/keywords/movie/下屬女友・妻子'), kv('室友', '/keywords/movie/室友'),
                    kv('同学母亲', '/keywords/movie/同学母亲'), kv('母親的朋友', '/keywords/movie/母親的朋友'),
                    kv('儿子的朋友', '/keywords/movie/儿子的朋友'), kv('粉丝', '/keywords/movie/粉丝'),
                    kv('同事女友・妻子', '/keywords/movie/同事女友・妻子'), kv('前女友', '/keywords/movie/前女友'),
                    kv('夫妻', '/keywords/movie/夫妻'), kv('養女', '/keywords/movie/養女'),
                    kv('房东', '/keywords/movie/房东'), kv('弟媳', '/keywords/movie/弟媳'),
                    kv('男朋友的朋友', '/keywords/movie/男朋友的朋友'), kv('妻子的朋友', '/keywords/movie/妻子的朋友'),
                    kv('父親', '/keywords/movie/父親'), kv('女房東', '/keywords/movie/女房東'),
                    kv('姐夫', '/keywords/movie/姐夫'),
                ]}
            ],
            '/keywords/movie/黑絲': [
                {'key': 'cateId', 'name': '衣着', 'value': [
                    kv('黑絲', '/keywords/movie/黑絲'), kv('內衣', '/keywords/movie/內衣'),
                    kv('情趣内衣', '/keywords/movie/情趣内衣'), kv('制服', '/keywords/movie/制服'),
                    kv('JK校服', '/keywords/movie/JK校服'), kv('猥褻穿著', '/keywords/movie/猥褻穿著'),
                    kv('過膝襪・小腿襪', '/keywords/movie/過膝襪・小腿襪'), kv('COSPLAY服飾', '/keywords/movie/COSPLAY服飾'),
                    kv('和服・浴衣・喪服', '/keywords/movie/和服・浴衣・喪服'), kv('女僕制服', '/keywords/movie/女僕制服'),
                    kv('泳裝', '/keywords/movie/泳裝'), kv('短裙・迷你裙', '/keywords/movie/短裙・迷你裙'),
                    kv('眼鏡', '/keywords/movie/眼鏡'), kv('网袜', '/keywords/movie/网袜'),
                    kv('兔女郎妝扮', '/keywords/movie/兔女郎妝扮'), kv('護士制服', '/keywords/movie/護士制服'),
                    kv('網襪', '/keywords/movie/網襪'), kv('白絲', '/keywords/movie/白絲'),
                    kv('緊身衣', '/keywords/movie/緊身衣'), kv('高跟鞋', '/keywords/movie/高跟鞋'),
                    kv('運動服裝', '/keywords/movie/運動服裝'), kv('裸體圍裙', '/keywords/movie/裸體圍裙'),
                    kv('蒙面・面罩', '/keywords/movie/蒙面・面罩'), kv('身體意識', '/keywords/movie/身體意識'),
                    kv('中囯服裝', '/keywords/movie/中囯服裝'), kv('肉絲', '/keywords/movie/肉絲'),
                    kv('古裝', '/keywords/movie/古裝'), kv('警察制服', '/keywords/movie/警察制服'),
                    kv('靴子', '/keywords/movie/靴子'), kv('比基尼', '/keywords/movie/比基尼'),
                    kv('完全着衣', '/keywords/movie/完全着衣'), kv('貓耳裝飾', '/keywords/movie/貓耳裝飾'),
                    kv('空姐制服', '/keywords/movie/空姐制服'), kv('口罩', '/keywords/movie/口罩'),
                    kv('婚紗', '/keywords/movie/婚紗'), kv('牛仔褲', '/keywords/movie/牛仔褲'),
                    kv('丁字裤', '/keywords/movie/丁字裤'), kv('医生制服', '/keywords/movie/医生制服'),
                ]}
            ],
            '/keywords/movie/美少女': [
                {'key': 'cateId', 'name': '特征', 'value': [
                    kv('美少女', '/keywords/movie/美少女'), kv('蕩婦', '/keywords/movie/蕩婦'),
                    kv('人妻', '/keywords/movie/人妻'), kv('少女', '/keywords/movie/少女'),
                    kv('熟女', '/keywords/movie/熟女'), kv('癡女', '/keywords/movie/癡女'),
                    kv('素人', '/keywords/movie/素人'), kv('M男・M女', '/keywords/movie/M男・M女'),
                    kv('蠻橫嬌羞', '/keywords/movie/蠻橫嬌羞'), kv('新娘', '/keywords/movie/新娘'),
                    kv('叛逆少女', '/keywords/movie/叛逆少女'), kv('蘿莉', '/keywords/movie/蘿莉'),
                    kv('老頭子', '/keywords/movie/老頭子'), kv('變態', '/keywords/movie/變態'),
                    kv('辣妹', '/keywords/movie/辣妹'), kv('女同性戀', '/keywords/movie/女同性戀'),
                    kv('處男', '/keywords/movie/處男'), kv('大小姐', '/keywords/movie/大小姐'),
                    kv('痴漢', '/keywords/movie/痴漢'), kv('拜金女', '/keywords/movie/拜金女'),
                    kv('女王', '/keywords/movie/女王'), kv('綠茶婊', '/keywords/movie/綠茶婊'),
                    kv('文藝女', '/keywords/movie/文藝女'), kv('心機婊', '/keywords/movie/心機婊'),
                    kv('貴婦', '/keywords/movie/貴婦'), kv('變性者', '/keywords/movie/變性者'),
                    kv('校花', '/keywords/movie/校花'), kv('廢青', '/keywords/movie/廢青'),
                    kv('雙胞胎姐妹', '/keywords/movie/雙胞胎姐妹'), kv('中性', '/keywords/movie/中性'),
                    kv('富二代', '/keywords/movie/富二代'), kv('孕婦', '/keywords/movie/孕婦'),
                    kv('少數民族', '/keywords/movie/少數民族'), kv('處女', '/keywords/movie/處女'),
                ]}
            ],
            '/keywords/movie/淫亂': [
                {'key': 'cateId', 'name': '主题', 'value': [
                    kv('淫亂', '/keywords/movie/淫亂'), kv('多P', '/keywords/movie/多P'),
                    kv('按摩棒', '/keywords/movie/按摩棒'), kv('亂交', '/keywords/movie/亂交'),
                    kv('3P・4P', '/keywords/movie/3P・4P'), kv('凌辱', '/keywords/movie/凌辱'),
                    kv('抹油', '/keywords/movie/抹油'), kv('兩男一女', '/keywords/movie/兩男一女'),
                    kv('調教', '/keywords/movie/調教'), kv('拘束・拷問', '/keywords/movie/拘束・拷問'),
                    kv('捆綁', '/keywords/movie/捆綁'), kv('淫語', '/keywords/movie/淫語'),
                    kv('SM', '/keywords/movie/SM'), kv('放尿', '/keywords/movie/放尿'),
                    kv('兩女一男', '/keywords/movie/兩女一男'), kv('跳蛋', '/keywords/movie/跳蛋'),
                    kv('偷窺', '/keywords/movie/偷窺'), kv('性騷擾', '/keywords/movie/性騷擾'),
                    kv('色誘', '/keywords/movie/色誘'), kv('雙飛', '/keywords/movie/雙飛'),
                    kv('在丈夫面前被操', '/keywords/movie/在丈夫面前被操'), kv('调教', '/keywords/movie/调教'),
                    kv('誘騙女性', '/keywords/movie/誘騙女性'), kv('兩男兩女', '/keywords/movie/兩男兩女'),
                    kv('調戲', '/keywords/movie/調戲'), kv('一男多女', '/keywords/movie/一男多女'),
                    kv('催眠', '/keywords/movie/催眠'), kv('大亂交', '/keywords/movie/大亂交'),
                    kv('導尿', '/keywords/movie/導尿'), kv('露出', '/keywords/movie/露出'),
                    kv('約會', '/keywords/movie/約會'), kv('剃毛', '/keywords/movie/剃毛'),
                    kv('潛入', '/keywords/movie/潛入'), kv('立刻插入', '/keywords/movie/立刻插入'),
                    kv('喝尿', '/keywords/movie/喝尿'), kv('口球', '/keywords/movie/口球'),
                    kv('灌腸', '/keywords/movie/灌腸'), kv('猥亵', '/keywords/movie/猥亵'),
                    kv('蠟燭', '/keywords/movie/蠟燭'), kv('被外國人幹', '/keywords/movie/被外國人幹'),
                    kv('纹身刺字', '/keywords/movie/纹身刺字'), kv('瘙癢', '/keywords/movie/瘙癢'),
                ]}
            ],
            '/keywords/movie/絕頂高潮': [
                {'key': 'cateId', 'name': '角色状态', 'value': [
                    kv('絕頂高潮', '/keywords/movie/絕頂高潮'), kv('羞恥', '/keywords/movie/羞恥'),
                    kv('流汗', '/keywords/movie/流汗'), kv('慾求不滿', '/keywords/movie/慾求不滿'),
                    kv('酒醉', '/keywords/movie/酒醉'), kv('濕身', '/keywords/movie/濕身'),
                    kv('白眼失神', '/keywords/movie/白眼失神'),
                ]}
            ],
            '/keywords/movie/中出': [
                {'key': 'cateId', 'name': '做爱玩法', 'value': [
                    kv('中出', '/keywords/movie/中出'), kv('口交', '/keywords/movie/口交'),
                    kv('女上位', '/keywords/movie/女上位'), kv('潮吹', '/keywords/movie/潮吹'),
                    kv('乳交', '/keywords/movie/乳交'), kv('後入', '/keywords/movie/後入'),
                    kv('手指插入', '/keywords/movie/手指插入'), kv('騎乗位', '/keywords/movie/騎乗位'),
                    kv('舔陰', '/keywords/movie/舔陰'), kv('顏射', '/keywords/movie/顏射'),
                    kv('深喉', '/keywords/movie/深喉'), kv('接吻', '/keywords/movie/接吻'),
                    kv('69', '/keywords/movie/69'), kv('口爆', '/keywords/movie/口爆'),
                    kv('打手槍', '/keywords/movie/打手槍'), kv('自慰', '/keywords/movie/自慰'),
                    kv('吞精', '/keywords/movie/吞精'), kv('足交', '/keywords/movie/足交'),
                    kv('肛交', '/keywords/movie/肛交'), kv('舔腳', '/keywords/movie/舔腳'),
                    kv('打屁股', '/keywords/movie/打屁股'), kv('二穴同入', '/keywords/movie/二穴同入'),
                    kv('插入異物', '/keywords/movie/插入異物'), kv('唾液敷面', '/keywords/movie/唾液敷面'),
                    kv('顔面騎乘', '/keywords/movie/顔面騎乘'), kv('拳交', '/keywords/movie/拳交'),
                ]}
            ],
            '/keywords/movie/胸控・戀乳癖': [
                {'key': 'cateId', 'name': '习惯癖好', 'value': [
                    kv('胸控・戀乳癖', '/keywords/movie/胸控・戀乳癖'), kv('脚控・戀足癖', '/keywords/movie/脚控・戀足癖'),
                    kv('戀物癖', '/keywords/movie/戀物癖'), kv('性虐癖', '/keywords/movie/性虐癖'),
                    kv('臀控', '/keywords/movie/臀控'), kv('蘿莉控', '/keywords/movie/蘿莉控'),
                    kv('正太控', '/keywords/movie/正太控'),
                ]}
            ],
            '/keywords/movie/美乳': [
                {'key': 'cateId', 'name': '颜值身材', 'value': [
                    kv('美乳', '/keywords/movie/美乳'), kv('巨乳', '/keywords/movie/巨乳'),
                    kv('性感', '/keywords/movie/性感'), kv('苗條', '/keywords/movie/苗條'),
                    kv('美腳', '/keywords/movie/美腳'), kv('美臀', '/keywords/movie/美臀'),
                    kv('無毛', '/keywords/movie/無毛'), kv('大屁股', '/keywords/movie/大屁股'),
                    kv('美腿', '/keywords/movie/美腿'), kv('膚白', '/keywords/movie/膚白'),
                    kv('明星臉', '/keywords/movie/明星臉'), kv('大雞巴', '/keywords/movie/大雞巴'),
                    kv('小隻馬', '/keywords/movie/小隻馬'), kv('高妹', '/keywords/movie/高妹'),
                    kv('雙馬尾', '/keywords/movie/雙馬尾'), kv('貧乳', '/keywords/movie/貧乳'),
                    kv('刺青紋身', '/keywords/movie/刺青紋身'), kv('豐滿', '/keywords/movie/豐滿'),
                    kv('膚黑', '/keywords/movie/膚黑'), kv('短髮', '/keywords/movie/短髮'),
                    kv('大乳晕', '/keywords/movie/大乳晕'), kv('筋肉', '/keywords/movie/筋肉'),
                    kv('清纯', '/keywords/movie/清纯'), kv('金髮', '/keywords/movie/金髮'),
                    kv('乳釘、穿孔、乳環', '/keywords/movie/乳釘、穿孔、乳環'), kv('混血', '/keywords/movie/混血'),
                    kv('剛毛', '/keywords/movie/剛毛'),
                ]}
            ],
            '/keywords/movie/酒店': [
                {'key': 'cateId', 'name': '场景地点', 'value': [
                    kv('酒店', '/keywords/movie/酒店'), kv('自宅', '/keywords/movie/自宅'),
                    kv('學校', '/keywords/movie/學校'), kv('按摩・美容店', '/keywords/movie/按摩・美容店'),
                    kv('OFFICE', '/keywords/movie/OFFICE'), kv('溫泉', '/keywords/movie/溫泉'),
                    kv('醫院・診所', '/keywords/movie/醫院・診所'), kv('泡泡浴店', '/keywords/movie/泡泡浴店'),
                    kv('野外露天', '/keywords/movie/野外露天'), kv('風俗夜場', '/keywords/movie/風俗夜場'),
                    kv('電車', '/keywords/movie/電車'), kv('監獄', '/keywords/movie/監獄'),
                    kv('泳池', '/keywords/movie/泳池'), kv('AV拍攝現場', '/keywords/movie/AV拍攝現場'),
                    kv('酒吧', '/keywords/movie/酒吧'), kv('情趣酒店', '/keywords/movie/情趣酒店'),
                    kv('便利店', '/keywords/movie/便利店'), kv('健身房', '/keywords/movie/健身房'),
                    kv('鄉下', '/keywords/movie/鄉下'), kv('厠所', '/keywords/movie/厠所'),
                    kv('車震', '/keywords/movie/車震'), kv('商店', '/keywords/movie/商店'),
                    kv('海灘', '/keywords/movie/海灘'), kv('公共場所', '/keywords/movie/公共場所'),
                    kv('體育舘', '/keywords/movie/體育舘'), kv('更衣室', '/keywords/movie/更衣室'),
                    kv('KTV夜總會', '/keywords/movie/KTV夜總會'), kv('巴士', '/keywords/movie/巴士'),
                    kv('賭場', '/keywords/movie/賭場'), kv('倉庫', '/keywords/movie/倉庫'),
                    kv('廢墟', '/keywords/movie/廢墟'), kv('咖啡店', '/keywords/movie/咖啡店'),
                    kv('图书馆', '/keywords/movie/图书馆'), kv('畫室', '/keywords/movie/畫室'),
                    kv('電梯', '/keywords/movie/電梯'), kv('帐篷', '/keywords/movie/帐篷'),
                    kv('办公室', '/keywords/movie/办公室'), kv('計程車', '/keywords/movie/計程車'),
                    kv('厨房', '/keywords/movie/厨房'), kv('建築工地', '/keywords/movie/建築工地'),
                    kv('房车', '/keywords/movie/房车'),
                ]}
            ],
        }

    # ==================== TVBox 基础接口 ====================

    def getName(self):
        return self.name

    def init(self, extend=''):
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
            print('[%s] %s' % (self.name, msg))

    # ==================== 请求工具 ====================

    def _headers(self):
        return {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': self.host + '/',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Upgrade-Insecure-Requests': '1',
        }

    def _proxies(self):
        return {'http': self.PROXY, 'https': self.PROXY} if self.PROXY else None

    def _fetch_text(self, url, referer=None):
        """抓取页面文本"""
        try:
            hh = self._headers()
            if referer:
                hh['Referer'] = referer
            r = self.fetch(url, headers=hh, timeout=20, verify=False)
            if r is not None:
                if not r.encoding or r.encoding.lower() in ('iso-8859-1', 'latin-1'):
                    r.encoding = 'utf-8'
                return r.text or ''
        except Exception as e:
            self._log('请求失败: %s, %s' % (url, e))
        return ''

    # ==================== 解析工具 ====================

    def _parse_video_list(self, html):
        """
        解析视频列表页HTML，提取视频信息
        卡片结构: .mgn-item > .movie-card-link[href,title,data-movie-code] + .mgn-cover[src]
        """
        videos = []
        if not html:
            return videos

        parts = re.split(r'<div[^>]*class="[^"]*mgn-item[^"]*"', html)
        for block in parts[1:]:
            try:
                href_m = re.search(r'<a[^>]*class="[^"]*movie-card-link[^"]*"[^>]*href="([^"]*)"', block)
                if not href_m:
                    continue
                vod_id = href_m.group(1)

                code_m = re.search(r'data-movie-code="([^"]*)"', block)
                code = code_m.group(1) if code_m else ''

                title_m = re.search(r'<a[^>]*class="[^"]*movie-card-link[^"]*"[^>]*title="([^"]*)"', block)
                title = title_m.group(1) if title_m else ''

                cover_m = re.search(r'<img[^>]*class="[^"]*mgn-cover[^"]*"[^>]*src="([^"]*)"', block)
                cover = cover_m.group(1) if cover_m else ''

                rating_m = re.search(r'score-label[^>]*>([^<]*)<', block)
                rating = rating_m.group(1).strip() if rating_m else ''

                date_m = re.search(r'mgn-date[^>]*>([^<]*)<', block)
                date = date_m.group(1).strip() if date_m else ''

                if title and code and title != code:
                    vod_name = '%s %s' % (code, title)
                elif code:
                    vod_name = code
                else:
                    vod_name = title

                remarks = []
                if date:
                    remarks.append(u'\U0001F4C5' + date)
                if rating:
                    remarks.append(u'\u2B50' + rating)
                if not remarks:
                    remarks.append(code)

                videos.append({
                    'vod_id': vod_id,
                    'vod_name': vod_name,
                    'vod_pic': cover,
                    'vod_remarks': ' '.join(remarks),
                })
            except Exception:
                continue
        return videos

    def _get_page_count(self, html):
        """从分页信息提取总页数"""
        if not html:
            return 1
        m = re.search(r'data-page-info=".*?共\s*(\d+)\s*頁', html)
        if m:
            return int(m.group(1))
        pages = re.findall(r'page=(\d+)', html)
        if pages:
            return max(int(p) for p in pages)
        return 1

    @staticmethod
    def _play(url, headers, parse=0, play_url=''):
        """构造播放返回值"""
        ct = ''
        if '.m3u8' in str(url):
            ct = 'application/vnd.apple.mpegurl'
        return {
            'parse': parse,
            'playUrl': '',
            'url': url or play_url,
            'header': json.dumps(headers),
            'jx': 0,
            'contentType': ct,
        }

    # ==================== 标准方法 ====================

    def homeContent(self, filter=True):
        """首页: 分类列表 + 筛选器 + 首页推荐视频"""
        result = {
            'class': [{'type_name': c['type_name'], 'type_id': c['type_id']} for c in self.CATEGORIES],
            'filters': self.FILTERS,
            'parse': 0,
            'jx': 0,
        }
        try:
            html = self._fetch_text(self.host)
            result['list'] = self._parse_video_list(html)[:40]
        except Exception as e:
            self._log('homeContent 异常: %s' % e)
            result['list'] = []
        return result

    def homeVideoContent(self):
        """首页推荐视频"""
        try:
            html = self._fetch_text(self.host)
            return {'list': self._parse_video_list(html)[:40], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log('homeVideoContent 异常: %s' % e)
            return {'list': [], 'parse': 0, 'jx': 0}

    def categoryContent(self, tid, pg, filter=True, extend=None):
        """分类内容: 返回分类页视频列表"""
        page = int(pg) if pg else 1
        try:
            # 筛选器优先
            path = tid
            if extend and isinstance(extend, dict) and 'cateId' in extend:
                path = extend['cateId']

            url = '%s%s?page=%d' % (self.host, path, page)
            if '/keywords/' in path and 'moviesort' not in url:
                url += '&moviesort=5'

            html = self._fetch_text(url)
            items = self._parse_video_list(html)
            pc = self._get_page_count(html)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or 12,
                'total': pc * (len(items) or 12),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log('categoryContent 异常: %s' % e)
            return {'list': [], 'page': page, 'pagecount': page,
                    'limit': 12, 'total': 0, 'parse': 0, 'jx': 0}

    def detailContent(self, ids):
        """详情页: 返回视频详情和播放列表"""
        vod_id = ids[0] if isinstance(ids, (list, tuple)) else ids
        try:
            if vod_id.startswith('http'):
                url = vod_id
            elif vod_id.startswith('/'):
                url = self.host + vod_id
            else:
                url = self.host + '/' + vod_id

            html = self._fetch_text(url)
            if not html:
                return {'list': [{'vod_id': vod_id, 'vod_name': '获取失败', 'vod_pic': '',
                                  'vod_play_from': '默認線路', 'vod_play_url': ''}],
                        'parse': 0, 'jx': 0}

            vod = self._parse_detail(vod_id, html)
            return {'list': [vod], 'parse': 0, 'jx': 0}
        except Exception as e:
            self._log('detailContent 异常: %s' % e)
            return {'list': [{'vod_id': vod_id, 'vod_name': '获取失败', 'vod_pic': '',
                              'vod_play_from': '默認線路', 'vod_play_url': ''}],
                    'parse': 0, 'jx': 0}

    def _parse_detail(self, vod_id, html):
        """解析详情页HTML"""
        vod = {'vod_id': vod_id}

        # 标题: h1 > strong.fg-main(番号) + 标题文本
        code_m = re.search(r'<strong[^>]*class="[^"]*fg-main[^"]*"[^>]*>([^<]*)</strong>', html)
        code = code_m.group(1).strip() if code_m else ''

        h1_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if h1_m:
            title_text = re.sub(r'<[^>]+>', '', h1_m.group(1)).strip()
            vod['vod_name'] = title_text if title_text else code
        else:
            vod['vod_name'] = code

        # 封面: og:image
        og_m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]*)"', html)
        if og_m:
            vod['vod_pic'] = og_m.group(1)

        # 类型: .movie-type-link
        type_m = re.findall(r'movie-type-link[^>]*>([^<]*)<', html)
        if type_m:
            vod['type_name'] = ','.join(t.strip() for t in type_m if t.strip())

        # 年代
        year_m = re.search(r'mgn-badge-year[^>]*>([^<]*)<', html)
        if year_m:
            vod['vod_year'] = year_m.group(1).strip()

        # 演员: .mgn-actress a
        actress_m = re.findall(r'mgn-actress[^>]*>.*?<a[^>]*>([^<]*)</a>', html, re.S)
        if actress_m:
            vod['vod_actor'] = ','.join(a.strip() for a in actress_m if a.strip())

        # 简介: og:description
        desc_m = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        if desc_m:
            vod['vod_content'] = desc_m.group(1)

        # 播放: iframe#v2-player src
        iframe_m = re.search(r'<iframe[^>]*id="v2-player"[^>]*src="([^"]*)"', html)
        if not iframe_m:
            iframe_m = re.search(r'<iframe[^>]*src="([^"]*Player/V2[^"]*)"', html)

        if iframe_m:
            play_url = iframe_m.group(1).replace('&amp;', '&')
            vod['vod_play_from'] = u'\U0001F3A5' + u'默認線路'
            vod['vod_play_url'] = u'播放$' + play_url
        else:
            vod['vod_play_from'] = u'\U0001F3A5' + u'默認線路'
            vod['vod_play_url'] = ''

        return vod

    def searchContent(self, key, quick=False, pg='1'):
        """搜索: 返回搜索结果列表"""
        page = int(pg) if pg else 1
        try:
            if not key:
                return {'list': [], 'page': page, 'pagecount': 1, 'limit': 0, 'total': 0,
                        'parse': 0, 'jx': 0}
            keyword = quote(str(key))
            url = '%s/Search/%s.html' % (self.host, keyword)
            if page > 1:
                url += '?page=%d' % page

            html = self._fetch_text(url)
            items = self._parse_video_list(html)
            pc = self._get_page_count(html)
            return {
                'list': items,
                'page': page,
                'pagecount': pc,
                'limit': len(items) or 12,
                'total': pc * (len(items) or 12),
                'parse': 0,
                'jx': 0,
            }
        except Exception as e:
            self._log('searchContent 异常: %s' % e)
            return {'list': [], 'page': page, 'pagecount': 1, 'limit': 0, 'total': 0,
                    'parse': 0, 'jx': 0}

    def searchContentPage(self, key, quick=False, pg='1'):
        """搜索分页 (别名)"""
        return self.searchContent(key, quick, pg)

    def playerContent(self, flag, id, vipFlags=None):
        """播放解析: 从播放器页面提取m3u8直链"""
        pid = str(id or '').strip()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': self.host + '/',
        }
        try:
            # 已是直链 m3u8/mp4
            if pid.startswith('http') and self.isVideoFormat(pid):
                return self._play(pid, headers, parse=0)

            # 构建播放器页面URL
            if pid.startswith('http'):
                player_url = pid
            elif pid.startswith('/'):
                player_url = self.host + pid
            else:
                player_url = self.host + '/' + pid

            html = self._fetch_text(player_url, referer=self.host + '/')
            if not html:
                return self._play('', headers, parse=1, play_url=pid)

            # 从JavaScript中提取m3u8 URL
            m3u8_m = re.search(r'(https?://[^\s"\'\\]+\.m3u8[^\s"\'\\]*)', html)
            if not m3u8_m:
                m3u8_m = re.search(r'<source[^>]*src="([^"]*\.m3u8[^"]*)"', html)
            if not m3u8_m:
                m3u8_m = re.search(r'data-src="([^"]*\.m3u8[^"]*)"', html)
            if not m3u8_m:
                m3u8_m = re.search(r'src:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', html)

            if m3u8_m:
                m3u8_url = m3u8_m.group(1).replace('\\/', '/')
                return self._play(m3u8_url, headers, parse=0)

            # 无法提取m3u8, 交APP嗅探
            return self._play('', headers, parse=1, play_url=player_url)
        except Exception as e:
            self._log('playerContent 异常: %s' % e)
            return self._play('', headers, parse=1, play_url=pid)

    def localProxy(self, param):
        """本地代理 (封面回源)"""
        try:
            if isinstance(param, dict):
                url = param.get('url') or ''
            else:
                url = str(param or '')
            if not url.startswith('http'):
                return None
            r = self.fetch(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': self.host + '/',
            }, timeout=20, verify=False)
            ct = r.headers.get('Content-Type', 'application/octet-stream')
            return [200, ct, r.content]
        except Exception:
            return None


# ==================== 本地自测 ====================

if __name__ == '__main__':
    sp = Spider()
    sp.init()

    print('\n================ 1. 首页 / 分类 / 筛选器 ================')
    home = sp.homeContent(True)
    print('分类 %d 个' % len(home['class']))
    print('筛选器 %d 个' % len(home.get('filters', {})))
    print('首页推荐 %d 条' % len(home.get('list', [])))
    if home.get('list'):
        v = home['list'][0]
        print('  示例: %s | %s | %s' % (v['vod_name'], v['vod_id'], v['vod_remarks']))

    print('\n================ 2. 分类内容 ================')
    cat = sp.categoryContent('/movie/new', 1, True, {})
    print('视频 %d 条, 共 %s 頁' % (len(cat.get('list', [])), cat.get('pagecount')))

    print('\n================ 3. 搜索 ================')
    search = sp.searchContent('SSIS', False, '1')
    print('搜索结果 %d 条' % len(search.get('list', [])))

    print('\n测试完成')
