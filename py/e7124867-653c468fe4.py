# -*- coding: utf-8 -*-
# ============================================================
#  JRKAN体育直播 (PY源 - dr_py)
#  站点首页 : https://m.jrs03.com  (备用域名 www.jrs945.com)
#  站点类型 : JRKAN系 体育赛事直播(足球/篮球/电竞等)
#  数据链路 :
#    列表  : OSS静态文件 index.js (document.write格式, 含全天赛程+steamID)
#    比分  : OSS接口 tmp/event?type={zqlq|other} (AES-128-ECB加密JSON)
#            密文形如 cb_xx(["base64密文","UMY="]) -> 解密得 {list:{fields,values}}
#            fields: id/sportid/st_first/status/s1/s2... status: 0未开 1/3进行 2中场 8将开 -14完场
#    详情  : {host}/play/steam{ID}.html -> 频道列表(data-play相对路径)
#    播放  : 频道两类
#      1) /play/sm.html?id={ch}    -> /play/{ch}.html -> iframe id= -> m3u8直链
#      2) /play/{wen|pao|mgxl}/?id -> 页面encodedStr -> XXTEA解密 -> {ts,url} -> m3u8
#    加密  : 比分数据 AES-128-ECB(key='abcdabcdabcdabcd'); 播放页 XXTEA(key='ABCDEFGHIJKLMNOPQRSTUVWX')
#  环境     : dr_py 服务端 (需要 requests, 无其它依赖)
#  验证日期 : 2026-09-06  (实时比分解密+直播中场次m3u8实测通过)
# ============================================================

import sys
import json
import re
import time
import struct
import base64

sys.path.append('..')
from base.spider import Spider

try:
    import requests
except ImportError:
    requests = None  # dr_py 环境自带; 万一缺失时回退 self.fetch


class Spider(Spider):

    # 站点/线路配置 ------------------------------------------------
    M_HOST = 'https://m.jrs03.com'                                   # 列表站(备用: www.jrs945.com)
    INDEX_JS = 'https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js'
    # 实时比分接口(AES加密), zqlq=足球篮球 other=其它赛事
    EVENT_URL = 'https://im-imgs-bucket.oss-accelerate.aliyuncs.com/tmp/event?type=%s'
    AES_KEY = b'abcdabcdabcdabcd'                                    # 实时比分 AES-128-ECB 密钥
    # 三个播放线路(内容一致, 互为备份)
    PLAY_HOSTS = [
        'http://m.sportsteam53.com',
        'http://m.jw1104.com',
        'http://play.sportsteam356.com',
    ]
    XXTEA_KEY = b'ABCDEFGHIJKLMNOPQRSTUVWX'                          # paps播放器 XXTEA 密钥

    UA = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
    HEADERS = {
        'User-Agent': UA,
        'Referer': 'https://m.jrs03.com/',
    }
    PLAY_HEADERS = {
        'User-Agent': UA,
        'Referer': 'http://m.sportsteam53.com/',
    }

    # 分类: 0=今日全部 1=足球 2=篮球 9=其它
    TYPES = [
        {'type_id': '0', 'type_name': '今日赛事'},
        {'type_id': '1', 'type_name': '足球'},
        {'type_id': '2', 'type_name': '篮球'},
        {'type_id': '9', 'type_name': '其它'},
    ]

    # 列表缓存(赛程60秒) + 实时比分缓存(30秒)
    _list_cache = None
    _list_ts = 0
    LIST_TTL = 60
    _live_cache = None
    _live_ts = 0
    LIVE_TTL = 30

    # ---------------- 基础 ----------------

    def init(self, extend=''):
        pass

    def getName(self):
        return 'JRKAN体育直播'

    def isVideo(self):
        return True

    # ---------------- XXTEA 解密 ----------------

    DELTA = 0x9E3779B9

    @staticmethod
    def _to_u32(b):
        n = len(b) // 4
        return list(struct.unpack('<%dI' % n, b[:n * 4]))

    @classmethod
    def xxteaDecrypt(cls, data, key):
        """标准 XXTEA 解密(bytes -> bytes)"""
        if len(data) == 0 or len(data) % 4:
            return data
        v = cls._to_u32(data)
        k = cls._to_u32(key[:16].ljust(16, b'\x00'))
        n = len(v)
        if n < 2:
            return data
        rounds = 6 + 52 // n
        s = (rounds * cls.DELTA) & 0xFFFFFFFF
        y = v[0]
        while s:
            e = (s >> 2) & 3
            for p in range(n - 1, 0, -1):
                z = v[p - 1]
                mx = ((((z >> 5) ^ ((y << 2) & 0xFFFFFFFF)) + ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF))) ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & 0xFFFFFFFF
                v[p] = (v[p] - mx) & 0xFFFFFFFF
                y = v[p]
            z = v[n - 1]
            mx = ((((z >> 5) ^ ((y << 2) & 0xFFFFFFFF)) + ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF))) ^ ((s ^ y) + (k[0 ^ e] ^ z))) & 0xFFFFFFFF
            v[0] = (v[0] - mx) & 0xFFFFFFFF
            y = v[0]
            s = (s - cls.DELTA) & 0xFFFFFFFF
        return struct.pack('<%dI' % n, *v)

    def decodeEncStr(self, enc):
        """解密 encodedStr -> m3u8地址(失败返回空)"""
        try:
            raw = base64.b64decode(enc.strip())
            out = self.xxteaDecrypt(raw, self.XXTEA_KEY)
            txt = out.decode('utf-8', 'ignore').strip('\x00\r\n \t')
            m = re.search(r'(https?://[^"\s\\]+)', txt.replace('\\/', '/'))
            if m:
                return m.group(1)
            j = json.loads(txt)
            return j.get('url', '')
        except Exception:
            return ''

    # ---------------- AES-ECB 解密(实时比分数据) ----------------

    def aesEcbDecrypt(self, b64_text):
        """AES-128-ECB 解密 Base64 密文 -> 明文字符串(零依赖兜底)"""
        raw = base64.b64decode(b64_text.strip())
        try:
            from Crypto.Cipher import AES as _A
            out = _A.new(self.AES_KEY, _A.MODE_ECB).decrypt(raw)
        except Exception:
            out = self._aesEcbPure(raw)
        pad = out[-1] if out else 0
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            out = out[:-pad]
        return out.decode('utf-8', 'ignore')

    def _aesEcbPure(self, data):
        """纯Python AES-128-ECB 解密(无第三方依赖)"""
        sbox = self._SBOX()
        rk = self._expandKey(self.AES_KEY, sbox)
        out = bytearray()
        for i in range(0, len(data), 16):
            out += self._decBlock(data[i:i + 16], rk, sbox)
        return bytes(out)

    @staticmethod
    def _SBOX():
        box = getattr(Spider, '_SBOX_CACHE', None)
        return box if box is not None else Spider._build_sbox()

    @staticmethod
    def _build_sbox():
        p = q = 1
        box = [0] * 256
        while True:
            p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
            q ^= (q << 1) & 0xFF
            q ^= (q << 2) & 0xFF
            q ^= (q << 4) & 0xFF
            if q & 0x80:
                q ^= 0x09
            x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            box[p] = (x & 0xFF) ^ 0x63
            if p == 1:
                break
        box[0] = 0x63
        setattr(Spider, '_SBOX_CACHE', box)
        return box

    @staticmethod
    def _mul(a, b):
        r = 0
        for _ in range(8):
            if b & 1:
                r ^= a
            b >>= 1
            a <<= 1
            if a & 0x100:
                a ^= 0x11B
        return r

    @classmethod
    def _expandKey(cls, key, sbox):
        words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
        rc = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
        for i in range(4, 44):
            t = list(words[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [sbox[b] for b in t]
                t[0] ^= rc[i // 4 - 1]
            words.append([a ^ b for a, b in zip(words[i - 4], t)])
        return [sum(words[4 * r:4 * r + 4], []) for r in range(11)]

    @classmethod
    def _decBlock(cls, blk, rk, sbox):
        inv = getattr(Spider, '_INV_CACHE', None)
        if inv is None:
            inv = [0] * 256
            for i in range(256):
                inv[sbox[i]] = i
            setattr(Spider, '_INV_CACHE', inv)
        s = [a ^ b for a, b in zip(bytearray(blk), rk[10])]
        for rnd in range(9, -1, -1):
            t = s[:]
            for r in range(1, 4):
                for c in range(4):
                    t[((c + r) % 4) * 4 + r] = s[c * 4 + r]
            s = [inv[b] for b in t]
            s = [a ^ b for a, b in zip(s, rk[rnd])]
            if rnd:
                t = s[:]
                for c in range(4):
                    col = t[c * 4:c * 4 + 4]
                    s[c * 4 + 0] = cls._mul(col[0], 14) ^ cls._mul(col[1], 11) ^ cls._mul(col[2], 13) ^ cls._mul(col[3], 9)
                    s[c * 4 + 1] = cls._mul(col[0], 9) ^ cls._mul(col[1], 14) ^ cls._mul(col[2], 11) ^ cls._mul(col[3], 13)
                    s[c * 4 + 2] = cls._mul(col[0], 13) ^ cls._mul(col[1], 9) ^ cls._mul(col[2], 14) ^ cls._mul(col[3], 11)
                    s[c * 4 + 3] = cls._mul(col[0], 11) ^ cls._mul(col[1], 13) ^ cls._mul(col[2], 9) ^ cls._mul(col[3], 14)
        return bytes(s)

    # ---------------- 实时比分 ----------------

    def fetchLive(self, force=False):
        """拉取实时比分/状态 -> {mid: (status, s1, s2)} (缓存30秒)"""
        now = time.time()
        if not force and self._live_cache and now - self._live_ts < self.LIVE_TTL:
            return self._live_cache
        stat = {}
        for tp in ('zqlq', 'other'):
            body = self.get(self.EVENT_URL % tp + '&callback=cb&_=%d' % int(now * 1000))
            if not body:
                continue
            m = re.search(r'\["([A-Za-z0-9+/=]{50,})",\s*"[A-Za-z0-9+/=]*"\]', body)
            if not m:
                continue
            try:
                data = json.loads(self.aesEcbDecrypt(m.group(1)))
                lst = data.get('list', {}) or {}
                fields = lst.get('fields', [])
                if 'id' not in fields:
                    continue
                i_id, i_st = fields.index('id'), fields.index('status')
                i_s1, i_s2 = fields.index('s1'), fields.index('s2')
                for v in lst.get('values', []) or []:
                    try:
                        stat[str(v[i_id])] = (int(v[i_st]), str(v[i_s1]), str(v[i_s2]))
                    except Exception:
                        continue
            except Exception:
                continue
        Spider._live_cache = stat
        Spider._live_ts = now
        return stat

    @staticmethod
    def liveScore(it, live):
        """场次 -> (排序权重, 状态文本) 权重小在前: 0直播中 1即将开赛 2未开赛 3完场"""
        st = live.get(str(it.get('mid', '')))
        if st:
            status, s1, s2 = st
            has = (s1 or s2)
            if status == 0:
                return 2, '未开赛'
            if status == -14:
                return 3, ('完场 %s-%s' % (s1, s2)) if has else '完场'
            if status == 8:
                return 1, '即将开赛'
            if status == 2:
                return 0, ('中场 %s-%s' % (s1, s2)) if has else '中场'
            return 0, ('直播中 %s-%s' % (s1, s2)) if has else '直播中'
        # 无实时数据: 回退index.js比分或时间判断
        if it['hs'] and it['as']:
            return 0, '%s-%s' % (it['hs'], it['as'])
        return 2, '未开赛'

    # ---------------- HTTP ----------------

    def get(self, url, headers=None, timeout=15):
        """GET 返回文本(失败返回空串), 三线路自动切换仅用于播放域"""
        try:
            if requests is not None:
                r = requests.get(url, headers=headers or self.HEADERS, timeout=timeout)
                if r.encoding is None or r.encoding.lower() in ('iso-8859-1', 'ascii'):
                    r.encoding = 'utf-8'          # 站点无charset头, 强制utf-8避免中文乱码
                return r.text or ''
            r = self.fetch(url, headers=headers or self.HEADERS)
            txt = r.text or ''
            return txt
        except Exception as e:
            try:
                self.log('请求失败 %s : %s' % (url, e))
            except Exception:
                pass
            return ''

    def getPlayPage(self, path):
        """请求播放域页面(带线路容灾), path以/开头"""
        for host in self.PLAY_HOSTS:
            txt = self.get(host + path, headers=self.PLAY_HEADERS)
            if txt:
                return txt
        return ''

    # ---------------- 列表数据 ----------------

    def fetchMatches(self, force=False):
        """拉取并解析全天赛程(缓存60秒)"""
        now = time.time()
        if not force and self._list_cache and now - self._list_ts < self.LIST_TTL:
            return self._list_cache
        js = self.get(self.INDEX_JS + '?t=%d' % int(now * 1000))
        items = []
        if js:
            blocks = re.split(r"document\.write\('<ul ", js)
            for b in blocks:
                if 'data-lid' not in b:
                    continue
                lid = re.search(r'data-lid="([\d,]+)"', b)
                if not lid:
                    continue
                parts = lid.group(1).split(',')
                sportid = parts[1] if len(parts) > 1 else '1'
                mid = parts[0] if parts else ''                  # 赛事ID(实时比分系统的关联键)
                steam = re.search(r'getPlayUrl\("line\d+",\s*"(\d+)"\)', b)
                if not steam:
                    continue
                league = re.search(r'lab_events[^>]*><span class="name">([^<]*)</span>', b)
                t_m = re.search(r'lab_time">([\d\- :]+)<', b)
                home = re.search(r'lab_team_home"><strong class="name">([^<]*)</strong>', b)
                away = re.search(r'lab_team_away"><strong class="name">([^<]*)</strong>', b)
                hs = re.search(r'lab_team_home"><strong[^>]*>[^<]*</strong><em class="bf"[^>]*>([^<]*)<', b)
                as_ = re.search(r'lab_team_away"><strong[^>]*>[^<]*</strong><em class="bf"[^>]*>([^<]*)<', b)
                pic = re.search(r'lab_team_home">.*?<img[^>]*src="([^"]+)"', b)
                items.append({
                    'steam': steam.group(1),
                    'mid': mid,
                    'sportid': sportid,
                    'league': league.group(1) if league else '',
                    'time': t_m.group(1).strip() if t_m else '',
                    'home': home.group(1) if home else '',
                    'away': away.group(1) if away else '',
                    'hs': hs.group(1).strip() if hs and hs.group(1).strip() else '',
                    'as': as_.group(1).strip() if as_ and as_.group(1).strip() else '',
                    'pic': pic.group(1) if pic else '',
                })
        Spider._list_cache = items
        Spider._list_ts = now
        return items

    @staticmethod
    def matchItem(it, live=None):
        name = '%s %s vs %s' % (it['time'].split(' ', 1)[-1] if ' ' in it['time'] else it['time'],
                                it['home'], it['away'])
        rank, score = Spider.liveScore(it, live or {})
        return {
            'vod_id': it['steam'],
            'vod_name': name,
            'vod_pic': it['pic'],
            'vod_remarks': '%s | %s' % (it['league'], score),
        }, rank

    # ---------------- 首页 ----------------

    def homeContent(self, filter):
        result = {'class': self.TYPES, 'list': []}
        items = self.fetchMatches()
        live = self.fetchLive()
        pairs = sorted((self.matchItem(it, live) for it in items[:40]), key=lambda p: p[1])
        result['list'] = [p[0] for p in pairs]
        return result

    def homeVideoContent(self):
        items = self.fetchMatches()
        live = self.fetchLive()
        pairs = sorted((self.matchItem(it, live) for it in items[:40]), key=lambda p: p[1])
        return {'list': [p[0] for p in pairs]}

    # ---------------- 分类 ----------------

    def categoryContent(self, tid, pg, filter, extend):
        result = {'list': []}
        if int(pg) > 1:                       # 直播赛程单页, 无更多分页
            return result
        tid = str(tid)
        items = self.fetchMatches()
        if tid == '1':
            items = [i for i in items if i['sportid'] == '1']
        elif tid == '2':
            items = [i for i in items if i['sportid'] == '2']
        elif tid == '9':
            items = [i for i in items if i['sportid'] not in ('1', '2')]
        live = self.fetchLive()
        pairs = sorted((self.matchItem(it, live) for it in items), key=lambda p: p[1])
        result['list'] = [p[0] for p in pairs]
        result['page'] = 1
        result['pagecount'] = 1
        result['limit'] = len(result['list'])
        result['total'] = len(result['list'])
        return result

    # ---------------- 详情(频道列表) ----------------

    def detailContent(self, ids):
        steam_id = str(ids[0])
        html = self.getPlayPage('/play/steam%s.html' % steam_id)
        if not html:
            return {}
        # 比赛信息
        title = re.search(r'<title>([^<]*)</title>', html)
        league = re.search(r'lab_events[^>]*><span class="name">([^<]*)</span>', html)
        t_m = re.search(r'lab_time">([\d\- :]+)<', html)
        home = re.search(r'lab_team_home"><strong class="name">([^<]*)</strong>', html)
        away = re.search(r'lab_team_away"><strong class="name">([^<]*)</strong>', html)
        # 频道列表: <a class="item ..." data-play="/play/xxx" ...><strong>频道名</strong></a>
        chs = []
        for m in re.finditer(r'<a class="item[^"]*"[^>]*data-play="([^"]*)"[^>]*>(?:<em[^>]*></em>)?<strong>([^<]*)</strong></a>', html):
            play_path, ch_name = m.group(1).replace('&amp;', '&'), m.group(2).strip()
            if not play_path.startswith('/play/'):
                continue                       # 过滤注释掉的无效频道(&id2=)与广告位
            # 解析频道: /play/sm.html?id=56  |  /play/wen/?id=md5 | /play/pao/ | /play/mgxl/
            mm = re.match(r'/play/(sm\.html)\?id=(\d+)', play_path)
            if mm:
                chs.append((ch_name, 'sm', mm.group(2)))
                continue
            mm = re.match(r'/play/([a-z]+)/\?id=([A-Za-z0-9]+)', play_path)
            if mm:
                chs.append((ch_name, mm.group(1), mm.group(2)))
        if not chs:
            return {}
        # 高清加密频道(wen/pao/mgxl)优先, sm频道兜底排后
        order = {'wen': 0, 'pao': 1, 'mgxl': 2}
        chs.sort(key=lambda x: order.get(x[1], 9))
        seen, uniq = set(), []
        for c in chs:
            if c[0] not in seen:
                seen.add(c[0])
                uniq.append(c)
        vod = {
            'vod_id': steam_id,
            'vod_name': '%s vs %s' % (home.group(1) if home else '比赛',
                                      away.group(1) if away else '直播'),
            'vod_pic': '',
            'type_name': league.group(1) if league else '体育直播',
            'vod_year': '',
            'vod_area': '',
            'vod_actor': '',
            'vod_director': '',
            'vod_remarks': t_m.group(1).strip() if t_m else '直播中',
            'vod_content': '赛事: %s  开赛时间: %s\n直播线路一般在临近开赛前才上架, 若本场尚未开赛, 部分高清线路可能提示失败/404, 属正常现象, 请开赛后再来观看。' % (
                league.group(1) if league else '-', t_m.group(1).strip() if t_m else '-'),
        }
        vod['vod_play_from'] = '$$$'.join('%s[%s]' % (n, t) for n, t, _ in uniq)
        vod['vod_play_url'] = '$$$'.join('直播$%s:%s' % (t, p) for _, t, p in uniq)
        return vod

    # ---------------- 搜索(本地过滤) ----------------

    def searchContent(self, key, quick):
        key = (key or '').strip()
        if not key:
            return {}
        items = self.fetchMatches()
        live = self.fetchLive()
        hits = []
        for it in items:
            blob = '%s %s %s %s' % (it['league'], it['home'], it['away'], it['time'])
            if key.lower() in blob.lower():
                v, _ = self.matchItem(it, live)
                hits.append(v)
        if hits:
            return {'list': hits}
        return {}

    def searchContentPage(self, key, quick, pg):
        return self.searchContent(key, quick)

    # ---------------- 播放 ----------------

    def playerContent(self, flag, id, vipFlags):
        url = self.extractStream((id or '').strip())
        if not url:
            # 流未上架(未开赛)或提取失败: 返回网页播放器地址, parse=1交给webview嗅探
            # 开赛后网页端自动加载流, 嗅探即可拿到m3u8
            page = ''
            try:
                ctype, param = id.split(':', 1)
                if ctype == 'sm':
                    page = self.PLAY_HOSTS[0] + '/play/sm.html?id=%s&id2=' % param
                else:
                    page = self.PLAY_HOSTS[0] + '/play/%s/?id=%s&id2=' % (ctype, param)
            except Exception:
                pass
            return {'parse': 1, 'url': page, 'header': self.PLAY_HEADERS}
        return {
            'parse': 0,
            'playUrl': '',
            'url': url,
            'header': {
                'User-Agent': self.UA,
                'Referer': 'http://m.sportsteam53.com/',
            },
        }

    def extractStream(self, ch_id):
        """频道ID -> m3u8 (id格式: 'sm:56' / 'wen:md5' / 'pao:num' / 'mgxl:num')"""
        try:
            ctype, param = ch_id.split(':', 1)
        except ValueError:
            return ''
        if ctype == 'sm':
            # /play/{ch}.html -> iframe id=参数 即 m3u8
            html = self.getPlayPage('/play/%s.html' % param)
            m = re.search(r'<iframe[^>]*src="([^"]+)"', html)
            if not m:
                return ''
            src = m.group(1).replace('&amp;', '&')
            mu = re.search(r'id=(//[^"&\']+|https?://[^"&\']+)', src)
            if mu:
                u = mu.group(1)
                return ('http:' + u) if u.startswith('//') else u
            return ''
        # wen/pao/mgxl... : 通用 encodedStr + XXTEA 解密
        html = self.getPlayPage('/play/%s/?id=%s&id2=' % (ctype, param))
        if not html:
            return ''
        m = re.search(r'encodedStr\s*=\s*"([^"]+)"', html)
        if not m:
            return ''
        return self.decodeEncStr(m.group(1))

    # ---------------- 其它 ----------------

    def localProxy(self, param):
        return {}

    def run(self):
        return


if __name__ == '__main__':
    # 本地直测(非dr_py环境): 需同级有 base/spider.py 桩
    sp = Spider()
    home = sp.homeContent(True)
    print('[首页] 分类:', [c['type_name'] for c in home['class']], '| 赛事数:', len(home['list']))
    live = sp.fetchLive()
    print('[实时比分] 覆盖赛事:', len(live), '场 | 样本:', dict(list(live.items())[:3]))
    cat = sp.categoryContent('1', 1, 1, {})
    print('[足球] 场次:', len(cat['list']))
    for v in cat['list'][:6]:
        print('   ', v['vod_name'], '|', v['vod_remarks'])
    cat2 = sp.categoryContent('2', 1, 1, {})
    print('[篮球] 场次:', len(cat2['list']), '| 首场:', cat2['list'][0]['vod_name'] if cat2['list'] else '-')
    # 测一场直播中的比赛
    playing = [v for v in cat['list'] + cat2['list'] if '直播中' in v['vod_remarks'] or '中场' in v['vod_remarks']]
    test = (playing or cat['list'])[0]
    det = sp.detailContent([test['vod_id']])
    print('[详情]', det.get('vod_name'), '| 线路:', det.get('vod_play_from', '').replace('$$$', ' / '))
    for pu in det['vod_play_url'].split('$$$')[:2]:
        cid = pu.split('$', 1)[1]
        stream = sp.extractStream(cid)
        print('[播放]', cid, '-> m3u8:', (stream[:90] or '提取失败'))
        if stream:
            break
    sea = sp.searchContent('曼联', False)
    print('[搜索曼联]', len(sea.get('list', [])), '场:', [v['vod_name'] for v in sea.get('list', [])][:3])
