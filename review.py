# -*- coding: utf-8 -*-
"""盘后回填与规则统计。

流程：
  1. 把候选池涉及的股票日 K 拉下来落 daily 表
  2. 对每个 (信号日, 代码) 算 T+1 / T+3 / T+5 收益
  3. 按策略分组出胜率表，按日期出覆盖率

用法：
  python review.py                     # 回填最近 30 天并出报告
  python review.py --days 60           # 回填最近 60 天
  python review.py --date 2026-11-05   # 只看某一天
"""
import argparse
import json
import os
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import store
import trading_calendar as tc

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def secid(code: str) -> str:
    """东财 secid：沪市 1.，深市/北交所 0.。"""
    return ("1." if code.startswith("6") else "0.") + code


def fetch_daily(code: str, beg="20260101", end="20991231"):
    """拉个股日 K（前复权），返回 store.upsert_daily 需要的行。"""
    url = (f"{KLINE}?secid={secid(code)}&klt=101&fqt=1&beg={beg}&end={end}&lmt=1000"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read()).get("data") or {}
        rows = []
        for k in data.get("klines") or []:
            p = k.split(",")
            if len(p) < 9:
                continue
            rows.append((code, p[0], float(p[1]), float(p[3]), float(p[4]),
                         float(p[2]), float(p[8]), float(p[6])))
        return rows
    except Exception:
        return []


def sync_daily(codes, beg="20260101", workers=20):
    """批量拉日 K 落库，返回成功股票数。"""
    if not codes:
        return 0
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(lambda c: fetch_daily(c, beg), codes):
            if rows:
                store.upsert_daily(rows)
                ok += 1
    return ok


def load_daily_map(codes, start):
    """{code: [(date, close, high, open), ...]} 升序。"""
    out = defaultdict(list)
    with store.conn() as c:
        for code in codes:
            for r in c.execute(
                "SELECT trade_date, close, high, open FROM daily "
                "WHERE code=? AND trade_date>=? ORDER BY trade_date",
                (code, start),
            ):
                out[code].append((r["trade_date"], r["close"], r["high"], r["open"]))
    return out


def backfill(days=30):
    """回填 T+N 收益。"""
    start = tc.recent_trading_days(days)[0]
    with store.conn() as c:
        sigs = c.execute(
            """SELECT trade_date, code, AVG(price) AS base_price
               FROM candidates WHERE trade_date>=? AND pushed=1
               GROUP BY trade_date, code""",
            (start,),
        ).fetchall()
    if not sigs:
        print(f"[回填] {start} 之后没有推送记录，跳过。先跑一段 --loop --push 积累样本。")
        return 0

    codes = sorted({s["code"] for s in sigs})
    print(f"[回填] 涉及 {len(codes)} 只股票、{len(sigs)} 条推送信号，拉日 K ...")
    n = sync_daily(codes, beg=tc.prev_trading_day(start, 10).isoformat().replace("-", ""))
    print(f"[回填] 日 K 已更新 {n}/{len(codes)} 只")

    dm = load_daily_map(codes, start)
    done = 0
    for s in sigs:
        code, td, base = s["code"], s["trade_date"], s["base_price"]
        if not base or base <= 0:
            continue
        seq = [x for x in dm.get(code, []) if x[0] >= td]
        if not seq:
            continue
        row = {"trade_date": td, "code": code, "base_price": base,
               "close_pct": None, "ret_1d": None, "ret_3d": None,
               "ret_5d": None, "max_1d": None}
        # seq[0] 是信号当日
        if len(seq) >= 1:
            row["close_pct"] = round((seq[0][1] / base - 1) * 100, 2)
        if len(seq) >= 2:
            row["ret_1d"] = round((seq[1][1] / base - 1) * 100, 2)
            row["max_1d"] = round((seq[1][2] / base - 1) * 100, 2)
        if len(seq) >= 4:
            row["ret_3d"] = round((seq[3][1] / base - 1) * 100, 2)
        if len(seq) >= 6:
            row["ret_5d"] = round((seq[5][1] / base - 1) * 100, 2)
        store.save_outcome(row)
        done += 1
    print(f"[回填] 完成 {done} 条")
    return done


def report(days=30):
    """按策略出统计表。当日剩余与次日表现分开算，避免样本不足时整表空白。"""
    start = tc.recent_trading_days(days)[0]
    with store.conn() as c:
        rows = c.execute(
            """SELECT c.primary_rule AS rule, o.ret_1d, o.ret_3d, o.close_pct, o.max_1d
               FROM outcomes o
               JOIN (SELECT DISTINCT trade_date, code, primary_rule FROM candidates
                     WHERE pushed=1) c
                 ON c.trade_date=o.trade_date AND c.code=o.code
               WHERE o.trade_date>=?""",
            (start,),
        ).fetchall()

    if not rows:
        print("\n还没有推送样本。先跑一段 `python run.py --loop --push` 再回来看。")
        return

    g = defaultdict(list)
    for r in rows:
        g[r["rule"] or "未知"].append(r)

    # ---------- 表 A：信号价 → 信号日收盘 ----------
    print(f"\n{'='*78}")
    print(f"A. 当日剩余收益（信号价 → 收盘）　{start} 起，推送 {len(rows)} 条")
    print(f"{'='*78}")
    print(f"{'策略':<14}{'样本':>6}{'均值':>10}{'胜率':>10}{'最好':>10}{'最差':>10}")
    print("-" * 78)
    for rule, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        v = [x["close_pct"] for x in rs if x["close_pct"] is not None]
        if not v:
            continue
        win = sum(1 for x in v if x > 0) / len(v) * 100
        print(f"{rule:<14}{len(v):>6}{sum(v)/len(v):>9.2f}%{win:>9.1f}%"
              f"{max(v):>9.2f}%{min(v):>9.2f}%")
    print("-" * 78)

    # ---------- 表 B：次日表现 ----------
    print(f"\n{'='*78}")
    print("B. 次日表现（信号价 → 次日收盘）")
    print(f"{'='*78}")
    print(f"{'策略':<14}{'样本':>6}{'均值':>10}{'胜率':>10}{'最大浮盈':>12}")
    print("-" * 78)
    shown = 0
    for rule, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        r1 = [x["ret_1d"] for x in rs if x["ret_1d"] is not None]
        if not r1:
            continue
        mx = [x["max_1d"] for x in rs if x["max_1d"] is not None]
        win = sum(1 for v in r1 if v > 0) / len(r1) * 100
        print(f"{rule:<14}{len(r1):>6}{sum(r1)/len(r1):>9.2f}%{win:>9.1f}%"
              f"{(sum(mx)/len(mx) if mx else 0):>11.2f}%")
        shown += 1
    if shown == 0:
        print("  （还没有次日数据。今天推的信号要等下一个交易日收盘后回填。）")
    print("-" * 78)

    # ---------- 覆盖率 ----------
    print(f"\n{'='*78}")
    print("C. 覆盖率（当日涨幅 3~9% 的可介入标的 Top20 中被候选池捕获的比例）")
    print("   注：不用「全市场涨幅 Top20」——那些基本是涨停股，策略主动排除，算出来恒为 0。")
    print(f"{'='*78}")
    with store.conn() as c:
        dates = c.execute(
            """SELECT trade_date, COUNT(*) AS n FROM daily
               WHERE trade_date>=? GROUP BY trade_date HAVING n>=500
               ORDER BY trade_date""",
            (start,),
        ).fetchall()
    if not dates:
        print("  没有全市场日 K 数据。收盘后跑 `python review.py --sync-all` 存一份，")
        print("  之后每天的覆盖率就能算了。")
        return
    for row in dates[-10:]:
        d = row["trade_date"]
        with store.conn() as c:
            top = [r[0] for r in c.execute(
                "SELECT code FROM daily WHERE trade_date=? AND amount>0 "
                "AND pct BETWEEN 3 AND 9 ORDER BY pct DESC LIMIT 20", (d,))]
            cand = {r[0] for r in c.execute(
                "SELECT DISTINCT code FROM candidates WHERE trade_date=?", (d,))}
        if not top:
            print(f"  {d}  当日无涨幅 3~9% 的标的（单边行情）")
            continue
        hit = sum(1 for t in top if t in cand)
        bar = "█" * hit + "·" * (len(top) - hit)
        print(f"  {d}  {hit:>2}/{len(top):<2} = {hit/len(top)*100:>5.1f}%  {bar}")


def sync_all_today():
    """把当日全市场收盘快照写进 daily，用于算覆盖率。收盘后跑。"""
    import feed
    snaps = feed.fetch_all()
    rows = [(s.code, __import__("datetime").date.today().isoformat(),
             s.open, s.high, s.low, s.price, s.pct, s.amount)
            for s in snaps.values()]
    store.upsert_daily(rows)
    print(f"[快照] 已存 {len(rows)} 只全市场收盘数据")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--date", default=None)
    ap.add_argument("--sync-all", action="store_true", help="存当日全市场收盘快照")
    args = ap.parse_args()

    store.init()
    if args.sync_all:
        sync_all_today()
    backfill(args.days)
    report(args.days)
    print("\n库概况：", store.stats_overview())
