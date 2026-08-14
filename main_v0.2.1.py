#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票监控软件 v0.2.1（测试版）
============================

功能：
  - 股票搜索：支持按股票代码或名称搜索（新浪建议接口为主，东财为备用）
  - 单只股票监控（默认三环集团 sz300408，可通过搜索框切换标的）
  - 日 K 线蜡烛图（前复权），支持 20 / 50 / 200 交易日区间切换
  - 双均线 MA5 / MA20
  - 成交量副图（红涨绿跌，与同花顺配色一致）
  - 实时行情自动刷新（交易时段 5 秒，休息时段 30 秒）
  - 实时数据源三级降级：东方财富 → 新浪 → 腾讯（全部免费公开接口）
  - 历史 K 线数据源降级：东方财富 → 腾讯 → 新浪（前复权口径）
  - 历史 K 线本地 CSV 缓存，启动秒开
  - 起始页为项目简介页，左上角搜索框，深色主题美化界面
  - 本地 Web 界面（ECharts 深色主题），浏览器打开 http://127.0.0.1:8765

运行：
  python main_v0.2.1.py                # 默认监控三环集团（sz300408）
  python main_v0.2.1.py 600519         # 启动即监控贵州茅台
  python main_v0.2.1.py -p 9000        # 指定端口
  python main_v0.2.1.py --no-browser   # 不自动打开浏览器

依赖：
  pip install flask requests pandas
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
import webbrowser

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
    """归一化股票代码：600519 / 600519.SH / 600519.SS / SH600519 → 600519"""
    s = raw.strip().upper()
    for suf in (".SH", ".SS", ".SZ"):
        if s.endswith(suf):
            s = s[:-3]
            break
    if s.startswith(("SH", "SZ")):
        s = s[2:]
    if s.isdigit() and len(s) == 6:
        return s
    return None


def market_of(code: str) -> str:
    """6/5/9 开头为沪市，其余为深市"""
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


def fmt_price(v: float | None) -> str:
    """格式化价格显示：None 返回占位符 “—”，否则保留两位小数。"""
    return "—" if v is None else f"{v:.2f}"


def limit_pct(code: str) -> float:
    """单日涨跌幅容差（%）：创业板(3 开头)/科创板(68 开头)为 20%，主板 10%"""
    return 22.0 if code.startswith("3") or code.startswith("68") else 12.0


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
# 股票搜索（按代码或名称）
# --------------------------------------------------------------
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
    """按代码或名称搜索 A 股，新浪为主、东财备用。
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
    return results[:limit]

REALTIME_FETCHERS = {
    "eastmoney": fetch_realtime_eastmoney,
    "sina": fetch_realtime_sina,
    "tencent": fetch_realtime_tencent,
}

# 历史 K 线数据源优先级（eastmoney 支持标准前复权参数，腾讯/新浪为备用）
KLINE_FETCHERS = [
    ("eastmoney", fetch_kline_eastmoney),
    ("tencent", fetch_kline_tencent),
    ("sina", fetch_kline_sina),
]

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
            for name, fn in KLINE_FETCHERS:
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
        _realtime_mgr = RealtimeManager(config["realtime_sources"], config["fail_threshold"])
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
    global _history_mgr
    config["symbol"] = code
    config["market"] = market_of(code)
    if name:
        config["name"] = name
    _history_mgr = HistoryManager(code, config["history_days"], config["cache_ttl_sec"])
    logger.info("[switch] 监控标的切换为 %s（%s）", config["name"], code)
    return jsonify({"ok": True, "symbol": code, "name": config["name"], "market": config["market"]})


@app.route("/api/realtime")
def api_realtime():
    rt, _ = get_managers()
    data = rt.fetch(config["symbol"])
    if data is None:
        return jsonify({"error": "所有数据源均失败", "trade_status": trade_status()}), 503
    data["trade_status"] = trade_status()
    return jsonify(data)


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
<title>股票监控 v0.2.1 - 简介</title>
<style>
  :root {
    --bg: #0E1117; --bg2: #141923; --card: #1A202C; --card2: #1F2633;
    --border: #28303F; --text: #D5DAE3; --muted: #7C8495; --dim: #5A6272;
    --up: #EF232A; --down: #14B143; --gold: #F9D71C; --purple: #A967FF;
    --accent: #EF232A; --radius: 10px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { background: var(--bg); color: var(--text);
         font-family: "PingFang SC", "Microsoft YaHei", -apple-system, sans-serif;
         font-size: 14px; }
  a { color: inherit; text-decoration: none; }

  /* ---------- 顶部导航 ---------- */
  nav { position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 56px;
        display: flex; align-items: center; gap: 24px; padding: 0 24px;
        background: rgba(14,17,23,0.85); backdrop-filter: blur(12px);
        border-bottom: 1px solid var(--border); }
  .logo { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 700; color: #FFF; }
  .logo svg { display: block; }
  .ver { font-size: 10px; color: var(--gold); border: 1px solid var(--gold);
         border-radius: 4px; padding: 1px 5px; margin-left: 2px; font-weight: 600; }
  .search-box { position: relative; flex: 0 1 420px; margin-left: 8px; }
  .search-box .s-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--dim); }
  #searchInput { width: 100%; height: 36px; border-radius: 18px; border: 1px solid var(--border);
                 background: var(--bg2); color: var(--text); font-size: 13px;
                 padding: 0 16px 0 36px; outline: none; transition: border-color .2s, box-shadow .2s; }
  #searchInput::placeholder { color: var(--dim); }
  #searchInput:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(239,35,42,0.15); }
  .nav-links { margin-left: auto; display: flex; gap: 20px; color: var(--muted); font-size: 13px; }
  .nav-links a:hover { color: #FFF; }

  .dropdown { position: absolute; top: 42px; left: 0; right: 0; z-index: 200;
              background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
              box-shadow: 0 12px 32px rgba(0,0,0,0.5); overflow: hidden; }
  .dropdown.hidden { display: none; }
  .s-item { display: flex; align-items: center; gap: 10px; padding: 9px 14px; cursor: pointer;
            border-bottom: 1px solid rgba(40,48,63,0.5); }
  .s-item:last-child { border-bottom: none; }
  .s-item:hover, .s-item.active { background: rgba(239,35,42,0.10); }
  .s-name { font-size: 13px; color: var(--text); }
  .s-code { font-size: 12px; color: var(--muted); font-family: Menlo, monospace; }
  .s-mkt { margin-left: auto; font-size: 10px; color: var(--muted); border: 1px solid var(--border);
           border-radius: 3px; padding: 1px 5px; }
  .s-mkt.sh { color: #7FB4FF; border-color: #3A5A8C; }
  .s-mkt.sz { color: #FFB46E; border-color: #8C6A3A; }
  .s-empty { padding: 12px 14px; color: var(--dim); font-size: 12px; }

  /* ---------- 简介区 ---------- */
  .hero { max-width: 1060px; margin: 0 auto; padding: 120px 24px 40px; text-align: center; }
  .hero h1 { font-size: 38px; font-weight: 800; letter-spacing: 1px;
             background: linear-gradient(120deg, #FFF 20%, #F87171 45%, var(--gold) 70%, var(--purple) 95%);
             -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
  .hero .tagline { margin: 14px auto 0; max-width: 620px; color: var(--muted); line-height: 1.8; font-size: 14px; }
  .feature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                  gap: 14px; margin-top: 36px; text-align: left; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
          padding: 18px; display: flex; gap: 14px; align-items: flex-start;
          transition: transform .2s, border-color .2s, box-shadow .2s; }
  .card:hover { transform: translateY(-3px); border-color: #3A4456; box-shadow: 0 10px 24px rgba(0,0,0,0.35); }
  .card .icon { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 9px;
                display: flex; align-items: center; justify-content: center;
                background: rgba(239,35,42,0.12); }
  .card h3 { font-size: 14px; margin-bottom: 5px; color: #FFF; }
  .card p { font-size: 12px; color: var(--muted); line-height: 1.6; }
  .cta { margin-top: 34px; display: inline-block; padding: 11px 30px; border-radius: 22px;
         font-size: 14px; font-weight: 600; color: #FFF;
         background: linear-gradient(120deg, #EF232A, #C81D24);
         box-shadow: 0 8px 20px rgba(239,35,42,0.3); transition: transform .2s, box-shadow .2s; }
  .cta:hover { transform: translateY(-2px); box-shadow: 0 12px 28px rgba(239,35,42,0.45); }

  /* ---------- 监控区 ---------- */
  #monitor { max-width: 1060px; margin: 0 auto; padding: 24px; }
  .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px;
           overflow: hidden; box-shadow: 0 16px 40px rgba(0,0,0,0.4); }
  .panel-head { display: flex; align-items: baseline; gap: 10px; padding: 12px 16px;
                border-bottom: 1px solid var(--border); }
  #td-name { font-size: 20px; font-weight: 700; color: #FFF; }
  #td-code { color: var(--muted); font-family: Menlo, monospace; font-size: 13px; }
  #td-mkt { font-size: 10px; color: #7FB4FF; border: 1px solid #3A5A8C; border-radius: 3px; padding: 1px 5px; }
  #td-price { font-size: 28px; font-weight: 700; font-family: Menlo, monospace; margin-left: 14px; }
  #td-chg { font-size: 15px; font-family: Menlo, monospace; }
  .stats { display: flex; gap: 18px; padding: 8px 16px; border-bottom: 1px solid var(--border);
           color: var(--dim); font-size: 12px; flex-wrap: wrap; }
  .stats b { color: var(--text); font-weight: 500; font-family: Menlo, monospace; }
  .toolbar { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
             border-bottom: 1px solid var(--border); }
  .range-btn { background: transparent; color: var(--muted); border: 1px solid var(--border);
               border-radius: 6px; padding: 4px 14px; cursor: pointer; font-size: 12px;
               transition: all .2s; }
  .range-btn:hover { color: #FFF; border-color: var(--dim); }
  .range-btn.active { background: var(--accent); color: #FFF; border-color: var(--accent); font-weight: 600; }
  #ma-info { font-family: Menlo, monospace; font-size: 12px; margin-left: 10px; }
  .toolbar .hint { margin-left: auto; color: var(--dim); font-size: 12px; }
  #chartWrap { position: relative; height: 500px; }
  #chart { width: 100%; height: 100%; }
  #loading { position: absolute; inset: 0; display: flex; flex-direction: column; gap: 10px;
             align-items: center; justify-content: center; background: rgba(14,17,23,0.6); z-index: 50; }
  #loading.hidden { display: none; }
  .spinner { width: 34px; height: 34px; border-radius: 50%;
             border: 3px solid var(--border); border-top-color: var(--accent);
             animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #statusbar { display: flex; gap: 24px; padding: 8px 16px; border-top: 1px solid var(--border);
               font-size: 12px; color: var(--muted); }
  #st-alert { color: #FF5555; font-weight: 600; }
  #st-alert.hidden { display: none; }

  footer { max-width: 1060px; margin: 24px auto 40px; padding: 0 24px;
           color: var(--dim); font-size: 12px; text-align: center; line-height: 1.8; }
  footer .sep { color: var(--border); margin: 0 6px; }
  @media (max-width: 720px) {
    .hero h1 { font-size: 26px; } .nav-links { display: none; } #chartWrap { height: 380px; }
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
      <span>股票监控</span><span class="ver">v0.2.1</span>
    </div>
    <div class="search-box">
      <svg class="s-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
        <circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>
      </svg>
      <input id="searchInput" type="text" placeholder="输入股票代码或名称，如 600519 / 茅台 / 三环" autocomplete="off">
      <div id="dropdown" class="dropdown hidden"></div>
    </div>
    <div class="nav-links">
      <a href="#hero">简介</a><a href="#monitor">实时监控</a><a href="#footer">说明</a>
    </div>
  </nav>

  <header class="hero" id="hero">
    <h1>轻量 · 免费 · 同花顺风格的股票监控</h1>
    <p class="tagline">本地运行的股票监控测试版：K 线蜡烛图、双均线、三档时间区间、实时行情，
      数据全部来自免费公开接口，支持按代码或名称搜索切换监控标的。</p>
    <div class="feature-grid">
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#EF232A" stroke-width="2"><path d="M4 20V10 M9 20V4 M14 20V13 M19 20V7"/></svg></div>
        <div><h3>K 线蜡烛图</h3><p>前复权日 K，红涨绿跌，与同花顺配色一致，悬停显示完整 OHLC 明细</p></div>
      </div>
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#F9D71C" stroke-width="2"><path d="M3 17 L8 11 L13 14 L21 5"/><path d="M3 21 L8 15 L13 18 L21 9" opacity="0.6"/></svg></div>
        <div><h3>双均线叠加</h3><p>MA5（黄）+ MA20（紫）动态跟踪，金叉死叉一目了然</p></div>
      </div>
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#7FB4FF" stroke-width="2"><rect x="3" y="10" width="18" height="4" rx="1"/><rect x="5" y="6" width="14" height="3" rx="1"/><rect x="5" y="15" width="14" height="3" rx="1"/></svg></div>
        <div><h3>三档时间区间</h3><p>20 / 50 / 200 交易日一键切换，配合滚轮缩放与拖拽平移</p></div>
      </div>
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#14B143" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 3"/></svg></div>
        <div><h3>实时行情刷新</h3><p>交易时段 5 秒自动刷新，盘中最后一根 K 线随现价实时联动</p></div>
      </div>
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#A967FF" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg></div>
        <div><h3>多源自动容灾</h3><p>东方财富 / 新浪 / 腾讯三级降级，任一接口故障自动切换，不中断监控</p></div>
      </div>
      <div class="card">
        <div class="icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#F9D71C" stroke-width="2"><path d="M12 2l7 4v6c0 5-3 8-7 10-4-2-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg></div>
        <div><h3>完全免费零成本</h3><p>全部行情来自免费公开接口，无任何 key、无订阅费，本地缓存加速启动</p></div>
      </div>
    </div>
    <a class="cta" href="#monitor">进入实时监控 ↓</a>
  </header>

  <section id="monitor">
    <div class="panel">
      <div class="panel-head">
        <span id="td-name">--</span><span id="td-code"></span><span id="td-mkt"></span>
        <span id="td-price">--</span><span id="td-chg">--</span>
      </div>
      <div class="stats">
        <span>今开 <b id="td-open">--</b></span><span>昨收 <b id="td-prev">--</b></span>
        <span>最高 <b id="td-high">--</b></span><span>最低 <b id="td-low">--</b></span>
        <span>成交量 <b id="td-vol">--</b></span><span>成交额 <b id="td-amt">--</b></span>
      </div>
      <div class="toolbar">
        <span class="range-btn" data-range="20">20日</span>
        <span class="range-btn active" data-range="50">50日</span>
        <span class="range-btn" data-range="200">200日</span>
        <span id="ma-info"></span>
        <span class="hint">悬停查看明细 · 滚轮缩放 · 拖拽平移</span>
      </div>
      <div id="chartWrap">
        <div id="loading" class="hidden">
          <div class="spinner"></div>
          <div id="loading-text" style="color:var(--muted);font-size:13px">加载中…</div>
        </div>
        <div id="chart"></div>
      </div>
      <div id="statusbar">
        <span id="st-source">数据源: --</span>
        <span id="st-trade">--</span>
        <span id="st-time">--</span>
        <span id="st-alert" class="hidden"></span>
      </div>
    </div>
  </section>

  <footer id="footer">
    股票监控 v0.2.1 · 测试版<br>
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
                    pollTimer: null, chart: null, results: [], ki: -1, stockName: '' };

    function $(id) { return document.getElementById(id); }
    async function getJSON(url) {
      const r = await fetch(url);
      return r.json();
    }
    function fmtPrice(v, d) { if (v == null || isNaN(v)) return '--'; return Number(v).toFixed(d == null ? 2 : d); }
    function fmtVol(v) { if (v == null || isNaN(v)) return '--'; return v >= 10000 ? (v/10000).toFixed(2)+'万手' : v+'手'; }
    function fmtAmount(v) { if (v == null || isNaN(v)) return '--'; return v >= 1e8 ? (v/1e8).toFixed(2)+'亿' : (v/1e4).toFixed(0)+'万'; }
    function barColor(c, o) { return c > o ? UP : (c < o ? DOWN : FLAT); }
    function showAlert(m) { $('st-alert').textContent = m; $('st-alert').classList.remove('hidden'); }
    function hideAlert() { $('st-alert').classList.add('hidden'); }
    function setLoading(on, text) {
      const el = $('loading');
      if (el) el.classList.toggle('hidden', !on);
      if (text) { const t = $('loading-text'); if (t) t.textContent = text; }
    }

    /* ---------- 股票搜索 ---------- */
    const searchInput = $('searchInput'), dropdown = $('dropdown');

    function hideDropdown() { dropdown.classList.add('hidden'); state.ki = -1; }

    function renderDropdown(results) {
      state.results = results; state.ki = -1;
      if (!results.length) {
        dropdown.innerHTML = '<div class="s-empty">未找到相关股票，试试输入 6 位代码或名称</div>';
      } else {
        dropdown.innerHTML = results.map(function (r, i) {
          return '<div class="s-item" data-i="' + i + '">' +
            '<span class="s-name">' + r.name + '</span>' +
            '<span class="s-code">' + r.code + '</span>' +
            '<span class="s-mkt ' + r.market + '">' + (r.market === 'sh' ? '沪A' : '深A') + '</span></div>';
        }).join('');
        dropdown.querySelectorAll('.s-item').forEach(function (el) {
          el.addEventListener('click', function () {
            const item = state.results[+el.dataset.i];
            if (item) selectStock(item);
          });
          el.addEventListener('mouseenter', function () {
            state.ki = +el.dataset.i;
            highlightItem();
          });
        });
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
        const idx = state.ki >= 0 ? state.ki : 0;   // 未选中时默认选第一条
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
      } catch (e) { /* 忽略搜索错误 */ }
    }

    async function selectStock(item) {
      hideDropdown();
      searchInput.value = '';
      setLoading(true, '正在加载 ' + item.name + ' 的行情…');
      try {
        const r = await getJSON('/api/switch?code=' + item.code + '&name=' + encodeURIComponent(item.name));
        if (r.error) throw new Error(r.error);
        state.stockName = r.name;
        $('td-name').textContent = r.name;
        $('td-code').textContent = r.symbol;
        $('td-mkt').textContent = r.market.toUpperCase();
        $('monitor').scrollIntoView({ behavior: 'smooth' });
        await loadKline(true);
        poll();   // 立即刷新一次实时行情，避免等待下一轮轮询
      } catch (e) {
        showAlert('切换标的失败: ' + e.message);
      } finally {
        setLoading(false);
      }
    }

    /* ---------- 图表 ---------- */
    function initChart() {
      const chart = echarts.init($('chart'));
      const base = {
        backgroundColor: 'transparent',
        animation: false,
        legend: { data: ['日K', 'MA5', 'MA20', '成交量'], top: 6, right: 16,
                  textStyle: { color: '#999', fontSize: 11 } },
        grid: [
          { left: 72, right: 88, top: 34, bottom: '32%' },
          { left: 72, right: 88, top: '72%', bottom: '10%' }
        ],
        xAxis: [
          { type: 'category', data: [], gridIndex: 0,
            axisLine: { lineStyle: { color: '#444' } },
            axisLabel: { color: '#888', fontSize: 10, formatter: function (v) { return v.slice(5); } } },
          { type: 'category', data: [], gridIndex: 1,
            axisLine: { lineStyle: { color: '#444' } }, axisLabel: { show: false } }
        ],
        yAxis: [
          { scale: true, gridIndex: 0, position: 'left',
            splitLine: { lineStyle: { color: '#2A2A2A', type: 'dashed' } },
            axisLabel: { color: '#888', fontSize: 10 }, axisLine: { show: false } },
          { scale: true, gridIndex: 1, position: 'left',
            splitLine: { show: false },
            axisLabel: { color: '#888', fontSize: 10,
                         formatter: function (v) { return v >= 10000 ? (v/10000) + '万' : v; } } }
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
          { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18,
            borderColor: '#333', backgroundColor: '#222',
            fillerColor: 'rgba(239,35,42,0.15)', textStyle: { color: '#777', fontSize: 9 } }
        ],
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross', lineStyle: { color: '#888' },
                         label: { backgroundColor: '#444', fontSize: 10 } },
          backgroundColor: 'rgba(0,0,0,0.85)', borderColor: '#444',
          textStyle: { color: '#DDD', fontSize: 12 },
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
      $('st-source').textContent = '数据源: ' + j.source + (j.cached ? '（缓存）' : '');
      $('st-source').style.color = j.cached ? '#FFB020' : '#888';
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

    async function poll() {
      try {
        const j = await getJSON('/api/realtime');
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

    async function loadConfig() {
      try {
        const c = await getJSON('/api/config');
        if (c.error) return;
        state.range = c.default_range || 50;
        state.ma = c.ma || [5, 20];
        state.pollMs = (c.refresh_interval_sec || 5) * 1000;
        state.stockName = c.name;
        $('td-name').textContent = c.name;
        $('td-code').textContent = c.symbol;
        $('td-mkt').textContent = c.market.toUpperCase();
        document.querySelectorAll('.range-btn').forEach(function (b) {
          b.classList.toggle('active', +b.dataset.range === state.range);
        });
      } catch (e) { /* 保持默认 */ }
    }

    document.querySelectorAll('.range-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('.range-btn').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        state.range = +b.dataset.range;
        render();
      });
    });
    window.addEventListener('resize', function () { if (state.chart) state.chart.resize(); });

    initChart();
    loadConfig().then(loadKline).then(poll);
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
    parser = argparse.ArgumentParser(description="股票监控 v0.1（测试版）")
    parser.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL,
                        help="股票代码，如 300408 / 600519 / 600519.SH，默认三环集团")
    parser.add_argument("-p", "--port", type=int, default=config["port"], help="服务端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    code = normalize_symbol(args.symbol)
    if code is None:
        print(f"无效股票代码: {args.symbol!r}（示例: 300408）", file=sys.stderr)
        sys.exit(1)
    config["symbol"] = code
    config["market"] = market_of(code)

    ensure_echarts()
    threading.Thread(target=preload_history, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print("=" * 56)
    print(f"  股票监控 v0.1  监控标的: {config['name']} ({code})")
    print(f"  浏览器访问: {url}")
    print(f"  数据源: 东方财富 / 新浪 / 腾讯（免费接口，自动降级）")
    print("  Ctrl+C 退出")
    print("=" * 56)

    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()