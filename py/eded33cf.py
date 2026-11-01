# coding=utf-8
from base.spider import Spider
import requests
import json
import re
import base64
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor
import math
import random
import socket
import string
import struct
import threading
import time
import uuid
import os
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import parse_qs, urlparse

# ========== 逐字歌词工具 ==========
_LX_LINE_TIME = re.compile(r'\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]')
_LX_WORD_TIME = re.compile(r'<(-?\d+),(-?\d+)(?:,-?\d+)?>')

def _lx_lyric_stamp(ms):
    ms = max(0, int(ms))
    return '%02d:%02d.%03d' % (ms // 60000, ms // 1000 % 60, ms % 1000)

def _lx_line_lyric(text):
    output = []
    for line in str(text or '').splitlines():
        first = _LX_LINE_TIME.match(line)
        if not first:
            output.append(line); continue
        end = first.end()
        while True:
            another = _LX_LINE_TIME.match(line, end)
            if not another: break
            end = another.end()
        if _LX_LINE_TIME.search(line[end:]):
            end = first.end()
        prefix, body = line[:end], line[end:]
        body = _LX_LINE_TIME.sub('', body)
        body = re.sub(r'<\d+:\d{2}(?:[.:]\d{1,3})?>', '', body)
        body = _LX_WORD_TIME.sub('', body)
        output.append(prefix + body)
    return '\n'.join(output)

def _lx_word_lyric(lx_text, original=''):
    """深度修复版：自适应字级 offset 语义，精准处理 duration 和 interval 差异"""
    om = re.search(r'^\[offset:\s*([+-]?\d+)\s*\]', lx_text, re.M | re.I)
    offset = int(om.group(1)) if om else 0
    rendered, starts, has_words = [], set(), False
    for line in lx_text.replace('\r', '').split('\n'):
        stamp = _LX_LINE_TIME.match(line)
        if not stamp: continue
        start = int(stamp.group(1)) * 60000 + int(stamp.group(2)) * 1000 + int((stamp.group(3) or '').ljust(3, '0')) + offset
        if start < 0: continue
        body = line[stamp.end():]
        tags = list(_LX_WORD_TIME.finditer(body))
        if not tags: continue
        has_words = True

        offs = [int(t.group(1)) for t in tags]
        durs = [int(t.group(2)) for t in tags]

        # 1) 判定是“相对行首偏移”还是“绝对时间戳”
        near_zero = offs[0] < 3000
        monotonic = all(offs[i+1] >= offs[i] for i in range(len(offs)-1))
        delta_ok = True
        for i in range(len(offs)-1):
            gap = offs[i+1] - offs[i]
            if durs[i] > 0 and not (durs[i] * 0.3 <= gap <= durs[i] * 3):
                delta_ok = False
                break
        relative_mode = near_zero and monotonic and delta_ok

        # 2) 判定 duration 的语义：是“持续时间(duration)” 还是 “到下一字的间隔(interval)”
        interval_mode = False
        if len(offs) >= 2:
            match_count = sum(1 for i in range(len(offs)-1)
                              if abs((offs[i] + durs[i]) - offs[i+1]) <= 20)
            if match_count >= (len(offs) - 1) * 0.7:
                interval_mode = True

        result, last_end = '[' + _lx_lyric_stamp(start) + ']', start
        result += body[:tags[0].start()]
        for i, tag in enumerate(tags):
            raw_off = int(tag.group(1))
            raw_dur = int(tag.group(2))
            ws = max(start, start + raw_off) if relative_mode else max(start, raw_off + offset)
            we = ws + max(0, raw_dur)
            
            # 如果是 duration 模式且与下一个字重叠，则强制截断
            if not interval_mode and i + 1 < len(tags):
                nxt = int(tags[i+1].group(1))
                next_ws = max(start, start + nxt) if relative_mode else max(start, nxt + offset)
                if we > next_ws:
                    we = next_ws
                    
            if ws != last_end:
                result += '[' + _lx_lyric_stamp(ws) + ']'
            result += body[tag.end():tags[i+1].start() if i + 1 < len(tags) else len(body)]
            result += '[' + _lx_lyric_stamp(we) + ']'
            last_end = we
        rendered.append((start, result)); starts.add(start)
    if not has_words: return ''
    
    metadata = []
    om2 = re.search(r'^\[offset:\s*([+-]?\d+)\s*\]', original, re.M | re.I)
    orig_off = int(om2.group(1)) if om2 else 0
    for line in _lx_line_lyric(original).splitlines():
        stamps = list(_LX_LINE_TIME.finditer(line))
        if not stamps:
            if line and not re.match(r'^\[offset:', line, re.I):
                metadata.append(line)
            continue
        text = line[stamps[-1].end():]
        for stamp in stamps:
            s = int(stamp.group(1)) * 60000 + int(stamp.group(2)) * 1000 + int((stamp.group(3) or '').ljust(3, '0')) + orig_off
            if s >= 0 and s not in starts:
                rendered.append((s, '[' + _lx_lyric_stamp(s) + ']' + text))
    return '\n'.join(metadata + [t for _, t in sorted(rendered, key=lambda x: x[0])])

def _split_render_units(text):
    units, buf = [], []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch == "'"):
            buf.append(ch)
        else:
            if buf: units.append(''.join(buf)); buf = []
            units.append(ch)
    if buf: units.append(''.join(buf))
    return units

def _unit_weight(u):
    if not u: return 0.0
    if u == ' ': return 0.2
    if len(u) == 1:
        if u in '。！？；：': return 1.6
        if u in '，、': return 1.2
        if u.isascii():
            return 0.9 if u in '.!?;:' else (0.7 if u == ',' else 0.5)
        return 1.0
    # 修复英文单词时长大问题（封顶 1.6）
    return min(max(0.5, len(u) * 0.35), 1.6)

def _estimate_unit_ms(entries):
    if len(entries) < 2: return 350
    span = entries[-1][0] - entries[0][0]
    if span <= 0: return 350
    tw = sum(_unit_weight(u) for _, t in entries for u in _split_render_units(t))
    if tw <= 0: return 350
    return max(120, min(int(span / tw), 1200))

def _derive_word_lyric_from_line(line_lrc):
    """修复版：为每行字级总时长设置合理下限（至少 gap 的 90%），并保证每字 ≥30ms。"""
    entries, offset = [], 0
    for line in str(line_lrc or '').splitlines():
        om = re.match(r'^\[offset:\s*([+-]?\d+)\s*\]', line, re.I)
        if om:
            try: offset = int(om.group(1))
            except ValueError: offset = 0
            continue
        stamps = list(_LX_LINE_TIME.finditer(line))
        if not stamps: continue
        text = line[stamps[-1].end():]
        text = re.sub(r'<\d+:\d{2}(?:[.:]\d{1,3})?>', '', text)
        text = _LX_WORD_TIME.sub('', text)
        if not text.strip(): continue
        for stamp in stamps:
            entries.append((int(stamp.group(1)) * 60000 + int(stamp.group(2)) * 1000
                            + int((stamp.group(3) or '').ljust(3, '0')) + offset, text))
    if not entries: return ''
    entries.sort(key=lambda x: x[0])
    per_unit_ms = _estimate_unit_ms(entries)
    output = []
    for i, (start, text) in enumerate(entries):
        next_start = entries[i + 1][0] if i + 1 < len(entries) else None
        units = _split_render_units(text)
        weights = [_unit_weight(u) for u in units]
        tw = sum(weights)
        if not units or tw <= 0:
            output.append('[' + _lx_lyric_stamp(start) + ']' + text); continue
        est = max(int(tw * per_unit_ms), 800)
        if next_start is None:
            dur = est + 1500
        else:
            gap = max(0, next_start - start)
            dur = min(gap, est)
            if dur < gap * 0.9:
                dur = int(gap * 0.9)
            if dur < 400: dur = min(gap, 400)
        if dur <= 0: dur = 500
        cum = 0.0
        parts = []
        for j, unit in enumerate(units):
            us = int(round(cum / tw * dur)); cum += weights[j]
            ue = int(round(cum / tw * dur))
            if ue <= us: ue = us + 30
            parts.append('<%d,%d>%s' % (us, max(0, ue - us), unit))
        output.append('[' + _lx_lyric_stamp(start) + ']' + ''.join(parts))
    return '\n'.join(output)


# ========== 酷我 mflac 解密 ==========
_QMC_M32 = 0xFFFFFFFF
_QMC_M64 = 0xFFFFFFFFFFFFFFFF
_ARRAY_E = [31, 0, 1, 2, 3, 4, -1, -1, 3, 4, 5, 6, 7, 8, -1, -1, 7, 8, 9, 10, 11, 12, -1, -1, 11, 12, 13, 14, 15, 16, -1, -1, 15, 16, 17, 18, 19, 20, -1, -1, 19, 20, 21, 22, 23, 24, -1, -1, 23, 24, 25, 26, 27, 28, -1, -1, 27, 28, 29, 30, 31, 30, -1, -1]
_ARRAY_IP = [57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7, 56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6]
_ARRAY_IP_1 = [39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25, 32, 0, 40, 8, 48, 16, 56, 24]
_ARRAY_P = [15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9, 1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24]
_ARRAY_PC_1 = [56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3]
_ARRAY_PC_2 = [13, 16, 10, 23, 0, 4, -1, -1, 2, 27, 14, 5, 20, 9, -1, -1, 22, 18, 11, 3, 25, 7, -1, -1, 15, 6, 26, 19, 12, 1, -1, -1, 40, 51, 30, 36, 46, 54, -1, -1, 29, 39, 50, 44, 32, 47, -1, -1, 43, 48, 38, 55, 33, 52, -1, -1, 45, 41, 49, 35, 28, 31, -1, -1]
_NS_BOX = [
    [14, 4, 3, 15, 2, 13, 5, 3, 13, 14, 6, 9, 11, 2, 0, 5, 4, 1, 10, 12, 15, 6, 9, 10, 1, 8, 12, 7, 8, 11, 7, 0, 0, 15, 10, 5, 14, 4, 9, 10, 7, 8, 12, 3, 13, 1, 3, 6, 15, 12, 6, 11, 2, 9, 5, 0, 4, 2, 11, 14, 1, 7, 8, 13],
    [15, 0, 9, 5, 6, 10, 12, 9, 8, 7, 2, 12, 3, 13, 5, 2, 1, 14, 7, 8, 11, 4, 0, 3, 14, 11, 13, 6, 4, 1, 10, 15, 3, 13, 12, 11, 15, 3, 6, 0, 4, 10, 1, 7, 8, 4, 11, 14, 13, 8, 0, 6, 2, 15, 9, 5, 7, 1, 10, 12, 14, 2, 5, 9],
    [10, 13, 1, 11, 6, 8, 11, 5, 9, 4, 12, 2, 15, 3, 2, 14, 0, 6, 13, 1, 3, 15, 4, 10, 14, 9, 7, 12, 5, 0, 8, 7, 13, 1, 2, 4, 3, 6, 12, 11, 0, 13, 5, 14, 6, 8, 15, 2, 7, 10, 8, 15, 4, 9, 11, 5, 9, 0, 14, 3, 10, 7, 1, 12],
    [7, 10, 1, 15, 0, 12, 11, 5, 14, 9, 8, 3, 9, 7, 4, 8, 13, 6, 2, 1, 6, 11, 12, 2, 3, 0, 5, 14, 10, 13, 15, 4, 13, 3, 4, 9, 6, 10, 1, 12, 11, 0, 2, 5, 0, 13, 14, 2, 8, 15, 7, 4, 15, 1, 10, 7, 5, 6, 12, 11, 3, 8, 9, 14],
    [2, 4, 8, 15, 7, 10, 13, 6, 4, 1, 3, 12, 11, 7, 14, 0, 12, 2, 5, 9, 10, 13, 0, 3, 1, 11, 15, 5, 6, 8, 9, 14, 14, 11, 5, 6, 4, 1, 3, 10, 2, 12, 15, 0, 13, 2, 8, 5, 11, 8, 0, 15, 7, 14, 9, 4, 12, 7, 10, 9, 1, 13, 6, 3],
    [12, 9, 0, 7, 9, 2, 14, 1, 10, 15, 3, 4, 6, 12, 5, 11, 1, 14, 13, 0, 2, 8, 7, 13, 15, 5, 4, 10, 8, 3, 11, 6, 10, 4, 6, 11, 7, 9, 0, 6, 4, 2, 13, 1, 9, 15, 3, 8, 15, 3, 1, 14, 12, 5, 11, 0, 2, 12, 14, 7, 5, 10, 8, 13],
    [4, 1, 3, 10, 15, 12, 5, 0, 2, 11, 9, 6, 8, 7, 6, 9, 11, 4, 12, 15, 0, 3, 10, 5, 14, 13, 7, 8, 13, 14, 1, 2, 13, 6, 14, 9, 4, 1, 2, 14, 11, 13, 5, 0, 1, 10, 8, 3, 0, 11, 3, 5, 9, 4, 15, 2, 7, 8, 12, 15, 10, 7, 6, 12],
    [13, 7, 10, 0, 6, 9, 5, 15, 8, 4, 3, 10, 11, 14, 12, 5, 2, 11, 9, 6, 15, 12, 0, 3, 4, 1, 14, 13, 1, 2, 7, 8, 1, 2, 12, 15, 10, 4, 0, 3, 13, 14, 6, 9, 7, 8, 9, 6, 15, 1, 5, 12, 3, 10, 14, 5, 8, 7, 11, 0, 4, 13, 2, 11],
]

_ARRAY_LS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_ARRAY_LS_MASK = [0, 0x100001, 0x300003]
_KUWO_SECRET_KEY = b'ylzsxkwm'
_QMC_V2_PREFIX = b'QQMusic EncV2,Key:'
_QMC_V2_KEY1 = bytes([0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40, 0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28])
_QMC_V2_KEY2 = bytes([0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25, 0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54])
_RC4_FIRST = 128
_RC4_SEG = 5120


def _bit_transform(arr, n, value):
    out = 0
    for i in range(n):
        b = arr[i]
        if b >= 0 and value & (1 << b):
            out |= 1 << i
    return out


def _des64(keys, value):
    o = _bit_transform(_ARRAY_IP, 64, value)
    s0, s1 = o & _QMC_M32, o >> 32
    for rk in keys:
        right = _bit_transform(_ARRAY_E, 64, s1) ^ rk
        s_out = 0
        for box in range(7, -1, -1):
            s_out = (s_out << 4) | _NS_BOX[box][(right >> (box * 8)) & 0xFF]
        right = _bit_transform(_ARRAY_P, 32, s_out)
        s0, s1 = s1, s0 ^ right
    s0, s1 = s1, s0
    merged = ((s1 << 32) & 0xFFFFFFFF00000000) | (s0 & _QMC_M32)
    return _bit_transform(_ARRAY_IP_1, 64, merged)


def _sub_keys(value, mode):
    ks = []
    t = _bit_transform(_ARRAY_PC_1, 56, value)
    for i in range(16):
        sh = _ARRAY_LS[i]
        mask = _ARRAY_LS_MASK[sh]
        t = (((t & mask) << (28 - sh)) | ((t & ~mask & _QMC_M64) >> sh)) & _QMC_M64
        ks.append(_bit_transform(_ARRAY_PC_2, 64, t))
    if mode == 1:
        ks.reverse()
    return ks


def _kuwo_des_decrypt(data, key=_KUWO_SECRET_KEY):
    if len(data) % 8:
        raise ValueError('kuwo des: length not multiple of 8')
    kb = int.from_bytes(key, 'little')
    ks = _sub_keys(kb, 1)
    out = bytearray()
    for i in range(0, len(data), 8):
        blk = int.from_bytes(data[i:i + 8], 'little')
        out += _des64(ks, blk).to_bytes(8, 'little')
    return bytes(out)


def _kuwo_b64_decrypt(value):
    res = _kuwo_des_decrypt(base64.b64decode(value))
    return res.rstrip(b'\x00').decode('utf-8')


def _kuwo_b64_encrypt(value):
    data = value.encode('utf-8')
    data += b'\0' * (8 - len(data) % 8)
    keys = _sub_keys(int.from_bytes(_KUWO_SECRET_KEY, 'little'), 0)
    encrypted = b''.join(_des64(keys, int.from_bytes(data[i:i + 8], 'little')).to_bytes(8, 'little')
                         for i in range(0, len(data), 8))
    return base64.b64encode(encrypted).decode('ascii')


def _qmc_raw_key_from_ekey(ekey):
    dec = _kuwo_b64_decrypt(ekey)
    for n in (704, 364):
        if len(dec) < n:
            continue
        cand = dec[-n:]
        try:
            base64.b64decode(cand, validate=True)
            return cand.encode('ascii')
        except Exception:
            continue
    raise ValueError('cannot extract QMC raw key from ekey')


def _simple_make_key(salt, length):
    return bytes((int(abs(math.tan(salt + i * 0.1)) * 100.0) & 0xFF) for i in range(length))


def _tea_block(block, key):
    v0, v1 = struct.unpack('>II', block)
    k0, k1, k2, k3 = struct.unpack('>4I', key)
    delta = 0x9E3779B9
    s = (delta * 16) & _QMC_M32
    for _ in range(16):
        v1 = (v1 - ((((v0 << 4) + k2) & _QMC_M32) ^ ((v0 + s) & _QMC_M32) ^ (((v0 >> 5) + k3) & _QMC_M32))) & _QMC_M32
        v0 = (v0 - ((((v1 << 4) + k0) & _QMC_M32) ^ ((v1 + s) & _QMC_M32) ^ (((v1 >> 5) + k1) & _QMC_M32))) & _QMC_M32
        s = (s - delta) & _QMC_M32
    return struct.pack('>II', v0, v1)


def _tencent_tea_decrypt(inp, key):
    if len(inp) % 8 or len(inp) < 16 or len(key) != 16:
        raise ValueError('bad tea input')
    dec = bytearray(_tea_block(inp[:8], key))
    pad = dec[0] & 7
    out_len = len(inp) - (1 + pad + 2 + 7)
    if out_len < 0:
        raise ValueError('bad tea padding')
    out = bytearray(out_len)
    prev, cur, pos, dest = bytes(8), bytes(inp[:8]), 8, 1 + pad

    def nxt():
        nonlocal prev, cur, pos, dest, dec
        prev = cur
        blk = bytes(inp[pos:pos + 8])
        if len(blk) != 8:
            raise ValueError('tea buffer end')
        cur = blk
        dec = bytearray(_tea_block(bytes(a ^ b for a, b in zip(dec, cur)), key))
        pos += 8
        dest = 0

    salt = 1
    while salt <= 2:
        if dest < 8:
            dest += 1
            salt += 1
        else:
            nxt()
    o = 0
    while o < out_len:
        if dest < 8:
            out[o] = dec[dest] ^ prev[dest]
            dest += 1
            o += 1
        else:
            nxt()
    z = 1
    while z <= 7:
        if dest < 8:
            if dec[dest] != prev[dest]:
                raise ValueError('tea zero check failed')
            dest += 1
            z += 1
        else:
            nxt()
    return bytes(out)


def _derive_key_v1(raw):
    if len(raw) < 16:
        raise ValueError('key too short')
    sk = _simple_make_key(106, 8)
    tk = bytearray(16)
    for i in range(8):
        tk[i << 1] = sk[i]
        tk[(i << 1) + 1] = raw[i]
    return bytes(raw[:8]) + _tencent_tea_decrypt(bytes(raw[8:]), bytes(tk))


def _derive_qmc_key(raw_key):
    dec = base64.b64decode(raw_key)
    if dec.startswith(_QMC_V2_PREFIX):
        buf = _tencent_tea_decrypt(dec[len(_QMC_V2_PREFIX):], _QMC_V2_KEY1)
        buf = _tencent_tea_decrypt(buf, _QMC_V2_KEY2)
        dec = base64.b64decode(buf)
    return _derive_key_v1(dec)


class _QmcMapCipher(object):
    def __init__(self, key):
        self.size = len(key)
        t = []
        for o in range(0x8000):
            idx = (o * o + 71214) % self.size
            v, r = key[idx], ((idx & 7) + 4) % 8
            t.append(((v << r) | (v >> r)) & 0xFF)
        self.table = bytes(t)
        self.period = self.table[:0x7FFF]

    def decrypt(self, buf, offset):
        n = len(buf)
        masks, o, rem = [], offset, n
        while rem > 0:
            if o <= 0x7FFF:
                take = min(rem, 0x8000 - o)
                masks.append(self.table[o:o + take])
            else:
                p = o % 0x7FFF
                take = min(rem, 0x7FFF - p)
                masks.append(self.period[p:p + take])
            o += take
            rem -= take
        m = b''.join(masks)
        return (int.from_bytes(buf, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')


class _QmcRc4Cipher(object):
    def __init__(self, key):
        self.key = key
        self.size = n = len(key)
        box = [i & 0xFF for i in range(n)]
        j = 0
        for i in range(n):
            j = (j + box[i] + key[i % n]) % n
            box[i], box[j] = box[j], box[i]
        self.box = box
        h = 1
        for v in key:
            if v == 0:
                continue
            nh = (h * v) & _QMC_M32
            if nh == 0 or nh <= h:
                break
            h = nh
        self.hash = h

    def _skip(self, seg):
        seed = self.key[seg % self.size]
        if seed == 0:
            return 0
        return int((self.hash / float((seg + 1) * seed)) * 100.0) % self.size

    def _first(self, buf, offset):
        key, skip = self.key, self._skip
        return bytes(b ^ key[skip(offset + i)] for i, b in enumerate(buf))

    def _segment(self, buf, offset):
        box = list(self.box)
        n = self.size
        j = k = 0
        skip = (offset % _RC4_SEG) + self._skip(offset // _RC4_SEG)
        out = bytearray(buf)
        for idx in range(skip + len(buf)):
            j = (j + 1) % n
            bj = box[j]
            k = (bj + k) % n
            bk = box[k]
            box[j], box[k] = bk, bj
            if idx >= skip:
                out[idx - skip] ^= box[(bk + bj) % n]
        return bytes(out)

    def decrypt(self, buf, offset):
        todo, done, out = len(buf), 0, []
        if offset < _RC4_FIRST:
            bs = min(todo, _RC4_FIRST - offset)
            out.append(self._first(buf[:bs], offset))
            offset += bs
            todo -= bs
            done += bs
            if todo == 0:
                return b''.join(out)
        if offset % _RC4_SEG:
            bs = min(todo, _RC4_SEG - offset % _RC4_SEG)
            out.append(self._segment(buf[done:done + bs], offset))
            offset += bs
            todo -= bs
            done += bs
            if todo == 0:
                return b''.join(out)
        while todo > _RC4_SEG:
            out.append(self._segment(buf[done:done + _RC4_SEG], offset))
            offset += _RC4_SEG
            todo -= _RC4_SEG
            done += _RC4_SEG
        if todo > 0:
            out.append(self._segment(buf[done:], offset))
        return b''.join(out)


_QMC_CIPHER_CACHE = {}


def kuwo_qmc_cipher(ekey):
    c = _QMC_CIPHER_CACHE.get(ekey)
    if c is None:
        key = _derive_qmc_key(_qmc_raw_key_from_ekey(ekey))
        c = _QmcRc4Cipher(key) if len(key) > 300 else _QmcMapCipher(key)
        if len(_QMC_CIPHER_CACHE) > 16:
            _QMC_CIPHER_CACHE.clear()
        _QMC_CIPHER_CACHE[ekey] = c
    return c


# ========== 本地代理服务 ==========
_KW_PROXY = {'srv': None, 'lock': threading.Lock()}
_KW_MV_STREAMS = {}

def _b64u(s):
    return base64.urlsafe_b64encode(s.encode('utf-8')).decode('ascii').rstrip('=')

def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)).decode('utf-8')


class _KwDecryptHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)

    def _serve(self, head):
        if urlparse(self.path).path == '/kw.mv.mp4':
            return self._serve_mv(head)
        try:
            q = parse_qs(urlparse(self.path).query)
            url = _b64u_dec(q['u'][0])
            cipher = kuwo_qmc_cipher(_b64u_dec(q['e'][0]))
        except Exception:
            self.send_error(400)
            return
        hdr = {'User-Agent': 'okhttp/3.10.0', 'Accept-Encoding': 'identity'}
        if self.headers.get('Range'):
            hdr['Range'] = self.headers['Range']
        try:
            r = requests.get(url, headers=hdr, stream=True, timeout=(8, 25))
        except Exception:
            self.send_error(502)
            return
        try:
            st = r.status_code
            if st not in (200, 206):
                self.send_error(st if st >= 400 else 502)
                return
            
            cr = r.headers.get('Content-Range', '')
            m = re.match(r'bytes (\d+)-', cr)
            off = int(m.group(1)) if (st == 206 and m) else 0
            self.send_response(st)
            self.send_header('Content-Type', 'audio/flac')
            self.send_header('Accept-Ranges', 'bytes')
            if r.headers.get('Content-Length'):
                self.send_header('Content-Length', r.headers['Content-Length'])
            if cr:
                self.send_header('Content-Range', cr)
            self.send_header('Connection', 'close')
            self.end_headers()
            if head:
                return
            for chunk in r.iter_content(65536):
                if chunk:
                    self.wfile.write(cipher.decrypt(chunk, off))
                    off += len(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            r.close()

    def _serve_mv(self, head):
        token = parse_qs(urlparse(self.path).query).get('id', [''])[0]
        with _KW_PROXY['lock']:
            info = _KW_MV_STREAMS.get(token)
        if not info:
            self.send_error(404, 'MV session expired; reopen the episode')
            return
            
        headers = dict(info['headers'])
        requested_range = self.headers.get('Range')
        if requested_range:
            headers['Range'] = requested_range
            
        response = None
        started = False
        try:
            response = requests.get(info['url'], headers=headers, stream=True, timeout=(8, 25))
            if response.status_code not in (200, 206):
                raise ValueError('upstream HTTP%s' % response.status_code)
                
            self.send_response(response.status_code)
            content_type = response.headers.get('Content-Type', 'video/mp4')
            self.send_header('Content-Type', content_type)
            self.send_header('Accept-Ranges', 'bytes')
            
            for key in ('Content-Length', 'Content-Range'):
                if response.headers.get(key):
                    self.send_header(key, response.headers[key])
                    
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            started = True
            
            if not head:
                for chunk in response.iter_content(65536):
                    if chunk:
                        self.wfile.write(chunk)
                        
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print('[KW MV relay] %s: %s' % (type(exc).__name__, str(exc)))
            if not started:
                self.send_error(502, 'MV stream rejected; see Python log')
        finally:
            self.close_connection = True
            if response is not None:
                response.close()


def _kw_proxy_port(port_hint=0):
    with _KW_PROXY['lock']:
        if _KW_PROXY['srv'] is None:
            srv = ThreadingHTTPServer(('0.0.0.0', int(port_hint or 0)), _KwDecryptHandler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _KW_PROXY['srv'] = srv
        return _KW_PROXY['srv'].server_address[1]


def _kw_detect_host():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('0.'):
            return ip
    except Exception:
        pass
    return '127.0.0.1'


class Spider(Spider):
    word_lyrics = True
    fake_word_lyric = True
    lyric_mode = 'line'
    debug_lyric = False
    last_word_lyric_trace = []
    last_raw_lyric_sample = ""
    last_used_fake = False
    _session = None
    _kw_token = ''

    _song_pic_cache = {}
    _PIC_SEP = '@@'
    _MV_META_SEP = '~mv~'
    _kw_hot_word_cache = {}

    LINE_AUDIO = '酷我'
    LINE_MV = 'MV'
    AUDIO_TIERS = [
        ('母带192kHz无损', ['20900kmflac'], 'flac'),
        ('环绕声5.1无损', ['20501kmflac'], 'flac'),
        ('立体声无损', ['20201kmflac'], 'flac'),
        ('Hi-Res无损', ['4000kflac'], 'flac'),
        ('无损FLAC', ['2000kflac'], 'flac'),
        ('高品质320K', ['320kmp3'], 'mp3'),
        ('标准128K', ['128kmp3'], 'mp3'),
    ]
    MV_TIERS = [
        ('1080P', ['1080P'], 'mp4'),
        ('720P', ['720P'], 'mp4'),
        ('480P', ['480P'], 'mp4'),
        ('MP4', ['mp4'], 'mp4'),
    ]
    _MV_CLIENT_API = 'https://anymatch.kuwo.cn/api/mv/play'
    _MV_CLIENT_VERSION = '12.2.2.4'
    _MV_CLIENT_SOURCE = 'kwplayer_ar_12.2.2.4_kuwoH5.apk'
    _TRACK_API = 'https://changenotice.kuwo.cn/mobi.s'
    _TRACK_SOURCE = 'kwplayer_ar_8.5.5.0_keluze.apk'
    _LEGACY_BR = {'2000kflac': 2000, '320kmp3': 320, '128kmp3': 128}
    debug_quality = False
    audio_tiers = None
    proxy_host = ''
    proxy_port = 0
    default_quality = 'lossless'

    def getName(self):
        return "小心儿悠悠"

    def init(self, extend=""):
        self._session = requests.Session()
        self._mv_verified = {}
        self._mv_info_cache = {}
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Referer': 'https://www.kuwo.cn/',
        })
        self._kw_search_keyword = ''
        self.audio_tiers = list(self.AUDIO_TIERS)
        if extend:
            try:
                cfg = json.loads(extend) if isinstance(extend, str) else extend
            except (ValueError, TypeError):
                cfg = None
            if isinstance(cfg, dict):
                if 'word_lyrics' in cfg: self.word_lyrics = self._bool_of(cfg['word_lyrics'])
                if 'fake_word_lyric' in cfg: self.fake_word_lyric = self._bool_of(cfg['fake_word_lyric'])
                if 'debug_lyric' in cfg: self.debug_lyric = self._bool_of(cfg['debug_lyric'])
                if 'debug_quality' in cfg: self.debug_quality = self._bool_of(cfg['debug_quality'])
                if 'lyric_mode' in cfg:
                    self.lyric_mode = 'word' if str(cfg['lyric_mode']).lower() in ('word', '逐字', '字') else 'line'
                if cfg.get('proxy_host'): self.proxy_host = str(cfg['proxy_host']).strip()
                if cfg.get('proxy_port'):
                    try: self.proxy_port = int(cfg['proxy_port'])
                    except (TypeError, ValueError): pass
                if cfg.get('default_quality') in ('lossless', 'high', 'standard'):
                    self.default_quality = cfg['default_quality']
                if isinstance(cfg.get('qualities'), list):
                    custom = []
                    for q in cfg['qualities']:
                        try:
                            brs = q[1] if isinstance(q[1], list) else [q[1]]
                            custom.append((str(q[0]), [str(b) for b in brs], str(q[2]) if len(q) > 2 else 'flac'))
                        except Exception:
                            continue
                    if custom: self.audio_tiers = custom
        
        # 读取本地保存的偏好设置
        self._load_settings()

    def _save_settings(self):
        try:
            root = os.environ.get("YS_KUWO_CONFIG_DIR", "").strip()
            if not root:
                try:
                    from com.chaquo.python import Python
                    root = str(Python.getPlatform().getApplication().getFilesDir().getAbsolutePath())
                except Exception:
                    root = os.path.join(os.path.expanduser("~"), ".ys")
            site_key = self.getName()
            digest = hashlib.sha256(site_key.encode("utf-8")).hexdigest()[:20]
            self.settings_path = os.path.join(root, "kuwo", "config-%s.json" % digest)
            
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            data = {
                "lyric_mode": self.lyric_mode,
                "default_quality": self.default_quality
            }
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print("[KW] Save config error:", e)

    def _load_settings(self):
        try:
            root = os.environ.get("YS_KUWO_CONFIG_DIR", "").strip()
            if not root:
                try:
                    from com.chaquo.python import Python
                    root = str(Python.getPlatform().getApplication().getFilesDir().getAbsolutePath())
                except Exception:
                    root = os.path.join(os.path.expanduser("~"), ".ys")
                    
            site_key = self.getName()
            digest = hashlib.sha256(site_key.encode("utf-8")).hexdigest()[:20]
            self.settings_path = os.path.join(root, "kuwo", "config-%s.json" % digest)
            
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "lyric_mode" in data:
                        self.lyric_mode = data["lyric_mode"]
                    if "default_quality" in data:
                        self.default_quality = data["default_quality"]
        except Exception as e:
            print("[KW] Load config error:", e)

    @staticmethod
    def _bool_of(v):
        if isinstance(v, bool): return v
        return str(v).lower() in ('true', '1', 'yes', '是', '开启')

    def isVideoFormat(self, url): pass
    def manualVideoCheck(self): pass

    def _headers(self, referer='https://www.kuwo.cn/'):
        return {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Referer': referer,
        }

    def _get_kw_token(self):
        if self._kw_token:
            return self._kw_token
        try:
            self._session.get('https://www.kuwo.cn/', headers=self._headers(), timeout=8)
            t = self._session.cookies.get('kw_token', '')
            if not t:
                t = self._session.cookies.get('Hm_token', '')
            if not t:
                import random, string
                t = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
            self._kw_token = t
        except Exception:
            import random, string
            self._kw_token = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
        return self._kw_token

    def _headers_with_csrf(self):
        token = self._get_kw_token()
        h = self._headers()
        h['csrf'] = token
        h['Cookie'] = 'kw_token=%s' % token
        return h

    @staticmethod
    def _clean_pic(pic):
        pic = str(pic or '').strip()
        if not pic:
            return ''
        if pic.startswith('//'):
            pic = 'https:' + pic
        elif not pic.lower().startswith('http'):
            pic = 'https://img1.kuwo.cn/star/albumcover/' + pic.lstrip('/')
        return pic.replace('#', '').replace('$', '').strip()

    def _pack_play_item(self, label, rid, pic='', mv=False):
        rid = str(rid or '').strip()
        if rid.startswith('MUSIC_'):
            rid = rid[6:]
        item = '%s$%s' % (label, rid)
        if mv:
            info = getattr(self, '_mv_info_cache', {}).get(rid)
            if info:
                item += self._MV_META_SEP + _b64u(json.dumps(info, ensure_ascii=False, separators=(',', ':')))
        pic = self._clean_pic(pic)
        if pic:
            self._song_pic_cache[rid] = pic
            item += self._PIC_SEP + pic
        return item

    def _split_play_id(self, play_id):
        s = str(play_id or '').strip()
        cover = ''
        if self._PIC_SEP in s:
            s, cover = s.split(self._PIC_SEP, 1)
            cover = cover.strip()
        rid = s[6:] if s.startswith('MUSIC_') else s
        if self._MV_META_SEP in rid:
            rid, encoded = rid.split(self._MV_META_SEP, 1)
            try:
                info = json.loads(_b64u_dec(encoded))
                if isinstance(info, dict):
                    self._remember_mv_info(info, rid)
            except (ValueError, TypeError, UnicodeError):
                pass
        if not cover:
            cover = self._song_pic_cache.get(rid, '')
        return rid, cover

    def _fetch_song_pic(self, rid):
        rid = str(rid or '').strip()
        if not rid:
            return ''
        if rid in self._song_pic_cache:
            return self._song_pic_cache[rid]
        pic = ''
        try:
            j = self._session.get(
                'https://wapi.kuwo.cn/api/www/music/musicInfo',
                params={'mid': rid, 'httpsStatus': '1'},
                headers=self._headers(), timeout=5).json()
            if isinstance(j, dict) and j.get('code') == 200:
                d = j.get('data') or {}
                pic = d.get('pic') or d.get('albumpic') or d.get('web_albumpic_short') or ''
        except Exception:
            pass
        pic = self._clean_pic(pic)
        if pic:
            self._song_pic_cache[rid] = pic
        return pic

    def _get_cover_for_keyword(self, keyword):
        if keyword in self._kw_hot_word_cache:
            return self._kw_hot_word_cache[keyword]
        try:
            url = "https://wapi.kuwo.cn/api/www/search/searchMusicBykeyWord"
            r = self._session.get(url, params={'key': keyword, 'pn': 1, 'rn': 1, 'httpsStatus': '1'}, headers=self._headers(), timeout=3)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                raw = data.get('data') or {}
                if isinstance(raw, dict) and raw.get('list'):
                    item = raw['list'][0]
                    pic = item.get('pic') or item.get('web_albumpic_short') or ''
                    if pic:
                        pic = self._clean_pic(pic)
                        self._kw_hot_word_cache[keyword] = pic
                        return pic
        except Exception:
            pass
        try:
            url = "https://search.kuwo.cn/r.s"
            r = self._session.get(url, params={'client': 'kt', 'pn': 0, 'rn': 1, 'all': keyword, 'vipver': '1', 'ft': 'artist', 'encoding': 'utf8', 'rformat': 'json', 'mobi': '1'}, headers=self._headers(), timeout=3)
            data = r.json()
            if isinstance(data, dict) and data.get('abslist'):
                item = data['abslist'][0]
                base = data.get('BASEPICPATH', 'http://img1.kuwo.cn/star/starheads/')
                pic = item.get('hts_PICPATH') or (base + item['PICPATH'] if item.get('PICPATH') else '')
                if pic:
                    pic = self._clean_pic(pic)
                    self._kw_hot_word_cache[keyword] = pic
                    return pic
        except Exception:
            pass
        return ''

    def _remember_mv_info(self, raw, rid=''):
        if not isinstance(raw, dict):
            return {}
        rid = str(rid or raw.get('rid') or raw.get('MUSICRID') or raw.get('musicrid') or raw.get('id') or '').replace('MUSIC_', '')
        if not rid.isdigit():
            return {}
        if not hasattr(self, '_mv_info_cache'):
            self._mv_info_cache = {}
        info = dict(self._mv_info_cache.get(rid) or {})
        pay = raw.get('mvpayinfo') or raw.get('mvPayInfo') or {}
        if isinstance(pay, str):
            try:
                pay = json.loads(pay)
            except ValueError:
                pay = {}
        for vid in (raw.get('vid'), raw.get('mvid'), pay.get('vid') if isinstance(pay, dict) else ''):
            if str(vid or '').isdigit() and int(vid) > 0:
                info['vid'] = str(vid)
                break
        name = raw.get('name') or raw.get('SONGNAME') or raw.get('NAME') or raw.get('songname') or ''
        artist = raw.get('artist') or raw.get('ARTIST') or raw.get('singer') or ''
        if isinstance(artist, list):
            artist = '&'.join(str(a.get('name', '') if isinstance(a, dict) else a) for a in artist)
        if name and str(name) != rid:
            info['name'] = str(name)
        if artist:
            info['artist'] = str(artist)
        self._mv_info_cache[rid] = info
        return info

    @staticmethod
    def _mv_name_key(text):
        return re.sub(r'[\W_]+', '', str(text or '').casefold())

    def _mv_client_info(self, rid):
        info = dict(getattr(self, '_mv_info_cache', {}).get(rid) or {})
        if info.get('vid'):
            return info
        if not info.get('name') or not info.get('artist'):
            info = self._remember_mv_info(self._fetch_song_detail(rid) or {}, rid)
        if not info.get('name') or not info.get('artist'):
            return info
        try:
            response = self._kw_get('https://search.kuwo.cn/r.s', {
                'client': 'kt', 'all': info['name'], 'ft': 'video', 'pn': 0, 'rn': 100,
                'mobi': '1', 'rformat': 'json', 'encoding': 'utf8'}, headers=self._headers(), timeout=6)
            rows = response.json().get('abslist') or []
            matches = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate_rid = str(row.get('MUSICRID') or row.get('rid') or row.get('id') or '').replace('MUSIC_', '')
                candidate = self._remember_mv_info(row, candidate_rid)
                if not candidate.get('vid'):
                    continue
                same_id = candidate_rid == rid
                same_name = self._mv_name_key(candidate.get('name')) == self._mv_name_key(info['name'])
                same_artist = self._mv_name_key(candidate.get('artist')) == self._mv_name_key(info['artist'])
                if same_id or (same_name and same_artist):
                    matches.append((not same_id, candidate))
            if matches:
                matches.sort(key=lambda pair: pair[0])
                info.update(matches[0][1])
                self._mv_info_cache[rid] = info
        except Exception as exc:
            print('[KW MV] VID lookup: %s' % type(exc).__name__)
        return info

    @staticmethod
    def _has_mv(raw):
        if not isinstance(raw, dict):
            return False
        for k in ('hasmv', 'HASMV', 'MVFLAG', 'mvflag', 'has_mv', 'hasmkv', 'HASMKV', 'MKVRID', 'mkvrid'):
            v = raw.get(k)
            if v in (None, ''):
                continue
            try:
                if int(v) > 0:
                    return True
            except (TypeError, ValueError):
                if str(v).lower() in ('true', 'yes'):
                    return True
        for value in (raw, raw.get('mvpayinfo'), raw.get('mvPayInfo')):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, TypeError):
                    continue
            if isinstance(value, dict):
                for key in ('vid', 'mvid'):
                    if str(value.get(key) or '').isdigit() and int(value[key]) > 0:
                        return True
        return False

    def _build_lines(self, entries):
        audio = [self._pack_play_item(l, r, p) for l, r, p, _ in entries]
        froms, urls = [self.LINE_AUDIO], ['#'.join(audio)]
        return '$$$'.join(froms), '$$$'.join(urls)

    @staticmethod
    def _rand_user():
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))

    @staticmethod
    def _br_num(br):
        m = re.match(r'(\d+)', str(br))
        return int(m.group(1)) if m else 0

    def _kw_get(self, url, params, headers=None, timeout=6):
        h = {'User-Agent': 'okhttp/3.10.0'}
        if headers:
            h.update(headers)
        return requests.get(url, params=params, headers=h, timeout=timeout)

    def _track_info(self, rid, br):
        try:
            r = self._kw_get(self._TRACK_API, {
                'f': 'web', 'source': self._TRACK_SOURCE, 'type': 'convert_url_with_sign',
                'rid': rid, 'br': br, 'user': self._rand_user()})
            j = r.json()
            if not isinstance(j, dict) or j.get('code') != 200:
                return None
            d = j.get('data') or {}
            url = str(d.get('url') or '').strip()
            if not url.startswith('http'):
                return None
            try:
                bitrate = int(d.get('bitrate') or 0)
            except (TypeError, ValueError):
                bitrate = 0
            return {'url': url, 'ekey': str(d.get('ekey') or ''), 'bitrate': bitrate,
                    'format': str(d.get('format') or '').lower()}
        except Exception:
            return None

    def _legacy_info(self, rid, br, fmt):
        kbps = self._LEGACY_BR.get(br)
        if not kbps:
            return None
        try:
            r = self._kw_get('https://nmobi.kuwo.cn/mobi.s', {
                'f': 'web', 'user': '0', 'source': 'kwplayer_ar_4.4.2.7_B_nuoweida_vh.apk',
                'type': 'convert_url_with_sign', 'rid': rid, 'bitrate': kbps, 'format': fmt},
                headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 10)', 'Referer': 'https://www.kuwo.cn/'})
            j = r.json()
            if j.get('code') == 200 and (j.get('data') or {}).get('url'):
                d = j['data']
                return {'url': d['url'], 'ekey': '', 'bitrate': kbps, 'format': fmt}
        except Exception:
            pass
        return None

    @staticmethod
    def _url_key(url):
        u = url.split('?', 1)[0]
        m = re.search(r'/resource/(.+)$', u)
        return m.group(1) if m else u

    def _proxy_base(self):
        try:
            port = _kw_proxy_port(self.proxy_port)
        except Exception:
            return ''
        return 'http://%s:%d' % (self.proxy_host or _kw_detect_host(), port)

    def _resolve_audio(self, rid, br, fmt):
        info = self._track_info(rid, br)
        src = 'changenotice'
        if not info:
            info = self._legacy_info(rid, br, fmt)
            src = 'nmobi'
        if not info:
            return '', '', '无返回'
        url = info['url']
        want = self._br_num(br)
        if want >= 2000 and info['bitrate'] and info['bitrate'] != want:
            return '', '', '被降级为%s' % info['bitrate']
        is_enc = info['format'] == 'mflac' or url.split('?', 1)[0].lower().endswith('.mflac')
        if is_enc:
            if not info['ekey']:
                return '', '', '加密但缺少ekey'
            base = self._proxy_base()
            if not base:
                return '', '', '解密代理启动失败'
            raw = url.split('?', 1)[0]
            return '%s/kw.flac?u=%s&e=%s' % (base, _b64u(raw), _b64u(info['ekey'])), self._url_key(raw), \
                'mflac %s via %s(解密代理)' % (info['bitrate'], src)
        return url, self._url_key(url), '%s %s via %s' % (info['format'] or fmt, info['bitrate'], src)

    _MV_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    @staticmethod
    def _extract_mv_url(value):
        if isinstance(value, dict):
            for key in ('url', 'mvurl', 'playUrl', 'playurl', 'data'):
                url = Spider._extract_mv_url(value.get(key))
                if url:
                    return url
        elif isinstance(value, list):
            for item in value:
                url = Spider._extract_mv_url(item)
                if url:
                    return url
        elif isinstance(value, str):
            value = value.strip()
            if value.startswith(('{', '[')):
                try:
                    return Spider._extract_mv_url(json.loads(value))
                except (ValueError, TypeError):
                    return ''
            match = re.search(r'(?mi)^url=(https?://[^\r\n]+)', value)
            url = (match.group(1) if match else value).strip()
            if re.fullmatch(r'https?://[^\s<>"\']+', url) and urlparse(url).hostname:
                return url
        return ''

    @staticmethod
    def _mv_video_track(data, container):
        if container == 'mp4':
            pos = data.find(b'hdlr')
            while pos >= 0:
                if data[pos + 12:pos + 16] == b'vide':
                    return True
                pos = data.find(b'hdlr', pos + 4)
            return False
        return b'\x83\x81\x01' in data and bool(re.search(rb'V_[A-Z0-9/_.]+', data))

    def _check_mv_url(self, url, formats):
        path = urlparse(url).path.lower()
        if path.endswith(('.mp3', '.aac', '.flac', '.m4a', '.mflac', '.ogg', '.wma')):
            return False, '接口返回音频，已过滤'
        
        limit = 64 * 1024
        headers = {'User-Agent': self._MV_UA, 'Referer': 'https://www.kuwo.cn/',
                   'Accept-Encoding': 'identity', 'Range': f'bytes=0-{limit-1}'}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(6, 8)) as response:
                if response.status_code not in (200, 206):
                    return False, f'媒体HTTP{response.status_code}'
                content = bytearray()
                for chunk in response.iter_content(16384):
                    content.extend(chunk[:limit - len(content)])
                    if len(content) >= limit:
                        break
                data = bytes(content)
                
                if len(data) >= 12 and data[4:8] == b'ftyp':
                    container = 'mp4'
                elif data.startswith(b'\x1a\x45\xdf\xa3'):
                    container = 'mkv'
                else:
                    return False, '返回内容不是MP4/MKV'
                
                if container not in formats:
                    return False, f'实际格式为{container}'
                    
                verified = {'url': url, 
                            'mime': 'video/mp4' if container == 'mp4' else 'video/x-matroska',
                            'headers': {'User-Agent': self._MV_UA, 'Referer': 'https://www.kuwo.cn/',
                                        'Accept-Encoding': 'identity'}}
                if not hasattr(self, '_mv_verified'):
                    self._mv_verified = {}
                self._mv_verified[url] = verified
                return True, container
        except Exception as exc:
            return False, 'MV校验失败:%s' % type(exc).__name__

    def _resolve_mv(self, rid, br, fmt=None):
        rid = str(rid).strip()
        if rid.startswith('MUSIC_'):
            rid = rid[6:]
        if not rid.isdigit():
            m = re.search(r'(\d+)', rid)
            if m:
                rid = m.group(1)
        if not rid.isdigit():
            return '', '', '无效歌曲ID'
            
        formats = set(str(br).lower().split('|')) & {'mp4', 'mkv'}
        if not formats:
            return '', '', '不支持的MV格式'
        notes = []
        hdr = {'User-Agent': self._MV_UA, 'Referer': 'https://www.kuwo.cn/'}
        
        info = self._mv_client_info(rid)
        if info.get('vid') and 'mp4' in formats:
            try:
                response = self._kw_get(self._MV_CLIENT_API, {
                    'f': 'web', 'type': 'get_url_by_vid', 'vid': info['vid'], 'quality': br,
                    'prod': 'kwplayer_ar_' + self._MV_CLIENT_VERSION,
                    'source': self._MV_CLIENT_SOURCE, 'corp': 'kuwo', 'newver': '3',
                    'vipver': self._MV_CLIENT_VERSION, 'secureScreen': '1', 'p2pid': '1',
                    'h265': '0', 'uid': '0', 'loginUid': '0', 'loginSid': '0'}, headers=hdr, timeout=6)
                data = response.json()
                if response.status_code == 200 and isinstance(data, dict) and data.get('code') == 200:
                    url = self._extract_mv_url(data.get('data'))
                    if url:
                        valid, note = self._check_mv_url(url, formats)
                        if valid:
                            return url, self._url_key(url), 'VID接口(秒播)'
                        notes.append('VID接口:' + note)
                    else:
                        notes.append('VID接口未返回地址')
                else:
                    notes.append('VID接口:%s' % (data.get('msg', data.get('code')) if isinstance(data, dict) else response.status_code))
            except Exception as exc:
                notes.append('VID接口:' + type(exc).__name__)
                
        request_data = {
            'user': self._rand_user(), 'prod': 'kwplayer_ar_' + self._MV_CLIENT_VERSION,
            'corp': 'kuwo', 'newver': '3', 'vipver': self._MV_CLIENT_VERSION,
            'source': self._MV_CLIENT_SOURCE, 'type': 'convert_mv_url2',
            'loginUid': '0', 'rid': rid, 'quality': br, 'network': 'WIFI',
            'mode': 'audition', 'format': 'mp4', 'br': '', 'sig': '',
            'loginSid': '0', 'secureScreen': '1', 'p2pid': '1', 'uid': '0'}
        payload = '&'.join('%s=%s' % item for item in request_data.items())
        endpoints = (('客户端RID', 'https://anymatch.kuwo.cn/mobi.s', {
            'f': 'kuwo', 'q': _kuwo_b64_encrypt(payload)}),)
            
        for tag, url, params in endpoints:
            try:
                r = self._kw_get(url, params, headers=hdr, timeout=6)
                if r.status_code == 200:
                    u = self._extract_mv_url(r.text or '')
                    if u:
                        valid, note = self._check_mv_url(u, formats)
                        if valid:
                            return u, self._url_key(u), tag + '(RID强转)'
                        notes.append(tag + ':' + note)
            except Exception as exc:
                notes.append('%s:%s' % (tag, type(exc).__name__))
                
        return '', '', ' / '.join(notes)

    def _resolve_tiers(self, rid, tiers, is_mv=False):
        def work(tier):
            name, brs, fmt = tier
            note = '无'
            for br in brs:
                if is_mv:
                    url, key, note = self._resolve_mv(rid, br, fmt)
                else:
                    url, key, note = self._resolve_audio(rid, br, fmt)
                if url:
                    return name, url, key, '%s %s <- %s' % (name, br, note)
            return name, '', '', '%s %s <- %s' % (name, ','.join(brs), note)
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(tiers)))) as ex:
            results = list(ex.map(work, tiers))
        seen, kept = set(), []
        for name, url, key, _ in reversed(results):
            if not url or key in seen:
                continue
            seen.add(key)
            kept.append((name, url))
        kept.reverse()
        return kept, [t for _, _, _, t in results]

    def _ordered_tiers(self):
        tiers = list(self.audio_tiers or self.AUDIO_TIERS)
        q = getattr(self, 'default_quality', 'lossless')

        def is_master(t):
            return '母带' in t[0]

        def is_surround(t):
            return '环绕' in t[0]

        def is_normal_lossless(t):
            return ('flac' in t[2].lower() or '无损' in t[0]) and not is_master(t) and not is_surround(t)

        def is_320(t):
            return '320' in t[1][0]

        def is_128(t):
            return '128' in t[1][0]

        if q == 'lossless':
            first = [t for t in tiers if is_normal_lossless(t)]
            second = [t for t in tiers if is_master(t)]
            third = [t for t in tiers if is_surround(t)]
            last = [t for t in tiers if not (is_normal_lossless(t) or is_master(t) or is_surround(t))]
            return first + second + third + last

        elif q == 'high':
            first = [t for t in tiers if is_320(t)]
            second = [t for t in tiers if is_normal_lossless(t)]
            third = [t for t in tiers if is_master(t) or is_surround(t)]
            last = [t for t in tiers if not (is_320(t) or is_normal_lossless(t) or is_master(t) or is_surround(t))]
            return first + second + third + last

        else:
            first = [t for t in tiers if is_128(t)]
            second = [t for t in tiers if is_320(t)]
            third = [t for t in tiers if is_normal_lossless(t)]
            last = [t for t in tiers if is_master(t) or is_surround(t)]
            return first + second + third + last

    def _mv_available(self, rid):
        url, _, _ = self._resolve_mv(rid, 'mp4|mkv')
        return bool(url)

    def _player_mv(self, rid, cover):
        qualities, trace = self._resolve_tiers(rid, self.MV_TIERS, True)
        if not qualities:
            return {"parse": 0, "playUrl": "", "url": "", "header": {}, "lrc": ""}
        _, url = qualities[0]
        info = self._mv_verified[url]
        
        clean_url = url.split('?')[0]
        
        return {
            "parse": 0, 
            "jx": 0, 
            "playUrl": "", 
            "url": clean_url, 
            "format": "mp4",
            "header": dict(info['headers']),
            "lrc": ""
        }

    def homeContent(self, filter):
        return {
            'class': [
                {"type_name": "搜索歌曲", "type_id": "kw_search"},
                {"type_name": "排行榜", "type_id": "kw_bang_list"},
                {"type_name": "歌单", "type_id": "kw_gedan_list"},
                {"type_name": "歌手", "type_id": "kw_artist"},
                {"type_name": "MV", "type_id": "kw_mv"},
                {"type_name": "歌词设置", "type_id": "kw_settings"},
            ],
            'filters': {
                "kw_gedan_list": [{
                    "key": "order", "name": "排序",
                    "value": [
                        {"n": "最热", "v": "hot"},
                        {"n": "最新", "v": "new"},
                        {"n": "推荐", "v": "rec"},
                    ]
                }],
                "kw_artist": [{
                    "key": "area", "name": "分类",
                    "value": [
                        {"n": "全部", "v": "0"},
                        {"n": "华语男", "v": "1"},
                        {"n": "华语女", "v": "2"},
                        {"n": "华语组合", "v": "3"},
                        {"n": "日韩男", "v": "4"},
                        {"n": "日韩女", "v": "5"},
                        {"n": "日韩组合", "v": "6"},
                        {"n": "欧美男", "v": "7"},
                        {"n": "欧美女", "v": "8"},
                        {"n": "欧美组合", "v": "9"},
                        {"n": "其他", "v": "0"},
                    ]
                }],
                "kw_mv": [{
                    "key": "area", "name": "分类",
                    "value": [
                        {"n": "MV排行榜", "v": "bang"},
                        {"n": "MV歌单", "v": "gedan"},
                        {"n": "全部", "v": "0"},
                        {"n": "华语男", "v": "1"},
                        {"n": "华语女", "v": "2"},
                        {"n": "华语组合", "v": "3"},
                        {"n": "日韩男", "v": "4"},
                        {"n": "日韩女", "v": "5"},
                        {"n": "日韩组合", "v": "6"},
                        {"n": "欧美男", "v": "7"},
                        {"n": "欧美女", "v": "8"},
                        {"n": "欧美组合", "v": "9"},
                    ]
                }]
            }
        }

    def homeVideoContent(self):
        return {"list": []}

    def _kw_settings_page(self):
        mode = 'word' if self.lyric_mode == 'word' else 'line'
        mode_name = '逐字歌词' if mode == 'word' else '逐句歌词'
        mode_desc = ('每个字独立时间轴，卡拉OK效果，加载稍慢' if mode == 'word'
                     else '每行一个时间点，传统LRC，加载快、兼容性好')
        other = 'line' if mode == 'word' else 'word'
        other_name = '逐句歌词' if other == 'line' else '逐字歌词'

        quality_map = {
            'lossless': ('无损优先', 'FLAC 无损 / Hi-Res，音质最好'),
            'high': ('高品质 320K', 'MP3 320K，体积小、加载快'),
            'standard': ('标准 128K', 'MP3 128K，最省流量'),
        }
        q_now = getattr(self, 'default_quality', 'lossless')
        q_name, q_desc = quality_map[q_now]

        items = [
            {
                "vod_id": "kw_set_title",
                "vod_name": "🎵 歌词显示设置",
                "vod_pic": "https://img.icons8.com/color/96/settings--v1.png",
                "vod_remarks": "点击下方按钮切换",
                "vod_tag": "folder",
                "cate": {"land": 1, "ratio": 1.0},
            },
            {
                "vod_id": json.dumps({"actionId": "kw_toggle_lyric_mode"}, ensure_ascii=False),
                "vod_name": "当前：%s（点击切换为%s）" % (mode_name, other_name),
                "vod_pic": ("https://img.icons8.com/color/96/microphone.png"
                            if mode == 'word' else
                            "https://img.icons8.com/color/96/music.png"),
                "vod_remarks": mode_desc,
                "vod_tag": "action",
            },
            {
                "vod_id": "kw_set_title_quality",
                "vod_name": "🎧 默认音质设置",
                "vod_pic": "https://img.icons8.com/color/96/headphones.png",
                "vod_remarks": "当前：%s" % q_name,
                "vod_tag": "folder",
                "cate": {"land": 1, "ratio": 1.0},
            },
            {
                "vod_id": json.dumps({"actionId": "kw_quality_lossless"}, ensure_ascii=False),
                "vod_name": ("✅ " if q_now == 'lossless' else "") + "无损优先",
                "vod_pic": "https://img.icons8.com/color/96/cd.png",
                "vod_remarks": "FLAC 无损 / Hi-Res",
                "vod_tag": "action",
            },
            {
                "vod_id": json.dumps({"actionId": "kw_quality_high"}, ensure_ascii=False),
                "vod_name": ("✅ " if q_now == 'high' else "") + "高品质 320K",
                "vod_pic": "https://img.icons8.com/color/96/music.png",
                "vod_remarks": "MP3 320K",
                "vod_tag": "action",
            },
            {
                "vod_id": json.dumps({"actionId": "kw_quality_standard"}, ensure_ascii=False),
                "vod_name": ("✅ " if q_now == 'standard' else "") + "标准 128K",
                "vod_pic": "https://img.icons8.com/color/96/audio-file.png",
                "vod_remarks": "MP3 128K",
                "vod_tag": "action",
            },
        ]
        return {
            'list': items,
            'page': 1,
            'pagecount': 1,
            'limit': len(items),
            'total': len(items),
        }

    def categoryContent(self, tid, pg, filter, extend):
        tid_s = str(tid)

        if tid_s == 'kw_settings':
            return self._kw_settings_page()
        if tid_s == 'kw_search':
            return self._kw_search_root(pg)
        if tid_s.startswith('kw_hot_word::'):
            self._kw_search_keyword = unquote(tid_s[len('kw_hot_word::'):])
            return self._kw_search_root(pg)

        if tid_s == 'kw_bang_list':
            return self._cat_bang_list(pg)
        if tid_s == 'kw_gedan_list':
            order = 'hot'
            if isinstance(extend, dict) and extend.get('order'):
                order = str(extend['order'])
            return self._cat_gedan_list(pg, order)
            
        if tid_s == 'kw_artist':
            area = '0'
            if isinstance(extend, dict) and extend.get('area'):
                area = str(extend['area'])
            return self._cat_artist_list(pg, area)

        if tid_s == 'kw_mv':
            area = '0'
            if isinstance(extend, dict) and extend.get('area'):
                area = str(extend['area'])
            
            if area == 'bang':
                return self._cat_bang_list(pg, mv=True)
            elif area == 'gedan':
                return self._cat_gedan_list(pg, 'hot', mv=True)
            else:
                return self._cat_artist_list(pg, area, mv=True)

        return {'list': [], 'page': pg, 'pagecount': 9999, 'limit': 90, 'total': 999999}

    def _cat_artist_list(self, pg, area='0', mv=False):
        result = {'list': [], 'page': pg, 'pagecount': 9999, 'limit': 90, 'total': 999999}
        url = f"https://wapi.kuwo.cn/api/www/artist/artistInfo?category={area}&prefix=&pn={pg}&rn=30"
        try:
            data = self._session.get(url, headers=self._headers(), timeout=10).json()
            videos = []
            if data.get('data') and data['data'].get('artistList'):
                for item in data['data']['artistList']:
                    vid = str(item.get('id', ''))
                    if mv:
                        vid = "mvartist_" + vid
                    videos.append({
                        "vod_id": vid,
                        "vod_name": item.get('name', ''),
                        "vod_pic": item.get('pic300') or item.get('pic') or item.get('pic120', ''),
                        "vod_remarks": ""
                    })
            result['list'] = videos
        except Exception as e:
            print(f"[ARTIST CAT] EXC {e}")
        return result

    def _cat_bang_list(self, pg, mv=False):
        result = {'list': [], 'page': pg, 'pagecount': 1, 'limit': 0, 'total': 0}
        items = []
        
        try:
            r = self._session.get(
                "https://wapi.kuwo.cn/api/www/bang/bang/bangMenu",
                headers=self._headers(), timeout=10)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                raw = data.get('data') or []
                if isinstance(raw, list):
                    for group in raw:
                        if isinstance(group, dict) and isinstance(group.get('list'), list):
                            for it in group['list']:
                                if isinstance(it, dict):
                                    items.append(it)
        except Exception as e:
            print(f"[BANG LIST] EXC {e}")

        videos = []
        for it in items:
            sourceid = str(it.get('sourceid') or '').strip()
            if not sourceid:
                continue
            name = (it.get('name') or '排行榜').strip()
            intro = (it.get('intro') or '').strip()
            vid = "bang_%s::%s" % (sourceid, quote(name))
            if mv:
                vid = "mvbang_" + vid
            videos.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": it.get('pic') or '',
                "vod_remarks": intro[:25] if intro else '排行榜',
            })
        result['list'] = videos
        result['limit'] = len(videos)
        result['total'] = len(videos)
        return result

    def _cat_gedan_list(self, pg, order='hot', mv=False):
        try:
            page = int(pg)
        except (TypeError, ValueError):
            page = 1
        if page < 1:
            page = 1
        result = {'list': [], 'page': page, 'pagecount': 99, 'limit': 30, 'total': 999999}
        items = []

        candidates = [
            ("https://wapi.kuwo.cn/api/pc/classify/playlist/getRcmPlayList",
             {'pn': page, 'rn': 30, 'order': order, 'tagId': '', 'total': 0},
             False),
            ("https://kuwo.cn/api/www/rcm/index/playlist",
             {'httpsStatus': '1'},
             True),
            ("https://kuwo.cn/api/www/classify/playlist/getRcmPlayList",
             {'pn': page, 'rn': 30, 'order': order, 'httpsStatus': '1'},
             True),
        ]
        for url, params, need_csrf in candidates:
            if items:
                break
            try:
                h = self._headers_with_csrf() if need_csrf else self._headers()
                r = self._session.get(url, params=params, headers=h, timeout=10)
                data = r.json()
                if isinstance(data, dict):
                    raw = data.get('data') or {}
                    if isinstance(raw, dict):
                        items = raw.get('list') or raw.get('data') or []
                    elif isinstance(raw, list):
                        items = raw
            except Exception:
                continue

        videos = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = (it.get('id') or it.get('playlistid') or it.get('pid')
                   or it.get('itemid') or '')
            pid = str(pid).strip()
            if pid.startswith('MUSIC_'):
                pid = pid[6:]
            if not pid:
                continue
            name = (it.get('name') or '歌单').strip()
            desc_bits = []
            if it.get('uname'):
                desc_bits.append(str(it['uname']))
            if it.get('listenerNum') or it.get('playCount'):
                desc_bits.append('%s次播放' % (it.get('listenerNum') or it.get('playCount')))
            if it.get('total'):
                desc_bits.append('%s首' % it['total'])
            vid = "gedan_%s::%s" % (pid, quote(name))
            if mv:
                vid = "mvgedan_" + vid
            videos.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": it.get('pic') or it.get('img') or '',
                "vod_remarks": ' · '.join(desc_bits) or '歌单',
            })
        result['list'] = videos
        return result

    def _kw_search_root(self, pg):
        try:
            page = max(int(pg), 1)
        except (TypeError, ValueError):
            page = 1
        keyword = str(getattr(self, '_kw_search_keyword', '') or '').strip()

        items = []

        search_input = {
            "vod_id": json.dumps({
                "actionId": "kw_input_search",
                "type": "multiInput",
                "title": "搜索歌曲",
                "msg": "当前平台：酷我音乐",
                "input": [
                    {"id": "keyword", "name": "关键词",
                     "tip": "例如：周杰伦、晴天、七里香",
                     "value": keyword}
                ],
            }, ensure_ascii=False),
            "vod_name": "🔍 输入关键词搜索 (酷我音乐)",
            "vod_pic": "https://img.icons8.com/color/96/search--v1.png",
            "vod_remarks": "当前平台：酷我音乐，点击输入搜索词",
            "vod_tag": "action",
        }
        items.append(search_input)

        if keyword:
            songs_mv = self._search_songs_and_mv(keyword, page)
            for s in songs_mv:
                remarks = ' · '.join(x for x in (s['artist'], s['album'], s['duration']) if x) or '歌曲'
                play_id = s['play_id']
                if s['pic'] and self._PIC_SEP not in play_id:
                    play_id += self._PIC_SEP + s['pic']
                    self._song_pic_cache[s['rid']] = s['pic']
                
                name_prefix = "🎬 [MV] " if s.get('is_mv') else ""
                items.append({
                    "vod_id": play_id,
                    "vod_name": name_prefix + s['name'],
                    "vod_pic": s['pic'],
                    "vod_remarks": remarks,
                    "vod_actor": s['artist'],
                    "vod_content": remarks,
                    "style": {"type": "rect", "ratio": 1.0},
                })
            if len(items) == 1:
                items.append({
                    "vod_id": "kw_no_result",
                    "vod_name": "😅 未找到相关歌曲或MV",
                    "vod_remarks": "换个关键词试试",
                    "vod_tag": "action",
                    "vod_pic": "",
                })

            result = {
                'list': items,
                'page': page,
                'pagecount': 9999 if len(songs_mv) >= 30 else page,
                'limit': 30,
                'total': (page - 1) * 30 + len(songs_mv)
            }
        else:
            if page == 1:
                hot_words = self._kw_hot_words()
                if hot_words:
                    items.append({
                        "vod_id": "kw_hot_title",
                        "vod_name": "🔥 热门搜索 [酷我音乐]",
                        "vod_pic": "https://img.icons8.com/color/96/fire-element.png",
                        "vod_remarks": "点击关键词快速搜索",
                        "vod_tag": "folder",
                        "cate": {"land": 1, "ratio": 1.0},
                    })
                    for word in hot_words:
                        pic = self._get_cover_for_keyword(word)
                        items.append({
                            "vod_id": "kw_hot_word::%s" % quote(word),
                            "vod_name": word,
                            "vod_pic": pic if pic else "",
                            "vod_remarks": "在酷我音乐 搜索",
                            "vod_tag": "folder",
                            "cate": {"land": 1, "ratio": 1.78},
                        })

            result = {
                'list': items,
                'page': page,
                'pagecount': 1,
                'limit': max(len(items), 1),
                'total': len(items)
            }

        return result

    def _kw_hot_words(self):
        words = []
        try:
            r = self._session.get(
                "https://wapi.kuwo.cn/api/www/search/searchKey",
                params={'key': '', 'httpsStatus': '1'},
                headers=self._headers(), timeout=6)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                raw = data.get('data') or {}
                if isinstance(raw, dict):
                    lst = raw.get('list') or raw.get('keyList') or []
                    for it in lst:
                        if isinstance(it, str):
                            words.append(it)
                        elif isinstance(it, dict):
                            w = it.get('key') or it.get('name') or it.get('word')
                            if w:
                                words.append(str(w))
        except Exception as e:
            print(f"[HOT WORDS] EXC {e}")

        if not words:
            words = ["周杰伦", "林俊杰", "邓紫棋", "告白气球", "晴天",
                     "孤勇者", "起风了", "海阔天空", "夜曲", "稻香"]
        return words[:20]

    def _search_songs_and_mv(self, keyword, pg=1):
        try:
            page = max(int(pg), 1)
        except (TypeError, ValueError):
            page = 1
        keyword = str(keyword or '').strip()
        if not keyword:
            return []

        results = []
        
        songs = []
        try:
            url = "https://wapi.kuwo.cn/api/www/search/searchMusicBykeyWord"
            r = self._session.get(url, params={
                'key': keyword, 'pn': page, 'rn': 15, 'httpsStatus': '1',
            }, headers=self._headers(), timeout=10)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                raw = data.get('data') or {}
                if isinstance(raw, dict):
                    songs = raw.get('list') or []
        except Exception as e:
            print(f"[SEARCH-SONG-1] EXC {e}")
            
        if not songs:
            try:
                url = "https://search.kuwo.cn/r.s"
                r = self._session.get(url, params={
                    'client': 'kt', 'pn': (page - 1) * 15, 'rn': 15,
                    'all': keyword, 'vipver': '1', 'ft': 'music',
                    'encoding': 'utf8', 'rformat': 'json', 'mobi': '1',
                }, headers=self._headers(), timeout=10)
                data = r.json()
                if isinstance(data, dict):
                    songs = data.get('abslist') or []
            except Exception as e:
                print(f"[SEARCH-SONG-2] EXC {e}")

        for raw_song in songs:
            s = self._parse_search_item(raw_song)
            if s:
                s['play_id'] = 'song_%s' % s['rid']
                s['is_mv'] = False
                results.append(s)

        try:
            r = self._kw_get('https://search.kuwo.cn/r.s', {
                'client': 'kt', 'all': keyword, 'ft': 'video', 'pn': page - 1, 'rn': 15,
                'mobi': '1', 'rformat': 'json', 'encoding': 'utf8'}, headers=self._headers(), timeout=10)
            mvs = r.json().get('abslist') or []
            for raw_mv in mvs:
                s = self._parse_search_item(raw_mv)
                if s:
                    s['play_id'] = 'mv_%s' % s['rid']
                    s['is_mv'] = True
                    results.append(s)
        except Exception as e:
            print(f"[SEARCH-MV] EXC {e}")

        return results

    def _parse_search_item(self, raw):
        if not isinstance(raw, dict):
            return None
        rid = (raw.get('rid') or raw.get('musicrid') or raw.get('DC_TARGETID')
               or raw.get('MUSICRID') or raw.get('id') or '')
        rid = str(rid).strip()
        if rid.startswith('MUSIC_'):
            rid = rid[6:]
        if not rid:
            return None

        name = (raw.get('name') or raw.get('songname') or raw.get('SONGNAME')
                or raw.get('title') or '未知歌曲')
        name = re.sub(r'[$#]', '', str(name)).strip() or '未知歌曲'

        artist = raw.get('artist') or raw.get('ARTIST') or raw.get('singer') or ''
        if isinstance(artist, list):
            parts = []
            for a in artist:
                parts.append(str(a.get('name') or '') if isinstance(a, dict) else str(a))
            artist = '、'.join(p for p in parts if p)
        artist = re.sub(r'[$#]', '', str(artist).strip())

        album = raw.get('album') or raw.get('ALBUM') or raw.get('albumname') or ''
        if isinstance(album, dict):
            album = album.get('name') or ''
        album = re.sub(r'[$#]', '', str(album).strip())

        pic = (raw.get('pic') or raw.get('hts_PICPATH') or raw.get('albumpic')
               or raw.get('img') or raw.get('web_albumpic_short') or '')
        if isinstance(pic, dict):
            pic = pic.get('url') or ''
        pic = self._clean_pic(str(pic))

        duration = raw.get('duration') or raw.get('DURATION') or ''
        try:
            sec = int(duration)
            duration = '%02d:%02d' % (sec // 60, sec % 60)
        except (TypeError, ValueError):
            duration = ''

        self._remember_mv_info(raw, rid)
        return {
            'rid': rid, 'name': name, 'artist': artist,
            'album': album, 'pic': pic, 'duration': duration,
        }

    def _search_songs_result(self, keyword, pg):
        result = {'list': [], 'page': pg, 'pagecount': 9999, 'limit': 30, 'total': 0}
        try:
            page = max(int(pg), 1)
        except (TypeError, ValueError):
            page = 1
        result['page'] = page

        songs_mv = self._search_songs_and_mv(keyword, page)
        videos = []
        for s in songs_mv:
            remarks = ' · '.join(x for x in (s['artist'], s['album'], s['duration']) if x) or '歌曲'
            play_id = s['play_id']
            if s['pic'] and self._PIC_SEP not in play_id:
                play_id += self._PIC_SEP + s['pic']
                self._song_pic_cache[s['rid']] = s['pic']
            
            name_prefix = "🎬 [MV] " if s.get('is_mv') else ""
            videos.append({
                'vod_id': play_id,
                'vod_name': name_prefix + s['name'],
                'vod_pic': s['pic'],
                'vod_remarks': remarks,
                'vod_actor': s['artist'],
                'vod_content': remarks,
                'style': {'type': 'rect', 'ratio': 1.0},
            })
        result['list'] = videos
        result['limit'] = max(len(videos), 1)
        result['total'] = len(videos)
        return result

    def _search_artists(self, key, pg=1):
        result = {'list': [], 'page': pg, 'pagecount': 9999, 'limit': 30, 'total': 999999}
        wd = quote(key)
        try:
            page_num = (int(pg) - 1) * 30
        except (TypeError, ValueError):
            page_num = 0
        url = f"https://search.kuwo.cn/r.s?client=kt&pn={page_num}&rn=30&all={wd}&vipver=1&ft=artist&encoding=utf8&rformat=json&mobi=1"
        try:
            data = self._session.get(url, headers=self._headers(), timeout=10).json()
            videos = []
            if data.get('abslist'):
                base = data.get('BASEPICPATH', 'http://img1.kuwo.cn/star/starheads/')
                for item in data['abslist']:
                    aid = item.get('ARTISTID') or item.get('DC_TARGETID', '')
                    pic = item.get('hts_PICPATH') or (base + item['PICPATH'] if item.get('PICPATH') else '')
                    videos.append({
                        "vod_id": str(aid), "vod_name": item.get('ARTIST', ''),
                        "vod_pic": pic, "vod_remarks": f"歌曲 :  {item.get('SONGNUM', 0)}首"
                    })
            result['list'] = videos
            result['limit'] = max(len(videos), 1)
            result['total'] = len(videos)
        except Exception as e:
            print(f"[ARTIST SEARCH] EXC {e}")
        return result

    def _fetch_song_detail(self, rid, cover=''):
        rid = str(rid or '').strip()
        if rid.startswith('MUSIC_'):
            rid = rid[6:]
        if not rid:
            return None
        try:
            url = "https://wapi.kuwo.cn/api/www/music/musicInfo"
            r = self._session.get(url, params={'mid': rid, 'httpsStatus': '1'},
                                  headers=self._headers(), timeout=8)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                info = data.get('data') or {}
                if isinstance(info, dict) and (info.get('name') or info.get('rid')):
                    info.setdefault('rid', rid)
                    if cover and not (info.get('pic') or info.get('albumpic')):
                        info['pic'] = cover
                    return info
        except Exception as e:
            print(f"[SONG INFO] EXC {e}")
        if cover:
            return {'rid': rid, 'name': rid, 'artist': '', 'album': '', 'pic': cover}
        return None

    def action(self, action_str):
        try:
            payload = json.loads(action_str) if isinstance(action_str, str) else action_str
            action_id = (payload.get("action") or payload.get("actionId") or "") if isinstance(payload, dict) else str(action_str)
            value = payload.get("value", {}) if isinstance(payload, dict) else {}
        except (ValueError, TypeError):
            action_id = str(action_str or "")
            value = {}

        if action_id == "kw_toggle_lyric_mode":
            self.lyric_mode = 'line' if self.lyric_mode == 'word' else 'word'
            self._save_settings()  # 保存设置
            name = '逐字歌词' if self.lyric_mode == 'word' else '逐句歌词'
            return {
                "action": {"actionId": "__refresh_list__"},
                "toast": "✅ 已切换为：%s" % name,
            }

        if action_id in ("kw_quality_lossless", "kw_quality_high", "kw_quality_standard"):
            self.default_quality = action_id.replace("kw_quality_", "")
            self._save_settings()  # 保存设置
            name = {"lossless": "无损优先", "high": "高品质 320K", "standard": "标准 128K"}[self.default_quality]
            return {
                "action": {"actionId": "__refresh_list__"},
                "toast": "✅ 默认音质已切换为：%s" % name,
            }

        if action_id == "kw_input_search":
            keyword = ""
            input_list = value.get("input")
            if isinstance(input_list, list):
                for item in input_list:
                    if item.get("id") == "keyword":
                        keyword = str(item.get("value") or "").strip()
            if not keyword:
                keyword = str(value.get("keyword") or "").strip()
            self._kw_search_keyword = keyword
            return {
                "action": {"actionId": "__refresh_list__"},
                "toast": "🔍 搜索: %s" % keyword if keyword else "请输入关键词",
            }
            
        return {"action": {"actionId": "__refresh_list__"}}

    def detailContent(self, ids):
        rid = str(ids[0]) if isinstance(ids, (list, tuple)) and ids else str(ids or '')

        if rid.startswith('mvbang_'):
            bang_id = rid[7:].split('::', 1)[0]
            songs = self._fetch_mv_list_by_keyword("热门MV", 1, 30)
            return self._wrap_mv_songs('MV排行榜', songs)

        if rid.startswith('mvgedan_'):
            pid = rid[8:].split('::', 1)[0]
            if pid.startswith('gedan_'):
                pid = pid[6:]
            songs = self._fetch_mv_list_by_keyword("经典MV", 1, 30)
            return self._wrap_mv_songs('MV歌单', songs)

        if rid.startswith('mv_'):
            body = rid[3:]
            mid, cover = self._split_play_id(body)
            mv_info = self._mv_client_info(mid)
            name = mv_info.get('name') or '未知MV'
            artist = mv_info.get('artist') or '未知歌手'
            return {'list': [{
                "vod_id": rid, 
                "vod_name": name,
                "vod_pic": cover or self._fetch_song_pic(mid),
                "vod_content": f"MV：{name} - {artist}",
                "vod_remarks": f"🎬 纯MV线路 · {artist}",
                "vod_actor": artist, 
                "vod_play_from": "MV", 
                "vod_play_url": f"{name}${rid}",
            }]}

        if rid.startswith('mvartist_'):
            aid = rid[9:]
            artist_name = "未知歌手"
            all_songs = []
            try:
                info_url = f"https://wapi.kuwo.cn/api/www/artist/artist?artistid={aid}"
                info_data = self._session.get(info_url, headers=self._headers(), timeout=10).json().get('data', {})
                artist_name = info_data.get('name', '未知歌手')
                
                r = self._kw_get('https://search.kuwo.cn/r.s', {
                    'client': 'kt', 'all': artist_name, 'ft': 'video', 'pn': 0, 'rn': 50,
                    'mobi': '1', 'rformat': 'json', 'encoding': 'utf8'}, headers=self._headers(), timeout=10)
                all_songs = r.json().get('abslist') or []
            except Exception as e:
                print(f"[MV ARTIST] {e}")
            
            return self._wrap_mv_songs(artist_name, all_songs)

        if rid.startswith('song_'):
            body = rid[5:]
            sid, cover = self._split_play_id(body)
            song_raw = self._fetch_song_detail(sid, cover)
            if song_raw:
                return self._wrap_songs('酷我搜索', [song_raw])
            return {'list': [{
                "vod_id": rid, "vod_name": '加载失败',
                "vod_content": '无法获取歌曲信息', "vod_remarks": "加载失败",
                "vod_play_from": "酷我音乐", "vod_play_url": "",
            }]}

        if rid.startswith('bang_'):
            bang_id = rid[5:].split('::', 1)[0]
            songs = self._fetch_bang_songs(bang_id, 1, 200)
            return self._wrap_songs('酷我排行榜', songs)

        if rid.startswith('gedan_'):
            pid = rid[6:].split('::', 1)[0]
            songs = self._fetch_gedan_songs(pid)
            return self._wrap_songs('酷我歌单', songs)

        result = {}
        info_url = f"https://wapi.kuwo.cn/api/www/artist/artist?artistid={rid}"
        try:
            info_data = self._session.get(info_url, headers=self._headers(), timeout=10).json().get('data', {})
            artist_name = info_data.get('name', '')
            all_songs = self._get_artist_songs(rid)
            artist_info = re.sub(r'<[^>]+>', '', info_data.get('info', '')).replace('&nbsp;', ' ')
            artist_info = artist_info.replace('\r\n', '\n').replace('\r', '\n').strip()
            if len(all_songs) > 300: all_songs = all_songs[:300]
            entries = []
            for song in all_songs:
                name = re.sub(r'[$#]', '', song.get('name', '')).strip()
                sid = song.get('rid', '')
                album = song.get('album', '')
                pic = song.get('pic', '')
                label = f"{name} - {album}" if album else name
                entries.append((label, sid, pic, song.get('hasmv')))
            play_from, play_url = self._build_lines(entries)
            result['list'] = [{
                "vod_id": rid, "vod_name": artist_name,
                "vod_pic": info_data.get('pic300') or info_data.get('pic', ''),
                "vod_content": artist_info or "暂无歌手简介",
                "vod_remarks": f"歌曲 :   {len(all_songs)}首",
                "vod_actor": artist_name, "vod_play_from": play_from,
                "vod_play_url": play_url
            }]
        except Exception as e:
            result['list'] = [{
                "vod_id": rid, "vod_name": "加载失败",
                "vod_content": f"加载歌手信息失败: {str(e)}",
                "vod_remarks": "加载失败", "vod_actor": "未知",
                "vod_play_from": "酷我音乐", "vod_play_url": ""
            }]
        return result

    def _fetch_mv_list_by_keyword(self, keyword, pn=1, rn=30):
        try:
            r = self._kw_get('https://search.kuwo.cn/r.s', {
                'client': 'kt', 'all': keyword, 'ft': 'video', 'pn': pn - 1, 'rn': rn,
                'mobi': '1', 'rformat': 'json', 'encoding': 'utf8'}, headers=self._headers(), timeout=10)
            return r.json().get('abslist') or []
        except Exception:
            return []

    def _fetch_bang_songs(self, bang_id, pn=1, rn=30):
        try:
            r = self._session.get(
                "https://wapi.kuwo.cn/api/www/bang/bang/musicList",
                params={'bangId': bang_id, 'pn': pn, 'rn': rn, 'httpsStatus': '1'},
                headers=self._headers(), timeout=10)
            data = r.json()
            if isinstance(data, dict) and data.get('code') == 200:
                raw = data.get('data') or {}
                if isinstance(raw, dict):
                    lst = raw.get('musicList') or raw.get('list') or []
                    if lst:
                        return lst
        except Exception:
            pass

        try:
            r = self._session.get(
                'http://kbangserver.kuwo.cn/ksong.s',
                params={'from': 'pc', 'fmt': 'json', 'type': 'bang',
                        'data': 'content', 'id': bang_id, 'pn': pn - 1, 'rn': rn,
                        'isbang': 1, 'show_copyright_off': 0, 'pcmp4': 1,
                        'bangid': 0, 'vipver': 'MUSIC_8.7.7.0_PQ'},
                headers=self._headers('http://www.kuwo.cn/'), timeout=10)
            data = r.json()
            if isinstance(data, dict):
                lst = data.get('musiclist') or data.get('data') or data.get('list') or []
                if lst:
                    return lst
        except Exception:
            pass
        return []

    def _fetch_gedan_songs(self, pid, pn=1, rn=30):
        pid_str = str(pid).strip()
        if pid_str.startswith('MUSIC_'):
            pid_str = pid_str[6:]
        if not pid_str:
            return []

        attempts = [
            ('https://wapi.kuwo.cn/api/www/playlist/playListInfo',
             {'pid': pid_str, 'pn': 1, 'rn': rn, 'httpsStatus': '1'},
             False, ['musicList', 'musiclist', 'list', 'songs']),
            ('https://wapi.kuwo.cn/api/www/playlist/playListInfo',
             {'pid': pid_str, 'pn': 1, 'rn': rn, 'httpsStatus': '1'},
             True, ['musicList', 'musiclist', 'list', 'songs']),
            ('https://kuwo.cn/api/www/playlist/playListInfo',
             {'pid': pid_str, 'pn': 1, 'rn': rn, 'httpsStatus': '1'},
             True, ['musicList', 'musiclist', 'list', 'songs']),
            ('http://nplserver.kuwo.cn/pl.svc',
             {'op': 'getlistinfo', 'pid': pid_str, 'pn': 1, 'rn': rn},
             False, ['musiclist', 'musicList', 'list']),
            ('https://mobi.kuwo.cn/mobi.s',
             {'f': 'web', 'source': 'kwplayer_ar_5.1.0.0_B_jiakong_vh.apk',
              'type': 'playlist', 'data': 'content', 'id': pid_str,
              'pn': 0, 'rn': rn},
             False, ['musiclist', 'musicList', 'list']),
            ('https://mobi.kuwo.cn/mobi.s',
             {'f': 'web', 'source': 'kwplayer_ar_5.1.0.0_B_jiakong_vh.apk',
              'type': 'playlist', 'data': 'content', 'pid': pid_str,
              'pn': 0, 'rn': rn},
             False, ['musiclist', 'musicList', 'list']),
        ]

        for url, params, need_csrf, fields in attempts:
            try:
                h = self._headers_with_csrf() if need_csrf else self._headers()
                r = self._session.get(url, params=params, headers=h, timeout=8)
                if r.status_code != 200:
                    continue
                data = r.json()
                if not isinstance(data, dict):
                    continue
                raw = data.get('data')
                if not isinstance(raw, (dict, list)):
                    raw = data
                if isinstance(raw, dict):
                    for f in fields:
                        v = raw.get(f)
                        if isinstance(v, list) and v:
                            return v[:rn]
                elif isinstance(raw, list) and raw:
                    if raw and isinstance(raw[0], dict) and ('rid' in raw[0] or 'name' in raw[0]):
                        return raw[:rn]
            except Exception:
                continue
        return []

    def _get_artist_songs(self, rid):
        songs = []
        for page in range(1, 11):
            try:
                url = f"https://wapi.kuwo.cn/api/www/artist/artistMusic?artistid={rid}&pn={page}&rn=30"
                data = self._session.get(url, headers=self._headers(), timeout=10).json()
                if data.get('code') == 200:
                    song_list = data.get('data', {}).get('list', [])
                    if not song_list: break
                    for song in song_list:
                        song_name = song.get('name', '').strip()
                        if song_name:
                            self._remember_mv_info(song, song.get('rid', ''))
                            songs.append({
                                'name': song_name, 'rid': song.get('rid', ''),
                                'album': song.get('album', ''),
                                'duration': song.get('duration', ''),
                                'pic': song.get('pic') or song.get('albumpic') or '',
                                'hasmv': 1 if self._has_mv(song) else 0,
                            })
                    if len(songs) >= 300:
                        songs = songs[:300]; break
            except Exception:
                continue
        return songs

    def _wrap_mv_songs(self, from_name, songs):
        entries, first_pic = [], ''
        for raw in (songs or []):
            s = self._parse_song(raw)
            if not s: continue
            if not first_pic and s['pic']:
                first_pic = s['pic']
            label = s['name']
            if s['artist']: label = '%s - %s' % (s['name'], s['artist'])
            label = re.sub(r'[$#]', ' ', label).strip()
            
            play_id = 'mv_%s' % s['rid']
            if s['pic']:
                play_id += self._PIC_SEP + s['pic']
                self._song_pic_cache[s['rid']] = s['pic']
            entries.append((label, play_id, '', False))

        urls = []
        for label, play_id, _, _ in entries:
            urls.append('%s$%s' % (label, play_id))
            
        return {'list': [{
            "vod_id": '', "vod_name": from_name, "vod_pic": first_pic,
            "vod_content": from_name, "vod_remarks": '共%d首MV' % len(entries),
            "vod_actor": '', "vod_play_from": "MV",
            "vod_play_url": '#'.join(urls),
        }]}

    def _fetch_word_lyric(self, rid, fallback_line_lrc=''):
        self.last_word_lyric_trace = []; self.last_raw_lyric_sample = ""; self.last_used_fake = False
        headers = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10)', 'Referer': 'https://www.kuwo.cn/'}
        candidates = [
            ('https-new-apk', f"https://mobi.kuwo.cn/mobi.s?f=web&source=kwplayer_ar_5.1.0.0_B_jiakong_vh.apk&type=lyric&rid={rid}&lrcx=1&vipver=1"),
            ('http-new-apk', f"http://mobi.kuwo.cn/mobi.s?f=web&source=kwplayer_ar_5.1.0.0_B_jiakong_vh.apk&type=lyric&rid={rid}&lrcx=1&vipver=1"),
            ('https-old-apk', f"https://mobi.kuwo.cn/mobi.s?f=web&source=kwplayer_ar_4.4.2.7_B_nuoweida_vh.apk&type=lyric&rid={rid}&lrcx=1&vipver=1"),
            ('nmobi-new-apk', f"https://nmobi.kuwo.cn/mobi.s?f=web&user=0&source=kwplayer_ar_5.1.0.0_B_jiakong_vh.apk&type=lyric&rid={rid}&lrcx=1&vipver=1"),
        ]
        for name, url in candidates:
            try:
                r = self._session.get(url, headers=headers, timeout=6)
            except Exception:
                continue
            body = r.text or ""
            if not self.last_raw_lyric_sample: self.last_raw_lyric_sample = body[:4000]
            if r.status_code != 200 or not body: continue
            lyric = self._parse_lyric_response(body)
            if lyric and _LX_WORD_TIME.search(lyric): return lyric
        if self.fake_word_lyric:
            line_lrc = fallback_line_lrc or self._fetch_line_lyric(rid)
            if line_lrc:
                fake = _derive_word_lyric_from_line(line_lrc)
                if fake and _LX_WORD_TIME.search(fake):
                    self.last_used_fake = True; return fake
        return ''

    def _parse_lyric_response(self, text):
        if not text: return ''
        text = text.lstrip('\ufeff').strip()
        m = re.match(r'^data=(.+)$', text, re.S)
        if m: text = m.group(1).strip()
        try:
            data = json.loads(text)
            extracted = self._find_lyric_in_json(data)
            if extracted: return extracted
        except (ValueError, TypeError):
            pass
        return text

    def _find_lyric_in_json(self, data):
        if isinstance(data, str):
            return data if (_LX_LINE_TIME.search(data) or _LX_WORD_TIME.search(data)) else ''
        if isinstance(data, list):
            for item in data:
                r = self._find_lyric_in_json(item)
                if r: return r
            return ''
        if isinstance(data, dict):
            for key in ('lyric', 'lrc', 'lrclist', 'content', 'data', 'result'):
                if key in data:
                    r = self._find_lyric_in_json(data[key])
                    if r: return r
            for v in data.values():
                r = self._find_lyric_in_json(v)
                if r: return r
        return ''

    def _fetch_line_lyric(self, rid):
        try:
            lrc_api = f"https://kuwo.cn/openapi/v1/www/lyric/getlyric?musicId={rid}"
            lj = self._session.get(lrc_api, headers=self._headers(), timeout=5).json()
            if lj.get('data') and lj['data'].get('lrclist'):
                return "\n".join([
                    f"[{self._format_time(float(i.get('time', 0)))}]{i.get('lineLyric', '')}"
                    for i in lj['data']['lrclist']
                ])
        except Exception:
            pass
        return ''

    def playerContent(self, flag, id, vipFlags):
        s = str(id or '').strip()
        
        if s.startswith('mv_'):
            s = s[3:]
            if self._PIC_SEP in s:
                s = s.split(self._PIC_SEP, 1)[0]
            rid = s
            cover = self._fetch_song_pic(rid)
            return self._player_mv(rid, cover)

        rid, cover = self._split_play_id(id)
        if not cover:
            cover = self._fetch_song_pic(rid)
        if str(flag or '').strip().upper() == self.LINE_MV.upper():
            return self._player_mv(rid, cover)

        qualities, q_trace = self._resolve_tiers(rid, self._ordered_tiers(), False)
        if not qualities:
            return {"parse": 0, "playUrl": "", "url": "", "header": {}, "lrc": "",
                    "pic": cover, "cover": cover, "artwork": cover}
        
        urls = []
        for qn, qu in qualities:
            urls.append(qn); urls.append(qu)

        line_lrc = self._fetch_line_lyric(rid)
        use_word = (self.lyric_mode == 'word') and self.word_lyrics
        word_lrc = ''

        if use_word:
            word_lrc = self._fetch_word_lyric(rid, line_lrc)
            if not line_lrc and word_lrc:
                line_lrc = _lx_line_lyric(word_lrc)
            lrc = _lx_word_lyric(word_lrc, line_lrc or _lx_line_lyric(word_lrc)) or line_lrc
        else:
            if not line_lrc:
                fallback_word = self._fetch_word_lyric(rid, '')
                if fallback_word:
                    line_lrc = _lx_line_lyric(fallback_word)
            lrc = line_lrc

        # 移除了原有的 result["msg"] 调试信息输出
        result = {
            "parse": 0, "playUrl": "", "url": urls, "lrc": lrc,
            "pic": cover, "cover": cover, "artwork": cover,
            "header": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                       "Referer": "https://www.kuwo.cn/"},
        }
        
        # SSA 字幕兼容
        if line_lrc and not (use_word and word_lrc):
            try:
                ssa = self._create_ssa_subtitle(line_lrc)
                b64 = base64.b64encode(ssa.encode('utf-8')).decode('utf-8')
                result["subs"] = [{"name": "5行歌词", "url": f"data:text/x-ssa;base64,{b64}",
                                   "format": "text/x-ssa", "selected": True}]
            except Exception:
                pass
        return result

    def _format_time(self, seconds):
        total_ms = int(round(seconds * 1000))
        return f"{total_ms // 60000:02d}:{total_ms // 1000 % 60:02d}.{total_ms % 1000:03d}"

    def _create_ssa_subtitle(self, lrc_text):
        lines = []
        for line in lrc_text.split('\n'):
            m = re.match(r'\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)', line)
            if m:
                t = m.group(4).strip()
                if t: 
                    ms_part = m.group(3).ljust(3, '0')
                    lines.append({'start': int(m.group(1)) * 60 + int(m.group(2)) + int(ms_part) / 1000.0, 'text': t})
        if not lines: return ""
        ssa = """[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: 1280
PlayResY: 720
Timer: 100.0000
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: WAITING_TOP2,Roboto,55,&H0000FFFF,&H00808080,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,1,2,0,0,180,1
Style: WAITING_TOP1,Roboto,55,&H0000FFFF,&H00808080,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,1,2,0,0,260,1
Style: PLAYING_CENTER,Roboto,60,&H0000FF00,&H00808080,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,2,2,0,0,340,1
Style: PLAYED_BOTTOM1,Roboto,55,&H0000FFFF,&H00808080,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,1,2,0,0,420,1
Style: PLAYED_BOTTOM2,Roboto,55,&H0000FFFF,&H00808080,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,1,2,0,0,500,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        def ft(s): return f"{int(s // 3600)}:{int((s % 3600) // 60):02d}:{int(s % 60):02d}.{int((s * 100) % 100):02d}"
        events = []
        for i in range(len(lines)):
            cur = lines[i]
            cur_end = lines[i + 1]['start'] if i + 1 < len(lines) else cur['start'] + 5.0
            w2 = lines[i + 2] if i + 2 < len(lines) else None
            w1 = lines[i + 1] if i + 1 < len(lines) else None
            p1 = lines[i - 1] if i - 1 >= 0 else None
            p2 = lines[i - 2] if i - 2 >= 0 else None
            s, e = ft(cur['start']), ft(cur_end)
            if w2: events.append(f"Dialogue: 1,{s},{e},WAITING_TOP2,,0,0,0,,{w2['text']}")
            if w1: events.append(f"Dialogue: 2,{s},{e},WAITING_TOP1,,0,0,0,,{w1['text']}")
            events.append(f"Dialogue: 3,{s},{e},PLAYING_CENTER,,0,0,0,,{cur['text']}")
            if p1: events.append(f"Dialogue: 4,{s},{e},PLAYED_BOTTOM1,,0,0,0,,{p1['text']}")
            if p2: events.append(f"Dialogue: 5,{s},{e},PLAYED_BOTTOM2,,0,0,0,,{p2['text']}")
        return ssa + "\n".join(events)

    def _parse_song(self, song):
        if not isinstance(song, dict): return None
        rid = (song.get('rid') or song.get('MUSICRID') or song.get('musicrid') or song.get('id')
               or song.get('DC_TARGETID') or '')
        rid = str(rid)
        if rid.startswith('MUSIC_'): rid = rid[6:]
        if not rid: return None
        name = song.get('name') or song.get('SONGNAME') or song.get('NAME') or song.get('songname') or song.get('title') or '未知'
        name = re.sub(r'[$#]', '', str(name)).strip() or '未知'
        artist = song.get('artist') or song.get('ARTIST') or song.get('singer') or song.get('artists') or ''
        if isinstance(artist, list):
            parts = []
            for a in artist:
                parts.append(str(a.get('name') or '') if isinstance(a, dict) else str(a))
            artist = '、'.join([p for p in parts if p])
        artist = str(artist).strip()
        album = song.get('album') or song.get('albumname') or ''
        if isinstance(album, dict): album = album.get('name') or ''
        album = str(album).strip()
        pic = (song.get('pic') or song.get('albumpic') or song.get('img')
               or song.get('web_albumpic_short') or '')
        if isinstance(pic, dict): pic = pic.get('url') or ''
        self._remember_mv_info(song, rid)
        return {'rid': rid, 'name': name, 'artist': artist, 'album': album,
                'pic': self._clean_pic(pic), 'hasmv': self._has_mv(song)}

    def _wrap_songs(self, from_name, songs):
        entries, first_pic = [], ''
        for raw in (songs or []):
            s = self._parse_song(raw)
            if not s: continue
            if not first_pic and s['pic']:
                first_pic = s['pic']
            label = s['name']
            if s['artist']: label = '%s - %s' % (s['name'], s['artist'])
            label = re.sub(r'[$#]', ' ', label).strip()
            entries.append((label, s['rid'], s['pic'], s['hasmv']))
        play_from, play_url = self._build_lines(entries)
        return {'list': [{
            "vod_id": '', "vod_name": from_name, "vod_pic": first_pic,
            "vod_content": from_name, "vod_remarks": '共%d首' % len(entries),
            "vod_actor": '', "vod_play_from": play_from,
            "vod_play_url": play_url,
        }]}

    def searchContent(self, key, quick, pg=1):
        keyword = str(key or '').strip()
        search_type = 'song'
        for prefix in ('歌手:', '歌手：'):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                search_type = 'singer'
                break
        if search_type == 'singer':
            return self._search_artists(keyword, pg)
        return self._search_songs_result(keyword, pg)

    def searchContentPage(self, key, quick, pg):
        return self.searchContent(key, quick, pg)

    def localProxy(self, param):
        return None