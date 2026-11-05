# -*- coding: utf-8 -*-
"""日线历史缓存：给盘中策略提供「昨日涨跌停 / 20日均线 / 20日高点 / 前3日走势」。

盘中快照只有当日数据，但经过验证的信号（涨停次日、站上MA20、平台突破、
连跌首阳、三连阳）都需要历史。这里把最近 N 个交易日的日 K 缓到内存，
每轮扫描直接查，不发请求。

数据来源：store.daily 表。收盘后 run.py 会自动写入当日全市场快照，
首次部署用 `python history.py --backfill` 补齐历史。

用法：
  python history.py --backfill    # 回填最近 40 个交易日（约 2 分钟）
  python history.py --stats       # 看缓存覆盖情况
"""
import json
import os
import statistics
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import config
import store
import trading_calendar as tc

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}
KLINE = f"https://{config.EM_HIS_HOST}/api/qt/stock/kline/get"


def secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def fetch_daily(code: str, beg: str):
    url = (f"{KLINE}?secid={secid(code)}&klt=101&fqt=1&beg={beg}&end=20500101&lmt=200"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read()).get("data") or {}
            rows = []
            for k in d.get("klines") or []:
                p = k.split(",")
                if len(p) < 11:
                    continue
                rows.append((code, p[0], float(p[1]), float(p[3]), float(p[4]),
                             float(p[2]), float(p[8]), float(p[6])))
            return rows
        except Exception:
            if attempt == 1:
                return []
    return []


def backfill(days=40, workers=16):
    """把最近 days 个交易日的全市场日 K 写进 store.daily。"""
    store.init()
    snaps = None
    try:
        import feed
        snaps = feed.fetch_all(quiet=True, top_pages=60, timeout=30)
    except Exception as e:
        print(f"[回填] 取股票池失败：{e}")
    if not snaps:
        print("[回填] 没有股票池，先确保能拉到快照")
        return

    codes = list(snaps.keys())
    beg = tc.recent_trading_days(days)[0].replace("-", "")
    print(f"[回填] {len(codes)} 只 × 最近 {days} 个交易日（{beg} 起），并发 {workers} ...")

    import time
    t0 = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(lambda c: fetch_daily(c, beg), codes):
            if rows:
                store.upsert_daily(rows)
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(codes)}  {time.perf_counter()-t0:.0f}s")
    print(f"[回填] 完成 {done} 只，用时 {time.perf_counter()-t0:.0f}s")


class DailyHistory:
    """内存日线缓存。每轮扫描前 refresh 一次即可（读库，毫秒级）。"""

    def __init__(self, days=40):
        self.days = days
        self.data: dict[str, dict] = {}
        self.updated = None

    def refresh(self):
        since = tc.recent_trading_days(self.days)[0]
        rows = defaultdict(list)
        try:
            with store.conn() as c:
                for r in c.execute(
                    "SELECT code, trade_date, open, high, low, close, pct, amount "
                    "FROM daily WHERE trade_date>=? ORDER BY code, trade_date",
                    (since,),
                ):
                    rows[r["code"]].append(
                        (r["trade_date"], r["open"], r["high"], r["low"],
                         r["close"], r["pct"]))
        except Exception as e:
            print(f"[历史] 读取失败：{e}")
            return

        self.data = dict(rows)
        self.updated = len(self.data)
        return self.updated

    # ---------- 策略用到的派生量 ----------
    def bars(self, code, n=None):
        """返回 [(date, open, high, low, close, pct)]，d[-1] 是最近一个已收盘交易日。"""
        d = self.data.get(code)
        if not d:
            return None
        return d[-n:] if n else d

    def ma(self, code, n=20):
        """最近 n 个已收盘交易日的均线（不含今天）。"""
        d = self.data.get(code)
        if not d or len(d) < n:
            return None
        return sum(x[4] for x in d[-n:]) / n

    def high_n(self, code, n=20):
        """最近 n 个已收盘交易日的最高价。"""
        d = self.data.get(code)
        if not d or len(d) < n:
            return None
        return max(x[2] for x in d[-n:])

    def prev_pct(self, code):
        """昨日涨跌幅。"""
        d = self.data.get(code)
        return d[-1][5] if d else None

    def stats(self):
        if not self.data:
            return "缓存为空"
        lens = [len(v) for v in self.data.values()]
        return (f"{len(self.data)} 只 / 平均 {statistics.mean(lens):.1f} 天 / "
                f"最少 {min(lens)} 天 / 最多 {max(lens)} 天")


if __name__ == "__main__":
    import sys
    if "--backfill" in sys.argv:
        backfill()
    store.init()
    h = DailyHistory()
    h.refresh()
    print("缓存：", h.stats())
