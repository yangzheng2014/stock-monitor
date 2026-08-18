#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票监控软件 v0.3.1（测试版）
===============================

功能：
  - 股票搜索：支持按股票代码或名称搜索（新浪建议接口为主，东财为备用）
  - 单只股票监控（默认三环集团 sz300408，可通过搜索框切换标的）
  - 美股支持：输入美股代码（如 AAPL / MSFT / BABA）即可监控美股，
    行情来自 Yahoo Finance 免费公开接口（无需 key）
  - 日 K 线蜡烛图（前复权），支持 20 / 50 / 200 交易日区间切换
  - 双均线 MA5 / MA20
  - 成交量副图（红涨绿跌，与同花顺配色一致）
  - 实时行情自动刷新（交易时段 5 秒，休息时段 30 秒）
  - A 股实时数据源三级降级：东方财富 → 新浪 → 腾讯（全部免费公开接口）
  - 美股实时/K 线数据源：Yahoo Finance chart API（免费）
  - 历史 K 线本地 CSV 缓存，启动秒开
  - 起始页为项目简介页，左上角搜索框，深色主题美化界面
  - 本地 Web 界面（ECharts 深色主题），浏览器打开 http://127.0.0.1:8765
  - 个股新闻：右侧面板展示当前标的（A股/美股）相关新闻（东方财富搜索，免费接口），可隐藏
  - 涨停/跌停大框：可一键隐藏/展开
  - 收藏：监控页 ☆ 收藏标的，收藏页从主页横向滑动进入（鼠标拖拽 / 触摸滑动）

运行：
  python main_v0.3.1.py                # 默认监控三环集团（sz300408）
  python main_v0.3.1.py 600519         # 启动即监控贵州茅台
  python main_v0.3.1.py AAPL           # 启动即监控苹果（美股）
  python main_v0.3.1.py -p 9000        # 指定端口
  python main_v0.3.1.py --no-browser   # 不自动打开浏览器

依赖：
  pip install flask requests pandas
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, jsonify, request, send_from_directory

# --------------------------------------------------------------
# 基础配置
# --------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
ECHARTS_FILE = os.path.join(STATIC_DIR, "echarts.min.js")
ECHARTS_VERSION = "5.5.1"

COLOR_UP = "#EF232A"     # 上涨红（同花顺风格）
COLOR_DOWN = "#14B143"   # 下跌绿
COLOR_FLAT = "#999999"   # 平盘灰
MA_COLORS = {5: "#F9D71C", 20: "#A967FF"}

DEFAULT_SYMBOL = "300408"

config = {
    "symbol": DEFAULT_SYMBOL,
    "market": "sz",
    "name": "三环集团",
    "kline_ranges": [20, 50, 200],
    "default_range": 50,
    "history_days": 250,
    "ma": [5, 20],
    "refresh_interval_sec": 5,
    "cache_ttl_sec": 86400,
    "port": 8765,
    "fail_threshold": 2,
    "realtime_sources": ["eastmoney", "sina", "tencent"],
}

# 指数监控列表（东方财富 secid；A股 1/0 前缀，国际 100 前缀）
INDICES = [
    {"secid": "1.000001", "code": "000001", "name": "上证指数"},
    {"secid": "0.399001", "code": "399001", "name": "深证成指"},
    {"secid": "0.399006", "code": "399006", "name": "创业板指"},
    {"secid": "1.000300", "code": "000300", "name": "沪深300"},
    {"secid": "100.HSI", "code": "HSI", "name": "恒生指数"},
    {"secid": "100.N225", "code": "N225", "name": "日经225"},
    {"secid": "100.SPX", "code": "SPX", "name": "标普500"},
    {"secid": "100.DJIA", "code": "DJIA", "name": "道琼斯"},
]
INDEX_CACHE: dict = {"ts": 0.0, "payload": None}   # 指数实时缓存（节流，避免频繁请求）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
SINA_HEADERS = {**HEADERS, "Referer": "https://finance.sina.com.cn/"}

for _d in (STATIC_DIR, DATA_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("stockmonitor")

# --------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------
def normalize_symbol(raw: str) -> str | None:
    """归一化股票代码：600519 / 600519.SH / 600519.SS / SH600519 → 600519；
    美股代码：AAPL / aapl / AAPL.US → AAPL"""
    s = raw.strip().upper()
    for suf in (".SH", ".SS", ".SZ", ".US"):
        if s.endswith(suf):
            s = s[:-3]
            break
    if s.startswith(("SH", "SZ")):
        s = s[2:]
    if s.isdigit() and len(s) == 6:
        return s
    if s.isalpha() and 1 <= len(s) <= 5:
        return s
    return None


def market_of(code: str) -> str:
    """6/5/9 开头为沪市，其余为深市；纯字母为美股"""
    if code.isalpha():
        return "us"
    return "sh" if code[0] in ("5", "6", "9") else "sz"


def secid_of(code: str) -> str:
    """东方财富 secid：1=沪市，0=深市"""
    return f"{1 if market_of(code) == 'sh' else 0}.{code}"


def trade_status(now: dt.datetime | None = None) -> str:
    """根据北京时间返回交易状态"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return "休市日"
    hm = now.hour * 100 + now.minute
    if 915 <= hm < 930:
        return "集合竞价"
    if 930 <= hm < 1130 or 1300 <= hm < 1500:
        return "交易中"
    if 1130 <= hm < 1300:
        return "午间休市"
    return "已收盘"


# 美东时区：ZoneInfo 自动处理夏令时切换（3月第二个周日 ~ 11月第一个周日）
ET = ZoneInfo("America/New_York")
EDT = dt.timezone(dt.timedelta(hours=-4))   # 美东夏令时（EDT）
EST = dt.timezone(dt.timedelta(hours=-5))   # 美东标准时间（EST）


def us_et_time(now: dt.datetime | None = None) -> dt.datetime:
    """将时刻换算为美东时间（含夏令时），返回带 tzinfo 的 aware datetime。

    - now 为 None 时取当前 UTC 时间；
    - now 为 naive 时视为 UTC；
    - now 为 aware 时直接换算。
    返回值的 tzinfo 为 EDT（夏令时）或 EST（标准时间），便于区分。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    et = now.astimezone(ET)
    tz = EDT if et.utcoffset() == EDT.utcoffset(None) else EST
    return et.replace(tzinfo=tz)


def us_trade_status(now: dt.datetime | None = None) -> str:
    """美股交易状态：按美东时间（含夏令时）判断。
    盘前 4:00-9:30，正式交易 9:30-16:00，盘后 16:00-20:00，周末休市。"""
    et = us_et_time(now)
    if et.weekday() >= 5:
        return "休市日"
    hm = et.hour * 100 + et.minute
    if 930 <= hm < 1600:
        return "交易中"
    if 400 <= hm < 930:
        return "盘前"
    if 1600 <= hm < 2000:
        return "盘后"
    return "已收盘"


def current_trade_status(now: dt.datetime | None = None) -> str:
    """按当前监控标的所在市场返回交易状态"""
    if market_of(config["symbol"]) == "us":
        return us_trade_status(now)
    return trade_status(now)


def fmt_price(v: float | None) -> str:
    """格式化价格显示：None 返回占位符 “—”，否则保留两位小数。"""
    return "—" if v is None else f"{v:.2f}"


def limit_pct(code: str) -> float:
    """单日涨跌幅容差（%）：创业板(3 开头)/科创板(68 开头)为 20%，主板 10%"""
    return 22.0 if code.startswith("3") or code.startswith("68") else 12.0


def limit_ratio(code: str, name: str = "") -> float:
    """涨跌停幅度：ST 股 ±5%，创业板(30)/科创板(68) ±20%，主板 ±10%"""
    if "ST" in name.upper():
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


# ---------- 涨停/跌停池（东方财富 push2ex） ----------

_limit_pool_cache: dict = {"ts": 0.0, "payload": None}


def parse_pool_item(raw: dict) -> dict:
    """涨停/跌停池单条 → 前端展示字段（p 为价格×1000，fbt 为 HHMMSS）"""
    fbt = raw.get("fbt")
    if isinstance(fbt, int) and fbt > 0:
        fbt_s = f"{fbt // 10000:02d}:{fbt % 10000 // 100:02d}:{fbt % 100:02d}"
    else:
        fbt_s = ""
    return {
        "code": raw.get("c", ""),
        "name": raw.get("n", ""),
        "price": round(raw.get("p", 0) / 1000, 2),
        "pct": round(raw.get("zdp", 0), 2),
        "lbc": raw.get("lbc", 0),
        "fbt": fbt_s,
    }


def pool_cache_age_ok(age: float, trading: bool) -> bool:
    """池缓存是否仍有效：交易中 30 秒，非交易 300 秒"""
    return age <= (30 if trading else 300)


def fetch_limit_pool(date: str) -> dict | None:
    """拉取指定日期涨停/跌停池。非交易日请求会自动返回最近交易日数据（qdate 为实际日期）。"""
    out = {"date": date, "up": [], "down": [], "up_count": 0, "down_count": 0}
    for kind, api, sort in (
        ("up", "getTopicZTPool", "fbt%3Aasc"),
        ("down", "getTopicDTPool", "fund%3Aasc"),
    ):
        url = ("https://push2ex.eastmoney.com/" + api +
               "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0"
               f"&pagesize=100&sort={sort}&date={date}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            data = (resp.json().get("data") or {})
            pool = data.get("pool") or []
        except Exception:
            return None
        out[kind] = [parse_pool_item(it) for it in pool][:60]
        out[kind + "_count"] = data.get("tc", len(pool))
        out["date"] = str(data.get("qdate") or date)
    return out


def validate_bar(bar: dict, limit: float = 12.0) -> dict | None:
    """校验单根 K 线的合法性，非法返回 None"""
    try:
        o, c, h, l = bar["open"], bar["close"], bar["high"], bar["low"]
        v = bar["volume"]
    except (KeyError, TypeError):
        return None
    if not (o > 0 and c > 0 and h > 0 and l > 0 and v >= 0):
        return None
    if l > min(o, c) or h < max(o, c):
        return None
    pct = abs((c - o) / o * 100) if o else 0
    if pct > limit:
        return None
    return bar

# --------------------------------------------------------------
# 数据源：东方财富（历史 K 线，主）
# --------------------------------------------------------------
def fetch_kline_eastmoney(symbol: str, days: int = 250) -> dict | None:
    """拉取最近 *days* 个交易日的前复权日 K 线。

    接口：push2his.eastmoney.com/api/qt/stock/kline/get
    返回 {"code", "name", "bars": [{date,open,close,high,low,volume,amount}, ...]}
    注意：fields2 字段顺序为 f51日期 f52开 f53收 f54高 f55低 f56量 f57额 f58振幅。
    """
    params = {
        "secid": secid_of(symbol),
        "klt": "101",          # 日 K
        "fqt": "1",            # 前复权
        "beg": "19900101",
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params=params, headers=HEADERS, timeout=10,
        )
        data = resp.json()
    except Exception as e:
        logger.warning("[kline][eastmoney] 请求失败: %s", e)
        return None
    payload = (data or {}).get("data")
    if not payload or payload.get("code") != symbol:
        logger.warning("[kline][eastmoney] 响应异常: %s", str(data)[:200])
        return None

    bars = []
    for line in payload.get("klines", []):
        parts = line.split(",")
        if len(parts) < 7:
            continue
        bar = {
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),   # 手
            "amount": float(parts[6]),   # 元
        }
        bar = validate_bar(bar, limit_pct(symbol))
        if bar:
            bars.append(bar)
    if len(bars) < 2:
        logger.warning("[kline][eastmoney] 有效 K 线不足: %d 根", len(bars))
        return None
    return {"code": symbol, "name": payload.get("name", ""), "bars": bars[-days:]}


# --------------------------------------------------------------
# 数据源：腾讯（历史 K 线，备用 1，前复权）
# --------------------------------------------------------------
def fetch_kline_tencent(symbol: str, days: int = 250) -> dict | None:
    """拉取前复权日 K 线（备用）。

    接口：web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,{days},qfq
    响应：data.sh600519.qfqday = [[日期,开,收,高,低,量(手)], ...]（升序）
    无成交额字段，按 量×100×收盘价 估算。
    """
    code = f"{market_of(symbol)}{symbol}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = requests.get(url, params={"param": f"{code},day,,,{days},qfq"},
                            headers=HEADERS, timeout=10)
        d = resp.json().get("data", {}).get(code, {})
        rows = d.get("qfqday") or d.get("day") or []
    except Exception as e:
        logger.warning("[kline][tencent] 请求失败: %s", e)
        return None
    bars = []
    for row in rows:
        if len(row) < 6:
            continue
        bar = validate_bar({
            "date": row[0],
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]),
            "amount": float(row[5]) * 100 * float(row[2]),  # 估算
        }, limit_pct(symbol))
        if bar:
            bars.append(bar)
    if len(bars) < 2:
        logger.warning("[kline][tencent] 有效 K 线不足: %d 根", len(bars))
        return None
    return {"code": symbol, "name": config["name"], "bars": bars[-days:]}


# --------------------------------------------------------------
# 数据源：新浪（历史 K 线，备用 2，前复权）
# --------------------------------------------------------------
def fetch_kline_sina(symbol: str, days: int = 250) -> dict | None:
    """拉取日 K 线（备用，新浪行情为前复权口径）。

    接口：quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
    参数：symbol=sh600519&scale=240&ma=no&datalen={days}
    响应：[{day,open,high,low,close,volume(股)}, ...]（升序）
    """
    code = f"{market_of(symbol)}{symbol}"
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData")
    try:
        resp = requests.get(url, params={"symbol": code, "scale": 240,
                                         "ma": "no", "datalen": days},
                            headers=SINA_HEADERS, timeout=10)
        rows = resp.json()
    except Exception as e:
        logger.warning("[kline][sina] 请求失败: %s", e)
        return None
    bars = []
    for row in rows or []:
        bar = validate_bar({
            "date": row.get("day", ""),
            "open": float(row["open"]),
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "volume": float(row["volume"]) / 100,          # 股 → 手
            "amount": float(row["volume"]) / 100 * 100 * float(row["close"]),  # 估算
        }, limit_pct(symbol))
        if bar:
            bars.append(bar)
    if len(bars) < 2:
        logger.warning("[kline][sina] 有效 K 线不足: %d 根", len(bars))
        return None
    return {"code": symbol, "name": config["name"], "bars": bars[-days:]}


# --------------------------------------------------------------
# 数据源：东方财富（实时行情，主）
# --------------------------------------------------------------
def fetch_realtime_eastmoney(symbol: str) -> dict | None:
    """实时行情：push2.eastmoney.com/api/qt/stock/get
    价格类字段 f43/f44/f45/f46/f60/f169 需按 f59 精度缩放（一般 ÷100）。
    """
    params = {
        "secid": secid_of(symbol),
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86,f169,f170",
    }
    try:
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params=params, headers=HEADERS, timeout=10,
        )
        d = resp.json().get("data") or {}
    except Exception as e:
        logger.warning("[realtime][eastmoney] 请求失败: %s", e)
        return None
    try:
        prec = int(d.get("f59") or 2)
        scale = 10 ** prec
        price = float(d["f43"]) / scale
        if price <= 0:
            return None  # 停牌
    except (KeyError, TypeError, ValueError):
        return None
    ts_ms = d.get("f86") or 0
    ts = dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts_ms else ""
    return {
        "code": str(d.get("f57", symbol)),
        "name": str(d.get("f58", "")),
        "price": price,
        "prev_close": float(d["f60"]) / scale,
        "open": float(d["f46"]) / scale,
        "high": float(d["f44"]) / scale,
        "low": float(d["f45"]) / scale,
        "change": float(d["f169"]) / scale,
        "pct": float(d["f170"]) / 100.0,
        "volume": float(d.get("f47") or 0),   # 手
        "amount": float(d.get("f48") or 0),   # 元
        "timestamp": ts,
    }

# --------------------------------------------------------------
# 数据源：新浪财经（实时行情，备用 1）
# --------------------------------------------------------------
def fetch_realtime_sina(symbol: str) -> dict | None:
    """实时行情：hq.sinajs.cn/list=sh600519，需 Referer，GBK 编码。
    A 股字段：0名称 1今开 2昨收 3现价 4最高 5最低 8成交量(股) 9成交额 30日期 31时间。
    """
    code = f"{market_of(symbol)}{symbol}"
    try:
        resp = requests.get(
            f"https://hq.sinajs.cn/list={code}",
            headers=SINA_HEADERS, timeout=10,
        )
        text = resp.content.decode("gbk", errors="ignore").strip()
        payload = text.split('"', 1)[1].rsplit('"', 1)[0]
        parts = payload.split(",")
        if len(parts) < 32:
            logger.warning("[realtime][sina] 字段不足: %d 项", len(parts))
            return None
        price = float(parts[3])
        if price <= 0:
            return None  # 停牌
        prev = float(parts[2])
        return {
            "code": code,
            "name": parts[0],
            "price": price,
            "prev_close": prev,
            "open": float(parts[1]),
            "high": float(parts[4]),
            "low": float(parts[5]),
            "change": price - prev,
            "pct": (price - prev) / prev * 100 if prev else 0.0,
            "volume": float(parts[8]) / 100,  # 股 → 手
            "amount": float(parts[9]),
            "timestamp": f"{parts[30]} {parts[31]}",
        }
    except Exception as e:
        logger.warning("[realtime][sina] 请求失败: %s", e)
        return None

# --------------------------------------------------------------
# 数据源：腾讯财经（实时行情，备用 2）
# --------------------------------------------------------------
def fetch_realtime_tencent(symbol: str) -> dict | None:
    """实时行情：qt.gtimg.cn/q=sh600519，GBK 编码。
    字段：1名称 3现价 4昨收 5今开 6成交量(手) 30时间 31涨跌 32涨跌幅。
    """
    code = f"{market_of(symbol)}{symbol}"
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={code}", headers=HEADERS, timeout=10)
        payload = resp.content.decode("gbk", errors="ignore").strip()
        parts = payload.split("=", 1)[1].strip('";').split("~")
        if len(parts) < 33:
            logger.warning("[realtime][tencent] 字段不足: %d 项", len(parts))
            return None
        price = float(parts[3])
        if price <= 0:
            return None
        prev = float(parts[4])
        return {
            "code": code,
            "name": parts[1],
            "price": price,
            "prev_close": prev,
            "open": float(parts[5]),
            "high": 0.0,  # 腾讯字段中无最高/最低，留给东财/新浪补齐
            "low": 0.0,
            "change": float(parts[31]),
            "pct": float(parts[32]),
            "volume": float(parts[6]),   # 手
            "amount": 0.0,
            "timestamp": parts[30],
        }
    except Exception as e:
        logger.warning("[realtime][tencent] 请求失败: %s", e)
        return None

# --------------------------------------------------------------
# 数据源：东方财富（指数实时行情，批量）
# --------------------------------------------------------------
def parse_index_diff(diff: list) -> list:
    """将东财 ulist.np 原始 diff 列表映射为按 INDICES 顺序的指数行情。"""
    def _num(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    by_code = {}
    for item in diff:
        code = str(item.get("f12") or "")
        try:
            by_code[code] = {
                "secid": str(item.get("f13") or "") + "." + code,
                "code": code,
                "name": str(item.get("f14") or ""),
                "price": _num(item.get("f2")),
                "change": _num(item.get("f4")),
                "pct": _num(item.get("f3")),
                "open": _num(item.get("f17")),
                "high": _num(item.get("f15")),
                "low": _num(item.get("f16")),
                "prev_close": _num(item.get("f18")),
                "volume": _num(item.get("f5")),
                "amount": _num(item.get("f6")),
                "timestamp": str(item.get("f86") or ""),
            }
        except (TypeError, ValueError):
            continue
    out = []
    for idx in INDICES:
        row = by_code.get(idx["code"])
        if row:
            out.append(row)
    return out


def fetch_indices_realtime() -> list | None:
    """批量获取指数实时行情。

    接口：push2.eastmoney.com/api/qt/ulist.np/get
    字段：f2点位 f3涨跌幅% f4涨跌额 f5成交量 f6成交额 f12代码 f13市场 f14名称
         f15最高 f16最低 f17今开 f18昨收 f86时间戳（-表示非交易时段）
    """
    secids = ",".join(i["secid"] for i in INDICES)
    params = {
        "secids": secids,
        "fields": "f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f86",
        "fltt": "2",
        "invt": "2",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params=params, headers=HEADERS, timeout=10,
        )
        data = resp.json().get("data") or {}
        diff = data.get("diff") or []
    except Exception as e:
        logger.warning("[indices][eastmoney] 请求失败: %s", e)
        return None
    if not diff:
        return None
    return parse_index_diff(diff) or None


def fetch_index_kline(secid: str, days: int = 250) -> dict | None:
    """拉取指数日 K 线（东方财富 push2his，与个股同一接口）。

    参数 secid 直接透传（如 100.HSI / 1.000001），不经过 secid_of 换算。
    """
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "1",
        "beg": "19900101",
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params=params, headers=HEADERS, timeout=10,
        )
        data = resp.json()
    except Exception as e:
        logger.warning("[index-kline][eastmoney] 请求失败: %s", e)
        return None
    payload = (data or {}).get("data")
    if not payload or not payload.get("klines"):
        logger.warning("[index-kline][eastmoney] 响应异常: %s", str(data)[:200])
        return None
    bars = []
    for line in payload.get("klines", []):
        parts = line.split(",")
        if len(parts) < 7:
            continue
        bars.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]),
        })
    if len(bars) < 2:
        return None
    return {
        "code": payload.get("code", secid),
        "name": payload.get("name", ""),
        "bars": bars[-days:],
    }

# --------------------------------------------------------------
# 数据源：新浪财经（美股，免费公开接口）
# --------------------------------------------------------------
def fetch_kline_us_sina(symbol: str, days: int = 250) -> dict | None:
    """拉取美股日 K（原始价，1984 年至今全量，取最近 *days* 根）。

    接口：stock.finance.sina.com.cn/usstock/api/jsonp.php/US_MinKService.getDailyK
    响应：var t=([{"d":日期,"o":开,"h":高,"l":低,"c":收,"v":量(股),"a":额(美元)}, ...])
    """
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
           "var%20t=/US_MinKService.getDailyK")
    try:
        resp = requests.get(url, params={"symbol": symbol.lower()},
                            headers=SINA_HEADERS, timeout=15)
        text = resp.text
        start = text.index("([") + 1
        rows = json.loads(text[start: text.rindex("])") + 1])
    except Exception as e:
        logger.warning("[kline][sina_us] 请求失败: %s", e)
        return None
    bars = []
    for row in rows or []:
        try:
            o, h, l, c = float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"])
            v = float(row.get("v") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        bar = validate_bar({
            "date": row["d"],
            "open": round(o, 4),
            "close": round(c, 4),
            "high": round(h, 4),
            "low": round(l, 4),
            "volume": v,
            "amount": float(row.get("a") or 0),
        }, limit=100.0)   # 美股无涨跌停限制，放宽校验
        if bar:
            bars.append(bar)
    if len(bars) < 2:
        logger.warning("[kline][sina_us][%s] 有效 K 线不足: %d 根", symbol, len(bars))
        return None
    return {"code": symbol, "name": config["name"], "bars": bars[-days:]}


def fetch_realtime_us_sina(symbol: str) -> dict | None:
    """美股实时行情：hq.sinajs.cn/list=gb_aapl，需 Referer，GBK 编码。
    字段：0名称 1现价 2涨跌幅% 3日期时间 4涨跌额 5今开 6最高 7最低 10成交量(股) 21昨收。"""
    try:
        resp = requests.get(
            f"https://hq.sinajs.cn/list=gb_{symbol.lower()}",
            headers=SINA_HEADERS, timeout=10,
        )
        text = resp.content.decode("gbk", errors="ignore").strip()
        payload = text.split('"', 1)[1].rsplit('"', 1)[0]
        parts = payload.split(",")
        if len(parts) < 22:
            logger.warning("[realtime][sina_us] 字段不足: %d 项", len(parts))
            return None
        price = float(parts[1])
        if price <= 0:
            return None  # 停牌
        prev = float(parts[21])
        return {
            "code": symbol,
            "name": parts[0],
            "price": round(price, 4),
            "prev_close": round(prev, 4),
            "open": round(float(parts[5]), 4),
            "high": round(float(parts[6]), 4),
            "low": round(float(parts[7]), 4),
            "change": round(float(parts[4]), 4),
            "pct": float(parts[2]),
            "volume": float(parts[10]),   # 股
            "amount": 0.0,                # 新浪美股无实时成交额字段
            "timestamp": parts[3],
        }
    except Exception as e:
        logger.warning("[realtime][sina_us] 请求失败: %s", e)
        return None

# --------------------------------------------------------------
# 股票搜索（按代码或名称，A 股 + 美股）
# --------------------------------------------------------------
# 内置常用美股列表（免费接口无美股搜索，输入代码也可直接切换）
US_STOCKS = [
    {"code": "AAPL", "name": "苹果"}, {"code": "MSFT", "name": "微软"},
    {"code": "GOOGL", "name": "谷歌A"}, {"code": "GOOG", "name": "谷歌C"},
    {"code": "AMZN", "name": "亚马逊"}, {"code": "NVDA", "name": "英伟达"},
    {"code": "TSLA", "name": "特斯拉"}, {"code": "META", "name": "Meta平台"},
    {"code": "NFLX", "name": "奈飞"}, {"code": "AMD", "name": "超威半导体"},
    {"code": "INTC", "name": "英特尔"}, {"code": "BABA", "name": "阿里巴巴"},
    {"code": "PDD", "name": "拼多多"}, {"code": "JD", "name": "京东"},
    {"code": "BIDU", "name": "百度"}, {"code": "NIO", "name": "蔚来"},
    {"code": "XPEV", "name": "小鹏汽车"}, {"code": "LI", "name": "理想汽车"},
    {"code": "TSM", "name": "台积电"}, {"code": "KO", "name": "可口可乐"},
    {"code": "PEP", "name": "百事可乐"}, {"code": "JPM", "name": "摩根大通"},
    {"code": "V", "name": "Visa"}, {"code": "MA", "name": "万事达"},
    {"code": "DIS", "name": "迪士尼"}, {"code": "WMT", "name": "沃尔玛"},
    {"code": "XOM", "name": "埃克森美孚"}, {"code": "BA", "name": "波音"},
    {"code": "GM", "name": "通用汽车"}, {"code": "UBER", "name": "优步"},
    {"code": "COIN", "name": "Coinbase"}, {"code": "PLTR", "name": "Palantir"},
    {"code": "QCOM", "name": "高通"}, {"code": "ORCL", "name": "甲骨文"},
    {"code": "CRM", "name": "赛富时"}, {"code": "ADBE", "name": "Adobe"},
    {"code": "GLD", "name": "金价ETF"}, {"code": "IAU", "name": "黄金ETF"},
]


def search_stock_us(keyword: str, limit: int = 10) -> list:
    """在内置美股列表里按代码或中文名匹配，代码前缀优先。"""
    kw = keyword.strip().upper()
    if not kw:
        return []
    hits = [s for s in US_STOCKS if kw in s["code"] or kw in s["name"]]
    hits.sort(key=lambda s: (0 if s["code"].startswith(kw) else 1, s["code"]))
    return [{"code": s["code"], "market": "us", "name": s["name"]} for s in hits[:limit]]


def search_stock_sina(keyword: str, limit: int = 10) -> list:
    """新浪建议接口搜索（主），GBK 编码，需 Referer。
    响应：var suggestdata_x="名称,类型,代码,完整代码,...;名称,类型,..."
    类型 11 = A 股，仅返回 A 股结果。
    """
    try:
        resp = requests.get(
            "https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15",
            params={"key": keyword, "name": "suggestdata_sm"},
            headers=SINA_HEADERS, timeout=8,
        )
        text = resp.content.decode("gbk", errors="ignore").strip()
        payload = text.split('"', 1)[1].rsplit('"', 1)[0]
    except Exception as e:
        logger.warning("[search][sina] 请求失败: %s", e)
        return []
    results = []
    for rec in payload.split(";"):
        parts = rec.split(",")
        if len(parts) < 4 or parts[1] != "11":
            continue
        name, code = parts[0], parts[2]
        market = "sh" if parts[3].startswith("sh") else "sz"
        results.append({"code": code, "market": market, "name": name})
        if len(results) >= limit:
            break
    return results


def search_stock_eastmoney(keyword: str) -> list:
    """东方财富建议接口搜索（备用），返回 JSON，仅 A 股。"""
    try:
        resp = requests.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={"input": keyword, "type": 14,
                    "token": "D43BF722C8E33BDC906FB84D85E326E8"},
            headers=HEADERS, timeout=8,
        )
        rows = (resp.json().get("QuotationCodeTable") or {}).get("Data") or []
    except Exception as e:
        logger.warning("[search][eastmoney] 请求失败: %s", e)
        return []
    results = []
    for r in rows:
        if r.get("Classify") != "AStock":
            continue
        code = r.get("Code", "")
        mkt = r.get("MktNum", "")
        results.append({
            "code": code,
            "market": "sh" if mkt == "1" else "sz",
            "name": r.get("Name", ""),
        })
    return results


def search_stock(keyword: str, limit: int = 10) -> list:
    """按代码或名称搜索，A 股为主（新浪、东财备用），美股用内置列表补充。
    新浪对纯数字输入返回的名称是完整代码（如 sz300408），此时用东财结果补全真实名称。
    """
    keyword = keyword.strip()
    if not keyword:
        return []
    results = search_stock_sina(keyword, limit)
    if not results:
        results = search_stock_eastmoney(keyword)
    elif any(r["name"].lower().startswith(("sh", "sz")) for r in results):
        em_map = {r["code"]: r["name"] for r in search_stock_eastmoney(keyword)}
        for r in results:
            if r["name"] in em_map:
                r["name"] = em_map[r["name"]]
            elif r["code"] in em_map:
                r["name"] = em_map[r["code"]]
    us = search_stock_us(keyword, limit)
    kw_upper = keyword.upper()
    exact_us = [u for u in us if u["code"] == kw_upper]
    rest_us = [u for u in us if u["code"] != kw_upper]
    seen = {r["code"] for r in results}
    merged = exact_us + results[:limit] + [u for u in rest_us if u["code"] not in seen]
    return merged[:limit]

NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
NEWS_CACHE: dict[str, tuple[float, list]] = {}
NEWS_CACHE_TTL = 120  # 秒


def parse_em_news_jsonp(text: str) -> list:
    """解析东方财富新闻搜索 JSONP 响应，返回 [{title, time, source, url, summary}]。

    JSONP 形如: cb({"code":0,"result":{"cmsArticleWebOld":[{...}]}})
    标题中的 <em> 高亮标签与摘要中的 HTML 标签一并清理。
    """
    try:
        start = text.index("(") + 1
        end = text.rindex(")")
        data = json.loads(text[start:end])
    except Exception:
        return []
    items = (data.get("result") or {}).get("cmsArticleWebOld") or []
    news = []
    for it in items:
        title = re.sub(r"</?em>", "", it.get("title", "") or "").strip()
        if not title:
            continue
        summary = re.sub(r"<[^>]+>", "", it.get("content", "") or "").strip()
        news.append({
            "title": title,
            "time": it.get("date", "") or "",
            "source": it.get("mediaName", "") or "",
            "url": it.get("url", "") or "",
            "summary": summary[:120],
        })
    return news


def fetch_news(keyword: str, limit: int = 12) -> list:
    """按关键词（标的名称）抓取东财个股新闻，带短时内存缓存。"""
    cache_key = f"{keyword}|{limit}"
    cached = NEWS_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < NEWS_CACHE_TTL:
        return cached[1]
    param = json.dumps({
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default", "pageIndex": 1,
            "pageSize": limit, "preTag": "<em>", "postTag": "</em>"}},
    }, ensure_ascii=False)
    url = f"{NEWS_URL}?cb=cb&param={requests.utils.quote(param)}"
    r = requests.get(url, timeout=10,
                     headers={**HEADERS,
                              "Referer": "https://so.eastmoney.com/"})
    r.raise_for_status()
    news = parse_em_news_jsonp(r.text)[:limit]
    NEWS_CACHE[cache_key] = (time.time(), news)
    return news


REALTIME_FETCHERS = {
    "eastmoney": fetch_realtime_eastmoney,
    "sina": fetch_realtime_sina,
    "tencent": fetch_realtime_tencent,
    "sina_us": fetch_realtime_us_sina,
}

# 历史 K 线数据源优先级（eastmoney 支持标准前复权参数，腾讯/新浪为备用）
KLINE_FETCHERS = [
    ("eastmoney", fetch_kline_eastmoney),
    ("tencent", fetch_kline_tencent),
    ("sina", fetch_kline_sina),
]

# 美股数据源（新浪财经免费接口）
KLINE_FETCHERS_US = [
    ("sina_us", fetch_kline_us_sina),
]

# 各市场实时数据源优先级
REALTIME_SOURCES = {
    "us": ["sina_us"],
    "cn": config["realtime_sources"],
}

# --------------------------------------------------------------
# 实时行情管理器（三级降级 + 节流）
# --------------------------------------------------------------
class RealtimeManager:
    """按优先级依次尝试数据源；连续失败 fail_threshold 次后降级；
    全部失败时返回最近一次成功的数据（cached=True）。"""

    def __init__(self, sources: list, fail_threshold: int = 2, min_interval: float = 3.0):
        self.sources = sources
        self.fail_threshold = fail_threshold
        self.min_interval = min_interval
        self.fail_counts = {s: 0 for s in sources}
        self.active = 0
        self.last_attempt: dict = {}
        self.last_data = None
        self.last_source = None
        self.lock = threading.Lock()

    def fetch(self, symbol: str) -> dict | None:
        with self.lock:
            n = len(self.sources)
            while n > 1 and self.fail_counts[self.sources[self.active]] >= self.fail_threshold:
                logger.warning("[realtime] %s 连续失败 %d 次，降级切换",
                               self.sources[self.active], self.fail_threshold)
                self.active = (self.active + 1) % n
            for i in range(n):
                idx = (self.active + i) % n
                name = self.sources[idx]
                if time.time() - self.last_attempt.get(name, 0) < self.min_interval:
                    continue
                self.last_attempt[name] = time.time()
                try:
                    data = REALTIME_FETCHERS[name](symbol)
                except Exception as e:
                    logger.warning("[realtime][%s] 异常: %s", name, e)
                    data = None
                if data:
                    self.fail_counts[name] = 0
                    self.active = idx
                    data["source"] = name
                    data["cached"] = False
                    self.last_data, self.last_source = data, name
                    return data
                self.fail_counts[name] += 1
                logger.warning("[realtime][%s] 获取失败（累计 %d 次）", name, self.fail_counts[name])
            if self.last_data:
                stale = dict(self.last_data)
                stale["source"], stale["cached"] = self.last_source, True
                return stale
            return None

# --------------------------------------------------------------
# 历史数据管理器（缓存 + 拉取）
# --------------------------------------------------------------
class HistoryManager:
    """日 K 数据：优先本地 CSV 缓存（秒开），过期后按优先级从多数据源拉取。"""

    def __init__(self, symbol: str, days: int = 250, ttl: int = 86400):
        self.symbol = symbol
        self.days = days
        self.ttl = ttl
        self.cache_file = os.path.join(DATA_DIR, f"kline_{market_of(symbol)}{symbol}.csv")
        self.fetchers = KLINE_FETCHERS_US if market_of(symbol) == "us" else KLINE_FETCHERS
        self.data = None
        self.last_fetch = 0.0
        self.source = None
        self._cond = threading.Condition()
        self._fetching = False

    def _load_cache(self) -> bool:
        """读取本地缓存；返回是否新鲜（未过期）。过期也读入以作降级兜底。"""
        try:
            if not os.path.exists(self.cache_file):
                return False
            mtime = os.path.getmtime(self.cache_file)
            df = pd.read_csv(self.cache_file)
            bars = [validate_bar(b, limit_pct(self.symbol)) for b in df.to_dict("records")]
            bars = [b for b in bars if b is not None]
            if len(bars) < 2:
                return False
            self.data = {"code": self.symbol, "name": config["name"], "bars": bars}
            return time.time() - mtime < self.ttl
        except Exception as e:
            logger.warning("[cache] 读取失败: %s", e)
            return False

    def _save_cache(self, data: dict) -> None:
        try:
            pd.DataFrame(data["bars"]).to_csv(self.cache_file, index=False)
            logger.info("[cache] 已写入 %s（%d 根）", self.cache_file, len(data["bars"]))
        except Exception as e:
            logger.warning("[cache] 写入失败: %s", e)

    def get(self, force: bool = False) -> dict | None:
        with self._cond:
            if self.data is None and self._fetching:
                self._cond.wait(timeout=15)
            if self.data is not None and not force and time.time() - self.last_fetch < self.ttl:
                return self.data
            if not force and self._load_cache():
                return self.data
            self._fetching = True
        try:
            fetched, src = None, None
            for name, fn in self.fetchers:
                fetched = fn(self.symbol, self.days)
                if fetched is not None:
                    src = name
                    break
                logger.warning("[history] %s 不可用，尝试下一数据源", name)
        finally:
            with self._cond:
                self._fetching = False
                self._cond.notify_all()
        if fetched is not None:
            if fetched.get("name"):
                config["name"] = fetched["name"]
            with self._cond:
                self.data, self.last_fetch, self.source = fetched, time.time(), src
            self._save_cache(fetched)
            logger.info("[history] 拉取成功（%s）：%s 共 %d 根 K 线",
                        src, self.symbol, len(fetched["bars"]))
            return fetched
        with self._cond:
            if self.data is not None:
                logger.warning("[history] 全部数据源失败，返回内存缓存数据")
                return self.data
        if self._load_cache():
            logger.warning("[history] 全部数据源失败，返回磁盘缓存数据")
            return self.data
        return None

# --------------------------------------------------------------
# 指标计算
# --------------------------------------------------------------
def calc_mas(bars: list, periods: list) -> dict:
    """计算多个周期的移动平均，前 n-1 个值为 None。"""
    closes = pd.Series([b["close"] for b in bars])
    result = {}
    for p in periods:
        mean = closes.rolling(p).mean()
        result[f"ma{p}"] = [None if pd.isna(v) else round(float(v), 2) for v in mean]
    return result

# --------------------------------------------------------------
# Flask 后端
# --------------------------------------------------------------
_realtime_mgr: RealtimeManager | None = None
_history_mgr: HistoryManager | None = None

def get_managers():
    global _realtime_mgr, _history_mgr
    if _realtime_mgr is None:
        sources = REALTIME_SOURCES.get(market_of(config["symbol"]), config["realtime_sources"])
        _realtime_mgr = RealtimeManager(sources, config["fail_threshold"])
        _history_mgr = HistoryManager(config["symbol"], config["history_days"], config["cache_ttl_sec"])
    return _realtime_mgr, _history_mgr

app = Flask(__name__)


@app.route("/")
def index():
    return HTML


@app.route("/static/<path:filename>")
def static_file(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/config")
def api_config():
    return jsonify({
        "symbol": config["symbol"],
        "market": config["market"],
        "name": config["name"],
        "kline_ranges": config["kline_ranges"],
        "default_range": config["default_range"],
        "ma": config["ma"],
        "ma_colors": {str(p): MA_COLORS.get(p, "#CCCCCC") for p in config["ma"]},
        "refresh_interval_sec": config["refresh_interval_sec"],
        "boot_id": str(os.getpid()),   # 每次启动变化，前端据此只在启动后首次打开时显示简介
    })


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": search_stock(q)})


@app.route("/api/switch")
def api_switch():
    """切换监控标的，返回新标的信息。"""
    code = normalize_symbol(request.args.get("code", ""))
    name = request.args.get("name", "").strip()
    if code is None:
        return jsonify({"error": "无效股票代码"}), 400
    global _history_mgr, _realtime_mgr
    config["symbol"] = code
    config["market"] = market_of(code)
    if name:
        config["name"] = name
    sources = REALTIME_SOURCES.get(config["market"], config["realtime_sources"])
    _realtime_mgr = RealtimeManager(sources, config["fail_threshold"])
    _history_mgr = HistoryManager(code, config["history_days"], config["cache_ttl_sec"])
    logger.info("[switch] 监控标的切换为 %s（%s）", config["name"], code)
    return jsonify({"ok": True, "symbol": code, "name": config["name"], "market": config["market"]})


@app.route("/api/realtime")
def api_realtime():
    rt, _ = get_managers()
    data = rt.fetch(config["symbol"])
    if data is None:
        return jsonify({"error": "所有数据源均失败", "trade_status": current_trade_status()}), 503
    data["trade_status"] = current_trade_status()
    if data.get("prev_close") and market_of(config["symbol"]) != "us":
        ratio = limit_ratio(config["symbol"], data.get("name", ""))
        data["limit_ratio"] = ratio
        data["limit_up"] = round(data["prev_close"] * (1 + ratio), 2)
        data["limit_down"] = round(data["prev_close"] * (1 - ratio), 2)
    return jsonify(data)


@app.route("/api/indices")
def api_indices():
    """各大指数实时行情（东方财富批量接口，缓存节流）。"""
    age = time.time() - INDEX_CACHE["ts"]
    if INDEX_CACHE["payload"] is None or age > 10:
        payload = fetch_indices_realtime()
        if payload is None:
            return jsonify({"error": "指数行情获取失败，请检查网络"}), 503
        INDEX_CACHE["payload"] = payload
        INDEX_CACHE["ts"] = time.time()
    return jsonify({"ok": True, "items": INDEX_CACHE["payload"]})


@app.route("/api/index-kline")
def api_index_kline():
    """单个指数日 K 线（secid 透传，如 100.HSI / 1.000001）。"""
    secid = request.args.get("secid", "").strip()
    if secid not in {i["secid"] for i in INDICES}:
        return jsonify({"error": "无效指数 secid"}), 400
    data = fetch_index_kline(secid)
    if data is None:
        return jsonify({"error": "指数 K 线获取失败，请检查网络"}), 503
    return jsonify({
        "code": data["code"],
        "name": data["name"],
        "count": len(data["bars"]),
        "dates": [b["date"] for b in data["bars"]],
        "ohlc": [[b["open"], b["close"], b["low"], b["high"]] for b in data["bars"]],
        "volume": [b["volume"] for b in data["bars"]],
        "amount": [b["amount"] for b in data["bars"]],
        "updated_at": data["bars"][-1]["date"],
    })


@app.route("/api/limit-pool")
def api_limit_pool():
    if market_of(config["symbol"]) == "us":
        return jsonify({"error": "美股无涨跌停机制"}), 503
    age = time.time() - _limit_pool_cache["ts"]
    trading = current_trade_status() != "已收盘"
    if _limit_pool_cache["payload"] is None or not pool_cache_age_ok(age, trading):
        payload = fetch_limit_pool(dt.datetime.now().strftime("%Y%m%d"))
        if payload is None:
            return jsonify({"error": "涨停/跌停池获取失败，请检查网络"}), 503
        _limit_pool_cache["payload"] = payload
        _limit_pool_cache["ts"] = time.time()
    return jsonify(_limit_pool_cache["payload"])


@app.route("/api/news")
def api_news():
    """当前监控标的的个股新闻（东方财富搜索，按名称匹配；美股用中文名如「苹果」）。"""
    code = request.args.get("code") or config["symbol"]
    name = request.args.get("name") or config["name"]
    keyword = name or code
    try:
        items = fetch_news(keyword)
        return jsonify({"ok": True, "code": code, "name": name,
                        "keyword": keyword, "items": items})
    except Exception as e:
        logger.warning("[news][%s] 新闻获取失败: %s", keyword, e)
        return jsonify({"ok": False, "error": "新闻获取失败"}), 502


@app.route("/api/kline")
def api_kline():
    _, hm = get_managers()
    force = request.args.get("refresh") == "1"
    data = hm.get(force=force)
    if data is None:
        return jsonify({"error": "历史数据获取失败，请检查网络后刷新"}), 503
    mas = calc_mas(data["bars"], config["ma"])
    return jsonify({
        "code": data["code"],
        "name": data["name"],
        "fqt": "qfq",
        "source": hm.source,
        "count": len(data["bars"]),
        "dates": [b["date"] for b in data["bars"]],
        "ohlc": [[b["open"], b["close"], b["low"], b["high"]] for b in data["bars"]],
        "volume": [b["volume"] for b in data["bars"]],
        "amount": [b["amount"] for b in data["bars"]],
        "ma": mas,
        "updated_at": data["bars"][-1]["date"],
    })


@app.route("/api/health")
def api_health():
    rt, hm = get_managers()
    return jsonify({
        "trade_status": trade_status(),
        "realtime": {
            "active_source": rt.sources[rt.active],
            "fail_counts": rt.fail_counts,
            "has_data": rt.last_data is not None,
        },
        "history": {
            "source_ok": hm.data is not None,
            "source": hm.source,
            "bars": len(hm.data["bars"]) if hm.data else 0,
            "cache_file": os.path.basename(hm.cache_file),
            "last_fetch": dt.datetime.fromtimestamp(hm.last_fetch).strftime("%Y-%m-%d %H:%M:%S")
            if hm.last_fetch else None,
        },
        "server_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

# --------------------------------------------------------------
# ECharts 本地化
# --------------------------------------------------------------
def ensure_echarts() -> bool:
    """下载 echarts.min.js 到 static/ 目录，失败则前端回退 CDN。"""
    if os.path.exists(ECHARTS_FILE) and os.path.getsize(ECHARTS_FILE) > 300_000:
        return True
    urls = [
        f"https://registry.npmmirror.com/echarts/{ECHARTS_VERSION}/files/dist/echarts.min.js",
        f"https://cdn.jsdelivr.net/npm/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
        f"https://unpkg.com/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 300_000:
                with open(ECHARTS_FILE, "wb") as f:
                    f.write(r.content)
                logger.info("ECharts 已下载到本地: %s", ECHARTS_FILE)
                return True
        except Exception:
            continue
    logger.warning("ECharts 本地下载失败，页面将使用 CDN 加载")
    return False

# --------------------------------------------------------------
# 前端页面
# --------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票监控 v0.3.1</title>
<script>
(function(){var t=localStorage.getItem('theme')||'tonghuashun';document.documentElement.setAttribute('data-theme',t);})();
</script>
<style>
  :root, [data-theme="tonghuashun"] {
    --bg: #020617; --bg2: #0B1120; --card: #0F172A; --card2: #131C31;
    --border: #1E293B; --text: #F8FAFC; --muted: #94A3B8; --dim: #64748B;
    --up: #EF232A; --down: #14B143; --gold: #F9D71C; --purple: #A967FF;
    --accent: #60A5FA; --radius: 10px; --radius-lg: 12px;
    --hover: rgba(255,255,255,0.04); --shadow: 0 8px 24px rgba(0,0,0,0.45);
    --grid: #2A2A2A; --axis: #888; --tooltip-bg: rgba(0,0,0,0.85); --tooltip-border: #444;
    --tooltip-text: #DDD; --lbc-up-bg: rgba(239,42,42,0.25); --lbc-up-c: #FF8A8A;
    --nav-bg: rgba(14,17,23,0.85); --skeleton: rgba(255,255,255,0.05);
  }
  [data-theme="minimal"] {
    --bg: #FAFAFA; --bg2: #FFFFFF; --card: #FFFFFF; --card2: #F4F4F5;
    --border: #E4E4E7; --text: #09090B; --muted: #52525B; --dim: #A1A1AA;
    --up: #EF232A; --down: #14B143; --gold: #18181B; --purple: #3F3F46;
    --accent: #2563EB; --radius: 10px; --radius-lg: 12px;
    --hover: rgba(0,0,0,0.04); --shadow: 0 6px 20px rgba(0,0,0,0.10);
    --grid: #E4E4E7; --axis: #71717A; --tooltip-bg: rgba(255,255,255,0.96); --tooltip-border: #E4E4E7;
    --tooltip-text: #18181B; --lbc-up-bg: rgba(239,42,42,0.12); --lbc-up-c: #DC2626;
    --nav-bg: rgba(255,255,255,0.88); --skeleton: rgba(0,0,0,0.05);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { background: var(--bg); color: var(--text);
         font-family: "PingFang SC", "Microsoft YaHei", -apple-system, sans-serif;
         font-size: 14px; transition: background .25s, color .25s; }
  a { color: inherit; text-decoration: none; }
  a, button { cursor: pointer; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
  }

  /* ---------- 顶部导航 ---------- */
  nav { position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 56px;
        display: flex; align-items: center; gap: 20px; padding: 0 24px;
        background: var(--nav-bg); backdrop-filter: blur(12px);
        border-bottom: 1px solid var(--border); }
  .logo { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 700; color: var(--text); }
  .logo svg { display: block; }
  .ver { font-size: 10px; color: var(--gold); border: 1px solid var(--gold);
         border-radius: 10px; padding: 1px 5px; margin-left: 2px; font-weight: 600; }
  .search-box { position: relative; flex: 0 1 420px; margin-left: 6px; }
  .search-box .s-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--dim); }
  #searchInput { width: 100%; height: 36px; border-radius: 10px; border: 1px solid var(--border);
                 background: var(--bg2); color: var(--text); font-size: 13px;
                 padding: 0 16px 0 36px; outline: none; transition: border-color .2s, box-shadow .2s; }
  #searchInput::placeholder { color: var(--dim); }
  #searchInput:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(96,165,250,0.18); }
  .nav-links { margin-left: auto; display: flex; align-items: center; gap: 16px; color: var(--muted); font-size: 13px; }
  .nav-links a { transition: color .2s; }
  .nav-links a:hover, .nav-links a.active { color: var(--text); }
  .nav-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
             border-radius: 10px; padding: 4px 12px; font-size: 12px; transition: all .2s; }
  .nav-btn:hover { color: var(--text); border-color: var(--dim); }
  .nav-btn.on { color: var(--accent); border-color: var(--accent); }

  .dropdown { position: absolute; top: 42px; left: 0; right: 0; z-index: 200;
              background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
              box-shadow: var(--shadow); overflow: hidden; }
  .dropdown.hidden { display: none; }
  .s-item { display: flex; align-items: center; gap: 10px; padding: 9px 14px; cursor: pointer;
            border-bottom: 1px solid var(--border); }
  .s-item:last-child { border-bottom: none; }
  .s-item:hover, .s-item.active { background: var(--hover); }
  .s-name { font-size: 13px; color: var(--text); }
  .s-code { font-size: 12px; color: var(--muted); font-family: Menlo, monospace; }
  .s-mkt { margin-left: auto; font-size: 10px; color: var(--muted); border: 1px solid var(--border);
           border-radius: 10px; padding: 1px 6px; }
  .s-mkt.sh { color: #7FB4FF; border-color: #3A5A8C; }
  .s-mkt.sz { color: #FFB46E; border-color: #8C6A3A; }
  .s-empty { padding: 12px 14px; color: var(--dim); font-size: 12px; }

  /* ---------- 简介全屏页 ---------- */
  .intro-overlay { position: fixed; inset: 0; z-index: 500; overflow-y: auto;
                   background: var(--bg); display: flex; align-items: center; justify-content: center; }
  .intro-overlay.hidden { display: none; }
  .intro-card { max-width: 1060px; width: 100%; margin: auto; padding: 96px 24px 40px; text-align: center; }
  .intro-card h1 { font-size: 34px; font-weight: 800; letter-spacing: 1px; color: var(--text); }
  .intro-card .tagline { margin: 14px auto 0; max-width: 640px; color: var(--muted); line-height: 1.8; font-size: 14px; }
  .feature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                  gap: 14px; margin-top: 34px; text-align: left; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
          padding: 18px; display: flex; gap: 14px; align-items: flex-start;
          transition: transform .2s, border-color .2s; }
  .card:hover { border-color: var(--dim); transform: translateY(-2px); }
  .card .icon { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 10px;
                display: flex; align-items: center; justify-content: center;
                background: rgba(96,165,250,0.12); }
  .card h3 { font-size: 14px; margin-bottom: 5px; color: var(--text); }
  .card p { font-size: 12px; color: var(--muted); line-height: 1.6; }
  .intro-actions { margin-top: 30px; display: flex; align-items: center; justify-content: center; gap: 14px; flex-wrap: wrap; }
  .cta { display: inline-block; padding: 11px 28px; border-radius: 12px;
         font-size: 14px; font-weight: 600; color: #0F172A;
         background: var(--accent); border: none; transition: background .2s; }
  .cta:hover { filter: brightness(1.12); }
  .btn-ghost { display: inline-block; padding: 11px 24px; border-radius: 12px;
               font-size: 14px; font-weight: 600; color: var(--muted);
               background: transparent; border: 1px solid var(--border); transition: all .2s; }
  .btn-ghost:hover { color: var(--text); border-color: var(--dim); }
  .noshow { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--dim); cursor: pointer; }
  .noshow input { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }

  /* ---------- 新手引导 ---------- */
  .tutorial { position: fixed; inset: 0; z-index: 600; }
  .tutorial.hidden { display: none; }
  .tut-mask { position: absolute; inset: 0; background: rgba(0,0,0,0.55); }
  .tut-spot { position: absolute; border: 2px solid var(--accent); border-radius: var(--radius);
              box-shadow: 0 0 0 4px rgba(96,165,250,0.35), 0 0 40px rgba(96,165,250,0.4);
              transition: all .3s ease; }
  .tut-tip { position: absolute; left: 50%; transform: translateX(-50%); bottom: 40px;
             width: min(520px, calc(100vw - 48px)); background: var(--card);
             border: 1px solid var(--border); border-radius: var(--radius-lg);
             box-shadow: var(--shadow); padding: 20px 22px; }
  .tut-text { font-size: 14px; line-height: 1.7; color: var(--text); }
  .tut-text b { color: var(--accent); }
  .tut-count { font-size: 11px; color: var(--dim); margin-top: 8px; font-family: Menlo, monospace; }
  .tut-btns { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
  .tut-btns .spacer { flex: 1; }
  .tut-btn { padding: 7px 18px; border-radius: 10px; font-size: 13px; font-weight: 600;
             border: 1px solid var(--border); background: transparent; color: var(--muted); transition: all .2s; }
  .tut-btn:hover { color: var(--text); border-color: var(--dim); }
  .tut-btn.primary { background: var(--accent); color: #0F172A; border-color: var(--accent); }
  .tut-btn.primary:hover { filter: brightness(1.12); }
  .tut-btn.ghost { border: none; }

  /* ---------- 页面容器（横向滑动） ---------- */
  .pages { max-width: 1500px; margin: 0 auto; display: flex; overflow: hidden;
           touch-action: pan-y; will-change: transform; transition: transform .35s ease; }
  .page { flex: 0 0 100%; min-width: 0; padding: 80px 24px 24px; }
  .page-dots { display: flex; justify-content: center; gap: 8px; padding: 2px 0 0; }
  .page-dots .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim);
                    cursor: pointer; transition: background .2s; }
  .page-dots .dot.active { background: var(--accent); }

  /* ---------- 组件系统 ---------- */
  .widgets { display: flex; flex-direction: column; gap: 14px; }
  .widget { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
            overflow: hidden; transition: border-color .2s; }
  .widget.dragging { opacity: 0.55; border-color: var(--accent); }
  .widget.drag-over { border-top: 3px solid var(--accent); }
  .widget.locked .drag-handle { cursor: default; opacity: 0.25; }
  .widget.hidden { display: none; }
  .widget-head { display: flex; align-items: center; gap: 10px; padding: 11px 16px;
                 border-bottom: 1px solid var(--border); }
  .widget-head .w-title { font-size: 14px; font-weight: 700; color: var(--text); }
  .widget-head .w-sub { font-size: 11px; color: var(--dim); font-family: Menlo, monospace; }
  .widget-head .w-spacer { flex: 1; }
  .drag-handle { font-size: 15px; color: var(--dim); cursor: grab; user-select: none;
                 padding: 0 2px; line-height: 1; }
  .drag-handle:active { cursor: grabbing; }
  .w-hide, .w-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
                    border-radius: 10px; padding: 2px 10px; font-size: 11px;
                    cursor: pointer; transition: all .2s; }
  .w-hide:hover, .w-btn:hover { color: var(--text); border-color: var(--dim); }
  .widget-body { padding: 10px 14px 14px; }
  .w-add { margin-top: 14px; position: relative; }
  .w-add-btn { background: var(--card); border: 1px dashed var(--border); color: var(--muted);
               border-radius: var(--radius-lg); padding: 10px 16px; font-size: 13px;
               width: 100%; transition: all .2s; }
  .w-add-btn:hover { color: var(--text); border-color: var(--accent); }
  .w-add-menu { position: absolute; bottom: calc(100% + 6px); left: 0; right: 0; z-index: 300;
                background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
                box-shadow: var(--shadow); overflow: hidden; }
  .w-add-menu.hidden { display: none; }
  .w-add-item { display: flex; align-items: center; gap: 10px; padding: 11px 16px; cursor: pointer;
                border-bottom: 1px solid var(--border); transition: background .15s; }
  .w-add-item:last-child { border-bottom: none; }
  .w-add-item:hover { background: var(--hover); }
  .w-add-item .wa-title { font-size: 13px; font-weight: 600; color: var(--text); }
  .w-add-item .wa-desc { font-size: 11px; color: var(--dim); }
  .w-add-item.disabled { opacity: 0.4; cursor: default; }
  .w-add-item.disabled:hover { background: transparent; }

  .index-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .index-card { border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px;
                background: var(--card2); transition: border-color .2s; }
  .index-card:hover { border-color: var(--dim); }
  .index-card .ic-name { font-size: 13px; font-weight: 600; color: var(--text); }
  .index-card .ic-price { font-size: 20px; font-weight: 700; font-family: Menlo, monospace; margin-top: 4px; }
  .index-card .ic-chg { font-size: 12px; font-family: Menlo, monospace; margin-top: 2px; }
  .index-card .up-c { color: var(--up); }
  .index-card .down-c { color: var(--down); }
  .index-card .flat-c { color: var(--dim); }
  .index-grid .empty, .news-list .empty, .fav-list .empty, .bigbox-list .empty {
    padding: 20px 14px; font-size: 12px; color: var(--dim); text-align: center; }
  .sk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .sk-card { height: 74px; border-radius: var(--radius); background: var(--skeleton);
             animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }

  /* ---------- 涨跌榜组件 ---------- */
  .bigbox-list { max-height: 320px; overflow-y: auto; padding: 4px 0; }
  .bigbox-list .pitem { display: flex; align-items: baseline; gap: 8px; padding: 7px 2px; cursor: pointer;
                        border-bottom: 1px solid var(--border); }
  .bigbox-list .pitem:last-child { border-bottom: none; }
  .bigbox-list .pitem:hover { background: var(--hover); }
  .pitem .p-name { width: 74px; flex-shrink: 0; font-size: 13px; color: var(--text);
                   white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pitem .p-code { width: 54px; flex-shrink: 0; font-size: 11px; color: var(--dim); font-family: Menlo, monospace; }
  .pitem .p-lbc { width: 42px; flex-shrink: 0; font-size: 10px; text-align: center; border-radius: 10px;
                  padding: 1px 0; font-family: Menlo, monospace; background: var(--lbc-up-bg); color: var(--lbc-up-c); }
  .pitem .p-pct { width: 56px; flex-shrink: 0; font-size: 12px; text-align: right; font-family: Menlo, monospace; }
  .pitem .p-price { margin-left: auto; font-size: 14px; font-family: Menlo, monospace; font-weight: 600; }
  .bigbox-up .pitem .p-pct, .bigbox-up .pitem .p-price { color: var(--up); }
  .bigbox-down .pitem .p-pct, .bigbox-down .pitem .p-price { color: var(--down); }
  .pool-date { text-align: center; font-size: 11px; color: var(--dim); font-family: Menlo, monospace; padding-top: 8px; }
  .l-val { font-family: Menlo, monospace; font-size: 15px; font-weight: 700; }
  .l-count { font-size: 11px; color: var(--dim); font-family: Menlo, monospace; }

  /* ---------- 新闻组件 ---------- */
  .news-list { max-height: 420px; overflow-y: auto; padding: 2px 0; }
  .news-list .nitem { display: block; padding: 9px 2px; border-bottom: 1px solid var(--border); }
  .news-list .nitem:last-child { border-bottom: none; }
  .news-list .nitem:hover { background: var(--hover); }
  .nitem .n-title { display: block; font-size: 13px; color: var(--text); line-height: 1.5; }
  .nitem .n-meta { display: block; font-size: 11px; color: var(--dim); margin-top: 4px;
                   font-family: Menlo, monospace; }

  /* ---------- 收藏面板（右侧） ---------- */
  .favs-layout { display: flex; gap: 14px; align-items: flex-start; }
  .favs-layout .widgets { flex: 1; min-width: 0; }
  .favs-side { width: 330px; flex-shrink: 0; background: var(--card); border: 1px solid var(--border);
               border-radius: var(--radius-lg); overflow: hidden; position: sticky; top: 70px; }
  .favs-side.hidden { display: none; }
  .fav-list { min-height: 200px; max-height: 70vh; overflow-y: auto; }
  .fav-list .fitem { display: flex; align-items: baseline; gap: 10px; padding: 11px 16px;
                     cursor: pointer; border-bottom: 1px solid var(--border); }
  .fav-list .fitem:last-child { border-bottom: none; }
  .fav-list .fitem:hover { background: var(--hover); }
  .fitem .f-name { font-size: 14px; color: var(--text); }
  .fitem .f-code { font-size: 12px; color: var(--muted); font-family: Menlo, monospace; }
  .fitem .f-mkt { margin-left: auto; font-size: 10px; color: var(--muted);
                  border: 1px solid var(--border); border-radius: 10px; padding: 1px 6px; }
  .fav-btn { align-self: center; width: 28px; height: 28px; background: transparent;
             border: 1px solid var(--border); border-radius: 10px; color: var(--muted);
             font-size: 15px; line-height: 1; cursor: pointer; transition: all .2s; }
  .fav-btn:hover { color: var(--text); border-color: var(--dim); }
  .fav-btn.on { color: var(--gold); border-color: #8C7A2A; }

  /* ---------- 股票监控（chart）组件 ---------- */
  .widget-chart .widget-body { padding: 0; }
  .panel-head { display: flex; align-items: baseline; gap: 10px; padding: 10px 16px;
                border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  #td-name { font-size: 20px; font-weight: 700; color: var(--text); }
  #td-code { color: var(--muted); font-family: Menlo, monospace; font-size: 13px; }
  #td-mkt { font-size: 10px; color: #7FB4FF; border: 1px solid #3A5A8C; border-radius: 10px; padding: 1px 6px; }
  #td-price { font-size: 26px; font-weight: 700; font-family: Menlo, monospace; margin-left: 12px; }
  #td-chg { font-size: 15px; font-family: Menlo, monospace; }
  .stats { display: flex; gap: 18px; padding: 8px 16px; border-bottom: 1px solid var(--border);
           color: var(--dim); font-size: 12px; flex-wrap: wrap; }
  .stats b { color: var(--text); font-weight: 500; font-family: Menlo, monospace; }
  .toolbar { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
             border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  .range-btn { background: transparent; color: var(--muted); border: 1px solid var(--border);
               border-radius: 10px; padding: 4px 14px; cursor: pointer; font-size: 12px;
               transition: all .2s; }
  .range-btn:hover { color: var(--text); border-color: var(--dim); }
  .range-btn.active { background: var(--accent); color: #0F172A; border-color: var(--accent); font-weight: 600; }
  #ma-info { font-family: Menlo, monospace; font-size: 12px; margin-left: 10px; }
  .toolbar .hint { margin-left: auto; color: var(--dim); font-size: 12px; }
  #chartWrap { position: relative; height: 500px; }
  #chart { width: 100%; height: 100%; }
  #loading { position: absolute; inset: 0; display: flex; flex-direction: column; gap: 10px;
             align-items: center; justify-content: center; background: var(--bg2); z-index: 50; }
  #loading.hidden { display: none; }
  .spinner { width: 34px; height: 34px; border-radius: 50%;
             border: 3px solid var(--border); border-top-color: var(--accent);
             animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #statusbar { display: flex; gap: 24px; padding: 8px 16px; border-top: 1px solid var(--border);
               font-size: 12px; color: var(--muted); flex-wrap: wrap; }
  #st-alert { color: #FF5555; font-weight: 600; }
  #st-alert.hidden { display: none; }

  footer { max-width: 1060px; margin: 24px auto 40px; padding: 0 24px;
           color: var(--dim); font-size: 12px; text-align: center; line-height: 1.8; }
  footer .sep { color: var(--border); margin: 0 6px; }
  @media (max-width: 1100px) {
    .favs-layout { flex-direction: column; }
    .favs-side { width: 100%; position: static; }
  }
  @media (max-width: 720px) {
    nav { padding: 0 16px; gap: 12px; }
    .nav-links { display: none; }
    .page { padding: 76px 16px 16px; }
    #chartWrap { height: 380px; }
    .index-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  }
  @media (max-width: 480px) {
    .intro-card { padding: 64px 16px 28px; }
    .intro-card h1 { font-size: 24px; }
    .search-box { flex: 1; }
  }
</style>
</head>
<body>
  <nav>
    <div class="logo">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
        <rect x="3" y="11" width="4" height="8" rx="1" fill="#EF232A"/>
        <rect x="10" y="5" width="4" height="14" rx="1" fill="#14B143"/>
        <rect x="17" y="8" width="4" height="11" rx="1" fill="#EF232A"/>
        <line x1="2" y1="19" x2="22" y2="19" stroke="#3A4456" stroke-width="1.5"/>
      </svg>
      <span>股票监控</span><span class="ver">v0.3.1</span>
    </div>
    <div class="search-box">
      <svg class="s-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
        <circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>
      </svg>
      <input id="searchInput" type="text" placeholder="输入股票代码或名称，如 600519 / 茅台 / AAPL" autocomplete="off">
      <div id="dropdown" class="dropdown hidden"></div>
    </div>
    <div class="nav-links">
      <a href="#" data-page-link="monitor" class="active">实时监控</a>
      <a href="#" data-page-link="favs">收藏监控</a>
      <span class="sep" style="color:var(--border)">|</span>
      <button id="theme-toggle" class="nav-btn" title="切换主题">🌓 主题</button>
      <button id="layout-lock" class="nav-btn" title="锁定/解锁布局">🔓 锁定</button>
      <button id="tutorial-btn" class="nav-btn" title="查看新手教程">?</button>
    </div>
  </nav>

  <!-- 简介全屏页（首次打开；不再提示 → 永久不再弹出） -->
  <div id="intro-overlay" class="intro-overlay hidden">
    <div class="intro-card">
      <h1>轻量 · 免费 · 同花顺风格的股票监控</h1>
      <p class="tagline">本地运行的股票监控：多页面组件化监控台，支持各大指数（恒生 / 标普 / 日经等）、个股新闻、涨跌榜、
        K 线蜡烛图、双主题（同花顺 / 极简黑白），组件可自由摆放与锁定，数据全部来自免费公开接口。</p>
      <div class="feature-grid">
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#60A5FA" stroke-width="2"><path d="M4 20V10 M9 20V4 M14 20V13 M19 20V7"/></svg></div>
          <div><h3>指数监控</h3><p>上证 / 深证 / 创业板 / 沪深300 / 恒生 / 日经 / 标普 / 道琼斯，多指数卡片实时刷新</p></div>
        </div>
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#EF232A" stroke-width="2"><path d="M3 17 L8 11 L13 14 L21 5"/></svg></div>
          <div><h3>涨跌榜</h3><p>涨停 / 跌停榜独立组件展示，点击任意股票一键切换监控</p></div>
        </div>
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#F9D71C" stroke-width="2"><path d="M12 2l7 4v6c0 5-3 8-7 10-4-2-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg></div>
          <div><h3>个股新闻</h3><p>当前标的（A股 / 美股）相关新闻实时展示，点击跳转原文</p></div>
        </div>
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#7FB4FF" stroke-width="2"><rect x="3" y="10" width="18" height="4" rx="1"/><rect x="5" y="6" width="14" height="3" rx="1"/><rect x="5" y="15" width="14" height="3" rx="1"/></svg></div>
          <div><h3>可拖拽布局</h3><p>组件自由拖拽摆放、一键隐藏、随时添加，摆好后可锁定布局防止误触</p></div>
        </div>
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#14B143" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 3"/></svg></div>
          <div><h3>双主题</h3><p>同花顺深色 + 极简黑白白底，一键切换，自动记忆偏好</p></div>
        </div>
        <div class="card">
          <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#A967FF" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg></div>
          <div><h3>多源容灾</h3><p>东方财富 / 新浪 / 腾讯三级降级，接口故障自动切换，不中断监控</p></div>
        </div>
      </div>
      <div class="intro-actions">
        <button id="intro-enter" class="cta">进入监控</button>
        <button id="intro-tutorial" class="btn-ghost">查看新手教程</button>
        <label class="noshow"><input type="checkbox" id="intro-noshow"> 不再提示</label>
      </div>
    </div>
  </div>

  <!-- 新手引导 -->
  <div id="tutorial" class="tutorial hidden">
    <div class="tut-mask" id="tut-mask"></div>
    <div class="tut-spot" id="tut-spot"></div>
    <div class="tut-tip">
      <div class="tut-text" id="tut-text"></div>
      <div class="tut-count" id="tut-count"></div>
      <div class="tut-btns">
        <button id="tut-skip" class="tut-btn ghost">跳过引导</button>
        <span class="spacer"></span>
        <button id="tut-prev" class="tut-btn">上一步</button>
        <button id="tut-next" class="tut-btn primary">下一步</button>
      </div>
    </div>
  </div>

  <div class="pages" id="pages">
    <!-- 页面 1：实时监控 -->
    <section id="page-monitor" class="page">
      <div class="widgets" id="widgets-monitor"></div>
      <div class="w-add">
        <button class="w-add-btn" data-add-menu="monitor">+ 添加组件</button>
        <div class="w-add-menu hidden" data-menu="monitor"></div>
      </div>
    </section>

    <!-- 页面 2：收藏 + 股票监控 -->
    <section id="page-favs" class="page">
      <div class="favs-layout">
        <div class="widgets" id="widgets-favs"></div>
        <div class="favs-side" id="favs-side">
          <div class="widget-head">
            <span class="w-title">我的收藏</span><span class="l-count" id="fav-count"></span>
            <span class="w-spacer"></span>
            <button id="toggle-favs" class="w-btn" title="隐藏/显示收藏">收起 ▾</button>
          </div>
          <div class="fav-list" id="fav-list"><div class="empty">暂无收藏，在股票监控组件点击 ☆ 收藏</div></div>
        </div>
      </div>
      <div class="w-add">
        <button class="w-add-btn" data-add-menu="favs">+ 添加组件</button>
        <div class="w-add-menu hidden" data-menu="favs"></div>
      </div>
    </section>
  </div>
  <div class="page-dots" id="page-dots">
    <span class="dot active" data-i="0" title="实时监控"></span><span class="dot" data-i="1" title="收藏监控"></span>
  </div>

  <footer id="footer">
    股票监控 v0.3.1 · 测试版<br>
    数据来源：东方财富 / 新浪财经 / 腾讯财经（免费公开接口）· 行情数据仅供参考，不构成投资建议
  </footer>

  <script src="/static/echarts.min.js"></script>
  <script>
    if (typeof echarts === 'undefined') {
      document.write('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"><\/script>');
    }
  </script>
  <script>
    const UP = '#EF232A', DOWN = '#14B143', FLAT = '#999999';
    const state = { data: null, range: 50, ma: [5, 20], pollMs: 5000,
                    pollTimer: null, indexTimer: null, chart: null, results: [], ki: -1,
                    stockName: '', market: 'sz', curCode: '', pageIdx: 0,
                    layoutLocked: false, tutStep: -1 };
    const PAGES = {
      monitor: { def: ['index', 'limit-up', 'limit-down', 'news'],
                 avail: ['index', 'limit-up', 'limit-down', 'news'],
                 names: { 'index': ['各大指数', '多指数实时卡片'],
                          'limit-up': ['涨停榜', '当日涨停股票池'],
                          'limit-down': ['跌停榜', '当日跌停股票池'],
                          'news': ['个股新闻', '当前标的新闻动态'] } },
      favs: { def: ['chart'],
              avail: ['index', 'limit-up', 'limit-down', 'news'],
              names: { 'chart': ['股票监控', 'K 线 + 实时行情'],
                       'index': ['各大指数', '多指数实时卡片'],
                       'limit-up': ['涨停榜', '当日涨停股票池'],
                       'limit-down': ['跌停榜', '当日跌停股票池'],
                       'news': ['个股新闻', '当前标的新闻动态'] } }
    };

    function $(id) { return document.getElementById(id); }
    function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
    async function getJSON(url) { const r = await fetch(url); return r.json(); }
    function fmtPrice(v, d) { if (v == null || isNaN(v)) return '--'; return Number(v).toFixed(d == null ? 2 : d); }
    function fmtVol(v) { if (v == null || isNaN(v)) return '--'; return v >= 10000 ? (v/10000).toFixed(2)+'万手' : v+'手'; }
    function fmtAmount(v) { if (v == null || isNaN(v)) return '--'; return v >= 1e8 ? (v/1e8).toFixed(2)+'亿' : (v/1e4).toFixed(0)+'万'; }
    function barColor(c, o) { return c > o ? UP : (c < o ? DOWN : FLAT); }
    function showAlert(m) { const el = $('st-alert'); el.textContent = m; el.classList.remove('hidden'); }
    function hideAlert() { $('st-alert').classList.add('hidden'); }
    function setLoading(on, text) {
      const el = $('loading');
      if (el) el.classList.toggle('hidden', !on);
      if (text) { const t = $('loading-text'); if (t) t.textContent = text; }
    }
    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    /* ---------- 主题 ---------- */
    function currentTheme() { return document.documentElement.getAttribute('data-theme') || 'tonghuashun'; }
    function applyTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem('theme', t);
      $('theme-toggle').textContent = t === 'minimal' ? '🌙 同花顺' : '🌓 极简';
      if (state.chart) { state.chart.dispose(); state.chart = null; initChart(); render(); }
    }
    $('theme-toggle').addEventListener('click', function () {
      applyTheme(currentTheme() === 'minimal' ? 'tonghuashun' : 'minimal');
    });

    /* ---------- 简介页（首次打开；不再提示 → 永久） ---------- */
    function maybeShowIntro() {
      if (localStorage.getItem('intro_dismissed') === '1') return;
      $('intro-overlay').classList.remove('hidden');
    }
    function closeIntro(runTut) {
      $('intro-overlay').classList.add('hidden');
      if ($('intro-noshow').checked) localStorage.setItem('intro_dismissed', '1');
      if (runTut) startTutorial();
      else if (localStorage.getItem('tutorial_seen') !== '1') startTutorial();
    }
    $('intro-enter').addEventListener('click', function () { closeIntro(false); });
    $('intro-tutorial').addEventListener('click', function () { closeIntro(true); });

    /* ---------- 新手引导 ---------- */
    const TUT_STEPS = [
      { sel: '#searchInput', page: 0, text: '<b>顶部搜索框</b>：输入股票代码或名称（如 600519 / 茅台 / AAPL）搜索并切换监控标的，支持键盘上下选择 + 回车确认。' },
      { sel: '#theme-toggle', page: 0, text: '<b>主题切换</b>：在「同花顺」深色与「极简」黑白白底之间一键切换，偏好自动保存。' },
      { sel: '#layout-lock', page: 0, text: '<b>布局锁定</b>：摆放好组件后可锁定布局，防止误拖；再次点击解锁即可继续调整。' },
      { sel: '[data-widget="index"]', page: 0, text: '<b>各大指数组件</b>：上证 / 深证 / 创业板 / 沪深300 / 恒生 / 日经 / 标普 / 道琼斯实时点位与涨跌幅，约 10 秒自动刷新。' },
      { sel: '[data-widget="limit-up"]', page: 0, text: '<b>涨停 / 跌停榜</b>：独立组件展示当日涨停与跌停股票池，点击列表项直接切换监控。' },
      { sel: '[data-widget="news"]', page: 0, text: '<b>个股新闻</b>：当前监控标的的相关新闻，点击跳转原文，可手动刷新。' },
      { sel: '[data-widget="chart"]', page: 1, text: '<b>股票监控组件</b>：K 线蜡烛图 + MA5/MA20 双均线 + 三档时间区间，交易时段 5 秒实时刷新。' },
      { sel: '#favs-side', page: 1, text: '<b>我的收藏</b>：收藏列表固定在右侧，可收起 / 展开；点击收藏项一键切换监控。' },
      { sel: '.w-add-btn', page: 1, text: '<b>添加组件</b>：每个页面都能添加 / 隐藏组件，组件可拖拽手柄自由排序，摆好后记得锁定布局。' }
    ];
    function tutGo(i) {
      if (i < 0 || i >= TUT_STEPS.length) { endTutorial(); return; }
      state.tutStep = i;
      const step = TUT_STEPS[i];
      if (state.pageIdx !== step.page) setPage(step.page);
      const el = $(step.sel.replace(/^#/, '')) || document.querySelector(step.sel);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setTimeout(function () {
          const r = el.getBoundingClientRect();
          const spot = $('tut-spot');
          spot.style.cssText = 'left:' + r.left + 'px;top:' + r.top + 'px;width:' + r.width + 'px;height:' + r.height + 'px;';
        }, 350);
      } else {
        $('tut-spot').style.cssText = 'display:none';
      }
      $('tut-text').innerHTML = step.text;
      $('tut-count').textContent = (i + 1) + ' / ' + TUT_STEPS.length;
      $('tut-prev').style.visibility = i === 0 ? 'hidden' : 'visible';
      $('tut-next').textContent = i === TUT_STEPS.length - 1 ? '完成' : '下一步';
      $('tutorial').classList.remove('hidden');
    }
    function startTutorial() {
      if (state.tutStep >= 0) return;
      state.tutStep = 0;
      tutGo(0);
    }
    function endTutorial() {
      state.tutStep = -1;
      localStorage.setItem('tutorial_seen', '1');
      $('tutorial').classList.add('hidden');
      $('tut-spot').style.cssText = 'display:none';
    }
    $('tut-next').addEventListener('click', function () { tutGo(state.tutStep + 1); });
    $('tut-prev').addEventListener('click', function () { tutGo(state.tutStep - 1); });
    $('tut-skip').addEventListener('click', endTutorial);
    $('tutorial-btn').addEventListener('click', function () {
      if (state.tutStep >= 0) endTutorial();
      else startTutorial();
    });

    /* ---------- 组件布局系统 ---------- */
    function layoutKey(page) { return 'layout_' + page; }
    function lockedKey() { return 'layout_locked'; }
    function getLayout(page) {
      try { return JSON.parse(localStorage.getItem(layoutKey(page))); }
      catch (e) { return null; }
    }
    function saveLayout(page, keys) { localStorage.setItem(layoutKey(page), JSON.stringify(keys)); }
    function isLocked() { return localStorage.getItem(lockedKey()) === '1'; }

    function widgetTemplate(page, key) {
      switch (key) {
        case 'index':
          return '<div class="widget" data-widget="index">' +
            '<div class="widget-head"><span class="drag-handle" title="拖拽排序">⠿</span>' +
            '<span class="w-title">各大指数</span><span class="w-sub" id="index-time"></span>' +
            '<span class="w-spacer"></span><button class="w-hide" title="隐藏组件">×</button></div>' +
            '<div class="widget-body"><div class="index-grid" id="index-grid"></div></div></div>';
        case 'limit-up':
          return '<div class="widget bigbox-up" data-widget="limit-up">' +
            '<div class="widget-head"><span class="drag-handle" title="拖拽排序">⠿</span>' +
            '<span class="w-title">涨停榜</span><span id="td-limit-up" class="l-val">--</span>' +
            '<span class="l-count" id="up-count"></span>' +
            '<span class="w-spacer"></span><button class="w-hide" title="隐藏组件">×</button></div>' +
            '<div class="widget-body"><div class="bigbox-list" id="up-list"></div>' +
            '<div id="pool-date" class="pool-date"></div></div></div>';
        case 'limit-down':
          return '<div class="widget bigbox-down" data-widget="limit-down">' +
            '<div class="widget-head"><span class="drag-handle" title="拖拽排序">⠿</span>' +
            '<span class="w-title">跌停榜</span><span id="td-limit-down" class="l-val">--</span>' +
            '<span class="l-count" id="down-count"></span>' +
            '<span class="w-spacer"></span><button class="w-hide" title="隐藏组件">×</button></div>' +
            '<div class="widget-body"><div class="bigbox-list" id="down-list"></div></div></div>';
        case 'news':
          return '<div class="widget" data-widget="news">' +
            '<div class="widget-head"><span class="drag-handle" title="拖拽排序">⠿</span>' +
            '<span class="w-title">个股新闻</span>' +
            '<span class="w-spacer"></span>' +
            '<button id="news-refresh" class="w-btn" title="刷新新闻">↻</button>' +
            '<button class="w-hide" title="隐藏组件">×</button></div>' +
            '<div class="widget-body"><div class="news-list" id="news-list"><div class="empty">加载中…</div></div></div></div>';
        case 'chart':
          return '<div class="widget widget-chart" data-widget="chart">' +
            '<div class="widget-head"><span class="drag-handle" title="拖拽排序">⠿</span>' +
            '<span id="td-name">--</span><span id="td-code"></span><span id="td-mkt"></span>' +
            '<span id="td-price">--</span><span id="td-chg">--</span>' +
            '<span class="w-spacer"></span>' +
            '<button id="fav-btn" class="fav-btn" title="收藏/取消收藏">☆</button></div>' +
            '<div class="widget-body">' +
            '<div class="panel-head"><span>今开 <b id="td-open">--</b></span><span>昨收 <b id="td-prev">--</b></span>' +
            '<span>最高 <b id="td-high">--</b></span><span>最低 <b id="td-low">--</b></span>' +
            '<span>成交量 <b id="td-vol">--</b></span><span>成交额 <b id="td-amt">--</b></span></div>' +
            '<div class="toolbar">' +
            '<span class="range-btn" data-range="20">20日</span>' +
            '<span class="range-btn active" data-range="50">50日</span>' +
            '<span class="range-btn" data-range="200">200日</span>' +
            '<span id="ma-info"></span>' +
            '<span class="hint">悬停查看明细 · 滚轮缩放 · 拖拽平移</span></div>' +
            '<div id="chartWrap"><div id="loading" class="hidden">' +
            '<div class="spinner"></div><div id="loading-text" style="color:var(--muted);font-size:13px">加载中…</div></div>' +
            '<div id="chart"></div></div>' +
            '<div id="statusbar"><span id="st-source">数据源: --</span>' +
            '<span id="st-trade">--</span><span id="st-time">--</span>' +
            '<span id="st-alert" class="hidden"></span></div></div></div>';
        default:
          return '';
      }
    }

    function initPage(page) {
      const box = $('widgets-' + page);
      const stored = getLayout(page);
      const keys = (stored && stored.length) ? stored : PAGES[page].def;
      if (!stored) saveLayout(page, keys);
      box.innerHTML = keys.map(function (k) { return widgetTemplate(page, k); }).join('');
      renderAddMenu(page);
    }

    function renderAddMenu(page) {
      const menu = document.querySelector('[data-menu="' + page + '"]');
      const current = $$('#' + 'widgets-' + page + ' .widget').map(function (w) { return w.dataset.widget; });
      menu.innerHTML = PAGES[page].avail.map(function (k) {
        const used = current.indexOf(k) >= 0;
        const nm = PAGES[page].names[k] || [k, ''];
        return '<div class="w-add-item' + (used ? ' disabled' : '') + '" data-add="' + k + '"' +
          (used ? ' title="已存在"' : '') + '>' +
          '<span><span class="wa-title">' + nm[0] + '</span><br><span class="wa-desc">' + nm[1] + '</span></span></div>';
      }).join('');
    }

    $$('.w-add-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        const menu = document.querySelector('[data-menu="' + b.dataset.addMenu + '"]');
        menu.classList.toggle('hidden');
      });
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.w-add')) {
        $$('.w-add-menu').forEach(function (m) { m.classList.add('hidden'); });
      }
    });
    document.addEventListener('click', function (e) {
      const item = e.target.closest('.w-add-item');
      if (!item || item.classList.contains('disabled')) return;
      const menu = item.parentElement;
      const page = menu.dataset.menu;
      const key = item.dataset.add;
      addWidget(page, key);
      menu.classList.add('hidden');
    });

    function addWidget(page, key) {
      const box = $('widgets-' + page);
      const keys = getLayout(page) || PAGES[page].def;
      if (keys.indexOf(key) >= 0) return;
      keys.push(key);
      box.insertAdjacentHTML('beforeend', widgetTemplate(page, key));
      saveLayout(page, keys);
      renderAddMenu(page);
      if (key === 'news') loadNews();
      if (key === 'index') loadIndices();
      if (key === 'limit-up' || key === 'limit-down') loadLimitPool();
      if (key === 'chart') initChart();
      bindWidgetButtons(page);
    }

    function hideWidget(page, key) {
      const box = $('widgets-' + page);
      const keys = (getLayout(page) || PAGES[page].def).filter(function (k) { return k !== key; });
      saveLayout(page, keys);
      const w = box.querySelector('[data-widget="' + key + '"]');
      if (w) w.remove();
      renderAddMenu(page);
    }

    function bindWidgetButtons(page) {
      const box = $('widgets-' + page);
      $$('.w-hide', box).forEach(function (b) {
        if (b.dataset.bound) return;
        b.dataset.bound = '1';
        b.addEventListener('click', function () {
          const w = b.closest('.widget');
          hideWidget(page, w.dataset.widget);
        });
      });
      const nr = box.querySelector('#news-refresh');
      if (nr && !nr.dataset.bound) { nr.dataset.bound = '1'; nr.addEventListener('click', loadNews); }
      const fb = box.querySelector('#fav-btn');
      if (fb && !fb.dataset.bound) { fb.dataset.bound = '1'; fb.addEventListener('click', toggleFav); }
    }

    /* 拖拽排序（指针事件，兼容鼠标与触摸） */
    function bindDrag(page) {
      const box = $('widgets-' + page);
      let dragEl = null, startY = 0, ghost = null, placeholder = null, moved = false;
      box.addEventListener('pointerdown', function (e) {
        if (isLocked()) return;
        const h = e.target.closest('.drag-handle');
        if (!h) return;
        const w = h.closest('.widget');
        if (!w) return;
        e.preventDefault();
        dragEl = w; startY = e.clientY; moved = false;
      });
      box.addEventListener('pointermove', function (e) {
        if (!dragEl) return;
        if (!moved && Math.abs(e.clientY - startY) < 6) return;
        moved = true;
        if (!ghost) {
          ghost = dragEl.cloneNode(true);
          ghost.style.cssText = 'position:fixed;left:0;right:0;top:' + e.clientY + 'px;opacity:0.85;pointer-events:none;z-index:400;box-shadow:var(--shadow);';
          document.body.appendChild(ghost);
          placeholder = document.createElement('div');
          placeholder.style.cssText = 'height:' + dragEl.offsetHeight + 'px;border:2px dashed var(--accent);border-radius:var(--radius-lg);margin-bottom:14px;';
          dragEl.style.display = 'none';
          dragEl.parentElement.insertBefore(placeholder, dragEl);
        }
        ghost.style.top = e.clientY + 'px';
        const under = document.elementFromPoint(e.clientX, e.clientY);
        const target = under && under.closest('.widget');
        const siblings = $$('.widget', box).filter(function (w) { return w !== dragEl; });
        if (target) {
          const tr = target.getBoundingClientRect();
          box.insertBefore(placeholder, e.clientY < tr.top + tr.height / 2 ? target : target.nextSibling);
        } else if (siblings.length) {
          const last = siblings[siblings.length - 1];
          box.insertBefore(placeholder, last.nextSibling);
        }
      });
      box.addEventListener('pointerup', function () {
        if (!dragEl) return;
        if (moved && placeholder) {
          box.insertBefore(dragEl, placeholder);
          if (placeholder.parentElement) placeholder.remove();
          const keys = $$('.widget', box).map(function (w) { return w.dataset.widget; });
          saveLayout(page, keys);
        } else {
          dragEl.style.display = '';
        }
        if (ghost) ghost.remove();
        ghost = null; placeholder = null; dragEl = null; moved = false;
      });
      box.addEventListener('pointercancel', function () {
        if (dragEl) dragEl.style.display = '';
        if (ghost) ghost.remove();
        ghost = null; placeholder = null; dragEl = null; moved = false;
      });
    }

    /* 布局锁定 */
    function applyLock() {
      const locked = isLocked();
      state.layoutLocked = locked;
      $('layout-lock').textContent = locked ? '🔒 已锁定' : '🔓 锁定';
      $('layout-lock').classList.toggle('on', locked);
      $$('.widget').forEach(function (w) { w.classList.toggle('locked', locked); });
      $$('.w-add-btn').forEach(function (b) { b.style.display = locked ? 'none' : ''; });
      $$('.w-hide').forEach(function (b) { b.style.display = locked ? 'none' : ''; });
    }
    $('layout-lock').addEventListener('click', function () {
      localStorage.setItem(lockedKey(), isLocked() ? '0' : '1');
      applyLock();
    });

    /* ---------- 页面切换 ---------- */
    function setPage(i) {
      state.pageIdx = i;
      $('pages').style.transform = 'translateX(-' + (i * 100) + '%)';
      $$('#page-dots .dot').forEach(function (d, idx) { d.classList.toggle('active', idx === i); });
      $$('.nav-links a[data-page-link]').forEach(function (a) {
        a.classList.toggle('active', a.dataset.pageLink === (i === 0 ? 'monitor' : 'favs'));
      });
      if (state.chart) state.chart.resize();
    }
    $$('#page-dots .dot').forEach(function (d) {
      d.addEventListener('click', function () { setPage(+d.dataset.i); });
    });
    $$('.nav-links a[data-page-link]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        setPage(a.dataset.pageLink === 'monitor' ? 0 : 1);
      });
    });

    /* 横向滑动切页（拖拽手柄区域不触发） */
    (function bindSwipe() {
      const pages = $('pages');
      const total = document.querySelectorAll('.page').length;
      let dragStartX = 0, dragDX = 0, dragging = false;
      pages.addEventListener('pointerdown', function (e) {
        if (e.target.closest('.drag-handle')) return;
        dragging = true;
        dragStartX = e.clientX;
        dragDX = 0;
      });
      window.addEventListener('pointermove', function (e) {
        if (!dragging) return;
        dragDX = e.clientX - dragStartX;
        pages.style.transition = 'none';
        pages.style.transform = 'translateX(calc(-' + (state.pageIdx * 100) + '% + ' + dragDX + 'px))';
      });
      window.addEventListener('pointerup', function () {
        if (!dragging) return;
        dragging = false;
        pages.style.transition = '';
        if (Math.abs(dragDX) > 80) setPage(state.pageIdx + (dragDX < 0 ? 1 : -1));
        else setPage(state.pageIdx);
      });
      pages.addEventListener('pointercancel', function () { dragging = false; });
      pages.addEventListener('click', function (e) {
        const item = e.target.closest('.fitem');
        if (item) {
          selectStock({ code: item.dataset.code, name: item.dataset.name });
          setPage(1);
        }
      });
    })();

    /* ---------- 股票搜索 ---------- */
    const searchInput = $('searchInput'), dropdown = $('dropdown');
    function hideDropdown() { dropdown.classList.add('hidden'); state.ki = -1; }
    function showDropdown() { dropdown.classList.remove('hidden'); }
    dropdown.addEventListener('click', function (e) {
      const itemEl = e.target.closest('.s-item');
      if (!itemEl) return;
      const item = state.results[+itemEl.dataset.i];
      if (item) selectStock(item);
    });
    dropdown.addEventListener('mouseover', function (e) {
      const itemEl = e.target.closest('.s-item');
      if (!itemEl) return;
      state.ki = +itemEl.dataset.i;
      highlightItem();
    });
    function renderDropdown(results) {
      state.results = results; state.ki = -1;
      if (!results.length) {
        dropdown.innerHTML = '<div class="s-empty">未找到相关股票，试试 6 位代码 / 名称 / 美股代码</div>';
      } else {
        dropdown.innerHTML = results.map(function (r, i) {
          const mkt = r.market === 'us' ? '美股' : (r.market === 'sh' ? '沪A' : '深A');
          return '<div class="s-item" data-i="' + i + '">' +
            '<span class="s-name">' + esc(r.name) + '</span>' +
            '<span class="s-code">' + esc(r.code) + '</span>' +
            '<span class="s-mkt ' + r.market + '">' + mkt + '</span></div>';
        }).join('');
      }
      dropdown.classList.remove('hidden');
    }
    function highlightItem() {
      dropdown.querySelectorAll('.s-item').forEach(function (el, i) {
        el.classList.toggle('active', i === state.ki);
      });
    }
    let searchTimer = null;
    searchInput.addEventListener('input', function () {
      clearTimeout(searchTimer);
      const q = this.value.trim();
      if (!q) { hideDropdown(); return; }
      searchTimer = setTimeout(function () { doSearch(q); }, 250);
    });
    searchInput.addEventListener('keydown', function (e) {
      const n = state.results.length;
      if (e.key === 'ArrowDown') { e.preventDefault(); state.ki = Math.min(state.ki + 1, n - 1); highlightItem(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); state.ki = Math.max(state.ki - 1, 0); highlightItem(); }
      else if (e.key === 'Enter') {
        const idx = state.ki >= 0 ? state.ki : 0;
        if (state.results[idx]) selectStock(state.results[idx]);
      }
      else if (e.key === 'Escape') hideDropdown();
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.search-box')) hideDropdown();
    });
    async function doSearch(q) {
      try {
        const j = await getJSON('/api/search?q=' + encodeURIComponent(q));
        renderDropdown(j.results || []);
      } catch (e) {
        $('dropdown').innerHTML = '<div class="s-empty">搜索失败，请检查网络</div>';
        showDropdown();
      }
    }

    /* ---------- 新闻 ---------- */
    function loadNews() {
      const list = $('news-list');
      if (!list) return;
      const reqCode = state.curCode;
      list.innerHTML = '<div class="empty">加载中…</div>';
      getJSON('/api/news').then(function (j) {
        if (reqCode !== state.curCode) return;
        if (j.error || j.ok === false) { list.innerHTML = '<div class="empty">新闻加载失败</div>'; return; }
        if (!j.items || !j.items.length) { list.innerHTML = '<div class="empty">暂无相关新闻</div>'; return; }
        list.innerHTML = '';
        j.items.forEach(function (n) {
          const a = document.createElement('a');
          a.className = 'nitem';
          a.href = n.url || '#';
          a.target = '_blank';
          a.rel = 'noopener';
          const t = document.createElement('span');
          t.className = 'n-title';
          t.textContent = n.title;
          const m = document.createElement('span');
          m.className = 'n-meta';
          m.textContent = (n.time ? n.time.slice(5, 16) + ' · ' : '') + n.source;
          a.appendChild(t);
          a.appendChild(m);
          list.appendChild(a);
        });
      }).catch(function () {
        list.innerHTML = '<div class="empty">新闻加载失败，请检查网络</div>';
      });
    }

    /* ---------- 收藏 ---------- */
    function getFavs() {
      try { return JSON.parse(localStorage.getItem('favs') || '[]'); } catch (e) { return []; }
    }
    function saveFavs(list) { localStorage.setItem('favs', JSON.stringify(list)); }
    function updateFavBtn() {
      const b = $('fav-btn');
      if (!b) return;
      const on = getFavs().some(function (f) { return f.code === state.curCode; });
      b.textContent = on ? '★' : '☆';
      b.classList.toggle('on', on);
    }
    function toggleFav() {
      const list = getFavs(), i = list.findIndex(function (f) { return f.code === state.curCode; });
      if (i >= 0) list.splice(i, 1);
      else list.push({ code: state.curCode, name: state.stockName, market: state.market });
      saveFavs(list);
      updateFavBtn();
      renderFavs();
    }
    function renderFavs() {
      const list = getFavs();
      $('fav-count').textContent = list.length ? list.length + ' 只' : '';
      $('fav-list').innerHTML = list.length ? list.map(function (f) {
        return '<div class="fitem" data-code="' + esc(f.code) + '" data-name="' + esc(f.name || '') + '">' +
          '<span class="f-name">' + esc(f.name || f.code) + '</span>' +
          '<span class="f-code">' + esc(f.code) + '</span>' +
          '<span class="f-mkt">' + esc(f.market || '') + '</span></div>';
      }).join('') : '<div class="empty">暂无收藏，在股票监控组件点击 ☆ 收藏</div>';
    }
    $('toggle-favs').addEventListener('click', function () {
      const side = $('favs-side'), hidden = side.classList.toggle('hidden');
      this.textContent = hidden ? '展开 ▸' : '收起 ▾';
    });

    /* ---------- 标的切换 ---------- */
    async function selectStock(item) {
      hideDropdown();
      searchInput.value = '';
      setLoading(true, '正在加载 ' + item.name + ' 的行情…');
      try {
        const r = await getJSON('/api/switch?code=' + item.code + '&name=' + encodeURIComponent(item.name));
        if (r.error) throw new Error(r.error);
        state.stockName = r.name;
        state.market = r.market;
        state.curCode = r.symbol;
        $('td-name').textContent = r.name;
        $('td-code').textContent = r.symbol;
        $('td-mkt').textContent = r.market.toUpperCase();
        if (r.market === 'us') {
          $$('.bigbox-up, .bigbox-down').forEach(function (w) { w.style.display = 'none'; });
          $('td-limit-up').textContent = '--';
          $('td-limit-down').textContent = '--';
        } else {
          $$('.bigbox-up, .bigbox-down').forEach(function (w) {
            const key = w.dataset.widget;
            const keys = getLayout('monitor') || PAGES.monitor.def;
            w.style.display = keys.indexOf(key) >= 0 ? '' : 'none';
          });
        }
        updateFavBtn();
        loadNews();
        setPage(1);
        await loadKline(true);
        poll();
      } catch (e) {
        showAlert('切换标的失败: ' + e.message);
      } finally {
        setLoading(false);
      }
    }

    /* ---------- 图表 ---------- */
    function initChart() {
      const el = $('chart');
      if (!el || state.chart) return;
      const st = getComputedStyle(document.documentElement);
      const axisC = st.getPropertyValue('--axis').trim() || '#888';
      const gridC = st.getPropertyValue('--grid').trim() || '#2A2A2A';
      const mutedC = st.getPropertyValue('--muted').trim() || '#999';
      const chart = echarts.init(el);
      const base = {
        backgroundColor: 'transparent',
        animation: false,
        legend: { data: ['日K', 'MA5', 'MA20', '成交量'], top: 6, right: 16,
                  textStyle: { color: mutedC, fontSize: 11 } },
        grid: [
          { left: 72, right: 88, top: 34, bottom: '32%' },
          { left: 72, right: 88, top: '72%', bottom: '10%' }
        ],
        xAxis: [
          { type: 'category', data: [], gridIndex: 0,
            axisLine: { lineStyle: { color: gridC } },
            axisLabel: { color: axisC, fontSize: 10, formatter: function (v) { return v.slice(5); } } },
          { type: 'category', data: [], gridIndex: 1,
            axisLine: { lineStyle: { color: gridC } }, axisLabel: { show: false } }
        ],
        yAxis: [
          { scale: true, gridIndex: 0, position: 'left',
            splitLine: { lineStyle: { color: gridC, type: 'dashed' } },
            axisLabel: { color: axisC, fontSize: 10 }, axisLine: { show: false } },
          { scale: true, gridIndex: 1, position: 'left',
            splitLine: { show: false },
            axisLabel: { color: axisC, fontSize: 10,
                         formatter: function (v) { return v >= 10000 ? (v/10000) + '万' : v; } } }
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
          { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18,
            borderColor: gridC, backgroundColor: 'transparent',
            fillerColor: 'rgba(239,35,42,0.15)', textStyle: { color: mutedC, fontSize: 9 } }
        ],
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross', lineStyle: { color: axisC },
                         label: { backgroundColor: '#444', fontSize: 10 } },
          backgroundColor: st.getPropertyValue('--tooltip-bg').trim() || 'rgba(0,0,0,0.85)',
          borderColor: st.getPropertyValue('--tooltip-border').trim() || '#444',
          textStyle: { color: st.getPropertyValue('--tooltip-text').trim() || '#DDD', fontSize: 12 },
          formatter: function (params) {
            const d = state.data;
            if (!d || !params.length) return '';
            const i = params[0].dataIndex;
            const k = d.ohlc[i];
            const o = k[0], c = k[1], l = k[2], h = k[3];
            const prev = i > 0 ? d.ohlc[i - 1][1] : o;
            const chg = c - prev;
            const pct = prev ? chg / prev * 100 : 0;
            const col = chg > 0 ? UP : (chg < 0 ? DOWN : FLAT);
            const sign = chg >= 0 ? '+' : '';
            const ma5 = d.ma.ma5 ? d.ma.ma5[i] : null;
            const ma20 = d.ma.ma20 ? d.ma.ma20[i] : null;
            return '<div style="line-height:1.7">' +
              '<b>' + d.dates[i] + '</b><br>' +
              '开 ' + fmtPrice(o) + '　高 <span style="color:' + barColor(c, o) + '">' + fmtPrice(h) + '</span><br>' +
              '低 ' + fmtPrice(l) + '　收 <span style="color:' + barColor(c, o) + '">' + fmtPrice(c) + '</span><br>' +
              '涨跌 <span style="color:' + col + '">' + sign + fmtPrice(chg) + ' (' + sign + fmtPrice(pct) + '%)</span><br>' +
              '量 ' + fmtVol(d.volume[i]) + '　额 ' + fmtAmount(d.amount ? d.amount[i] : null) + '<br>' +
              'MA5 ' + fmtPrice(ma5) + '　MA20 ' + fmtPrice(ma20) +
              '</div>';
          }
        },
        series: [
          { name: '日K', type: 'candlestick', data: [], xAxisIndex: 0, yAxisIndex: 0,
            itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN } },
          { name: 'MA5', type: 'line', data: [], xAxisIndex: 0, yAxisIndex: 0,
            symbol: 'none', smooth: true, lineStyle: { width: 1.2 }, itemStyle: { color: '#F9D71C' } },
          { name: 'MA20', type: 'line', data: [], xAxisIndex: 0, yAxisIndex: 0,
            symbol: 'none', smooth: true, lineStyle: { width: 1.2 }, itemStyle: { color: '#A967FF' } },
          { name: '成交量', type: 'bar', data: [], xAxisIndex: 1, yAxisIndex: 1, barWidth: '60%' }
        ]
      };
      chart.setOption(base);
      state.chart = chart;
    }

    function buildVolumeData() {
      const d = state.data;
      return d.ohlc.map(function (k, i) {
        return { value: d.volume[i], itemStyle: { color: barColor(k[1], k[0]) } };
      });
    }
    function recomputeLastMa(arr, n) {
      const L = state.data.ohlc.length;
      if (L < n) return null;
      let s = 0;
      for (let i = L - n; i < L; i++) s += state.data.ohlc[i][1];
      return +(s / n).toFixed(2);
    }
    function render() {
      const d = state.data;
      if (!d || !state.chart) return;
      const n = d.count;
      const take = Math.min(state.range, n);
      const start = Math.max(0, Math.round(100 * (1 - take / n)));
      state.chart.setOption({
        xAxis: [{ data: d.dates }, { data: d.dates }],
        series: [
          { data: d.ohlc }, { data: d.ma.ma5 || [] }, { data: d.ma.ma20 || [] },
          { data: buildVolumeData() }
        ],
        dataZoom: [{ start: start, end: 100 }, { start: start, end: 100 }]
      });
      const ma5v = d.ma.ma5 ? d.ma.ma5[n - 1] : null;
      const ma20v = d.ma.ma20 ? d.ma.ma20[n - 1] : null;
      $('ma-info').innerHTML =
        '<span style="color:#F9D71C">MA5 ' + fmtPrice(ma5v) + '</span>' +
        '&nbsp;&nbsp;<span style="color:#A967FF">MA20 ' + fmtPrice(ma20v) + '</span>';
    }

    async function loadKline(force) {
      try {
        setLoading(true, force ? '正在加载行情…' : '加载中…');
        const j = await getJSON('/api/kline' + (force ? '?refresh=1' : ''));
        if (j.error) throw new Error(j.error);
        state.data = j;
        $('td-name').textContent = j.name || state.stockName;
        $('td-code').textContent = j.code;
        render();
      } catch (e) {
        showAlert('历史数据加载失败: ' + e.message);
      } finally {
        setLoading(false);
      }
    }

    function updateTopbar(j) {
      const up = j.change >= 0;
      const col = j.change > 0 ? UP : (j.change < 0 ? DOWN : FLAT);
      $('td-price').textContent = fmtPrice(j.price);
      $('td-price').style.color = col;
      $('td-chg').textContent = (up ? '+' : '') + fmtPrice(j.change) + '  ' +
                                (up ? '+' : '') + fmtPrice(j.pct) + '%';
      $('td-chg').style.color = col;
      $('td-open').textContent = fmtPrice(j.open);
      $('td-prev').textContent = fmtPrice(j.prev_close);
      $('td-high').textContent = fmtPrice(j.high);
      $('td-low').textContent = fmtPrice(j.low);
      $('td-vol').textContent = fmtVol(j.volume);
      $('td-amt').textContent = fmtAmount(j.amount);
      if (j.limit_up != null && j.limit_down != null) {
        $('td-limit-up').textContent = fmtPrice(j.limit_up);
        $('td-limit-down').textContent = fmtPrice(j.limit_down);
        $('td-limit-up').style.color = j.price >= j.limit_up ? '#FFD93D' : '';
        $('td-limit-down').style.color = j.price <= j.limit_down ? '#FFD93D' : '';
      } else {
        $('td-limit-up').textContent = '--';
        $('td-limit-down').textContent = '--';
        $('td-limit-up').style.color = '';
        $('td-limit-down').style.color = '';
      }
      $('st-source').textContent = '数据源: ' + j.source + (j.cached ? '（缓存）' : '');
      $('st-source').style.color = j.cached ? '#FFB020' : 'var(--dim)';
      $('st-trade').textContent = j.trade_status;
      $('st-time').textContent = j.timestamp ? '最后更新 ' + j.timestamp.slice(11) : '';
    }

    function updateLastBar(j) {
      const d = state.data;
      if (!d || !d.ohlc.length) return;
      const last = d.ohlc[d.ohlc.length - 1];
      last[1] = j.price;
      last[0] = j.open || last[0];
      last[2] = Math.min(last[2], j.price);
      last[3] = Math.max(last[3], j.price);
      d.ohlc[d.ohlc.length - 1] = last;
      d.volume[d.volume.length - 1] = j.volume;
      if (d.ma.ma5) d.ma.ma5[d.ma.ma5.length - 1] = recomputeLastMa(d.ma.ma5, state.ma[0]);
      if (d.ma.ma20) d.ma.ma20[d.ma.ma20.length - 1] = recomputeLastMa(d.ma.ma20, state.ma[1]);
      state.chart.setOption({
        series: [{ data: d.ohlc }, { data: d.ma.ma5 }, { data: d.ma.ma20 }, { data: buildVolumeData() }]
      });
      $('ma-info').innerHTML =
        '<span style="color:#F9D71C">MA5 ' + fmtPrice(d.ma.ma5[d.ma.ma5.length - 1]) + '</span>' +
        '&nbsp;&nbsp;<span style="color:#A967FF">MA20 ' + fmtPrice(d.ma.ma20[d.ma.ma20.length - 1]) + '</span>';
    }

    function schedulePoll(tradeStatus) {
      let ms = state.pollMs;
      if (tradeStatus === '午间休市' || tradeStatus === '已收盘' || tradeStatus === '休市日') ms = 30000;
      if (state.pollTimer) clearInterval(state.pollTimer);
      state.pollTimer = setInterval(poll, ms);
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
        if (state.indexTimer) { clearInterval(state.indexTimer); state.indexTimer = null; }
      } else {
        if (state.pollTimer === null) { poll(); schedulePoll(); }
        if (state.indexTimer === null) { loadIndices(); scheduleIndices(); }
      }
    });
    async function poll() {
      const reqCode = state.curCode;
      try {
        const j = await getJSON('/api/realtime');
        if (reqCode !== state.curCode) return;
        if (j.error) { showAlert(j.error); return; }
        hideAlert();
        updateTopbar(j);
        if ((j.trade_status === '交易中' || j.trade_status === '集合竞价') &&
            state.data && j.price != null) {
          updateLastBar(j);
        }
        schedulePoll(j.trade_status);
      } catch (e) {
        showAlert('行情请求失败，稍后自动重试');
        schedulePoll();
      }
    }

    /* ---------- 涨跌榜 ---------- */
    function renderPool(list, el, isUp) {
      if (!list || !list.length) {
        el.innerHTML = '<div class="empty">今日暂无' + (isUp ? '涨停' : '跌停') + '股票</div>';
        return;
      }
      el.innerHTML = list.map(function (s) {
        return '<div class="pitem" data-code="' + esc(s.code) + '" data-name="' + esc(s.name) + '">' +
          '<span class="p-name">' + esc(s.name) + '</span>' +
          '<span class="p-code">' + esc(s.code) + '</span>' +
          '<span class="p-lbc">' + (isUp && s.lbc > 1 ? s.lbc + '连板' : '') + '</span>' +
          '<span class="p-pct">' + (s.pct > 0 ? '+' : '') + s.pct + '%</span>' +
          '<span class="p-price">' + fmtPrice(s.price) + '</span></div>';
      }).join('');
    }
    let limitPoolErr = 0;
    function scheduleLimitPool() {
      const base = 60000;
      const delay = base * Math.pow(2, Math.min(limitPoolErr, 2));
      setTimeout(loadLimitPool, delay);
    }
    async function loadLimitPool() {
      const hasUp = !!$('up-list');
      if (!hasUp) return;
      if (state.market === 'us') {
        $$('.bigbox-up, .bigbox-down').forEach(function (w) { w.style.display = 'none'; });
        return;
      }
      try {
        const j = await getJSON('/api/limit-pool');
        if (j.error) throw new Error(j.error);
        renderPool(j.up, $('up-list'), true);
        if ($('down-list')) renderPool(j.down, $('down-list'), false);
        if ($('up-count')) $('up-count').textContent = j.up_count + ' 只';
        if ($('down-count')) $('down-count').textContent = j.down_count + ' 只';
        const d = j.date, ds = d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6);
        if ($('pool-date')) $('pool-date').textContent = '最近交易日 ' + ds;
        limitPoolErr = 0;
        scheduleLimitPool();
      } catch (e) {
        limitPoolErr++;
        if ($('up-list')) $('up-list').innerHTML = '<div class="empty">加载失败，请检查网络</div>';
        if ($('down-list')) $('down-list').innerHTML = '<div class="empty">加载失败，请检查网络</div>';
        scheduleLimitPool();
      }
    }
    ['up-list', 'down-list'].forEach(function (id) {
      document.addEventListener('click', function (e) {
        if (!e.target.closest('#' + id)) return;
        const item = e.target.closest('.pitem');
        if (item) selectStock({ code: item.dataset.code, name: item.dataset.name });
      });
    });

    /* ---------- 指数组件 ---------- */
    function scheduleIndices() {
      if (state.indexTimer) clearInterval(state.indexTimer);
      state.indexTimer = setInterval(loadIndices, 10000);
    }
    async function loadIndices() {
      const grid = $('index-grid');
      if (!grid) return;
      try {
        const j = await getJSON('/api/indices');
        if (j.error || !j.items) { grid.innerHTML = '<div class="empty">指数行情加载失败</div>'; return; }
        grid.innerHTML = j.items.map(function (it) {
          const col = it.pct > 0 ? 'up-c' : (it.pct < 0 ? 'down-c' : 'flat-c');
          const sign = it.pct >= 0 ? '+' : '';
          return '<div class="index-card">' +
            '<div class="ic-name">' + esc(it.name) + '</div>' +
            '<div class="ic-price ' + col + '">' + fmtPrice(it.price) + '</div>' +
            '<div class="ic-chg ' + col + '">' + sign + fmtPrice(it.change) + ' (' + sign + fmtPrice(it.pct) + '%)</div></div>';
        }).join('');
        const t = j.items[0] && j.items[0].timestamp;
        if (t && t !== '-') $('index-time').textContent = '更新 ' + t;
        else $('index-time').textContent = '';
      } catch (e) {
        grid.innerHTML = '<div class="empty">指数行情加载失败</div>';
      }
    }

    /* ---------- 区间切换 / resize ---------- */
    document.addEventListener('click', function (e) {
      const b = e.target.closest('.range-btn');
      if (!b) return;
      $$('.range-btn').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      state.range = +b.dataset.range;
      render();
    });
    window.addEventListener('resize', function () { if (state.chart) state.chart.resize(); });

    /* ---------- 启动 ---------- */
    async function loadConfig() {
      try {
        const c = await getJSON('/api/config');
        if (c.error) return;
        state.range = c.default_range || 50;
        state.ma = c.ma || [5, 20];
        state.pollMs = (c.refresh_interval_sec || 5) * 1000;
        state.stockName = c.name;
        state.market = c.market;
        state.curCode = c.symbol;
        $('td-name').textContent = c.name;
        $('td-code').textContent = c.symbol;
        $('td-mkt').textContent = c.market.toUpperCase();
        if (c.market === 'us') {
          $$('.bigbox-up, .bigbox-down').forEach(function (w) { w.style.display = 'none'; });
        }
        updateFavBtn();
        loadNews();
        $$('.range-btn').forEach(function (b) {
          b.classList.toggle('active', +b.dataset.range === state.range);
        });
      } catch (e) { }
    }

    function boot() {
      applyTheme(currentTheme());
      initPage('monitor');
      initPage('favs');
      bindWidgetButtons('monitor');
      bindWidgetButtons('favs');
      bindDrag('monitor');
      bindDrag('favs');
      applyLock();
      renderFavs();
      initChart();
      maybeShowIntro();
      loadIndices(); scheduleIndices();
      loadLimitPool();
      loadConfig().then(loadKline).then(poll);
    }
    boot();
  </script>
</body>
</html>
"""

# 主入口
# --------------------------------------------------------------
def preload_history() -> None:
    """后台预热历史数据，首次打开页面时无需等待拉取。"""
    try:
        get_managers()[1].get()
    except Exception as e:
        logger.warning("[预热] 历史数据预拉取失败: %s", e)


def main():
    parser = argparse.ArgumentParser(description="股票监控 v0.3.1（测试版）")
    parser.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL,
                        help="股票代码，如 300408 / 600519 / AAPL（美股），默认三环集团")
    parser.add_argument("-p", "--port", type=int, default=config["port"], help="服务端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    code = normalize_symbol(args.symbol)
    if code is None:
        print(f"无效股票代码: {args.symbol!r}（示例: 300408 / AAPL）", file=sys.stderr)
        sys.exit(1)
    config["symbol"] = code
    config["market"] = market_of(code)

    ensure_echarts()
    threading.Thread(target=preload_history, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print("=" * 56)
    print(f"  股票监控 v0.3.1  监控标的: {config['name']} ({code})")
    print(f"  浏览器访问: {url}")
    if config["market"] == "us":
        print(f"  数据源: Yahoo Finance（免费接口）")
    else:
        print(f"  数据源: 东方财富 / 新浪 / 腾讯（免费接口，自动降级）")
    print("  Ctrl+C 退出")
    print("=" * 56)

    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()