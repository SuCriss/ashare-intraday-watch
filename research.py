# -*- coding: utf-8 -*-
"""严谨的策略验证框架。

为什么需要这个：
  之前 backtest.py 用 5 分钟 K 线，只有 32 个交易日、457 个信号，
  而且止损/止盈参数就是在这批数据上选的——典型样本内过拟合。

本框架的做法（三层验证）：
  第 1 层  日线代理 · 6 年 · 大样本 → 验证「策略逻辑」是否真的存在 alpha
  第 2 层  样本外   · 时间分割   → 验证「不是拟合出来的」
  第 3 层  稳健性   · 分年/分市值/参数扰动 → 验证「不是靠某个特殊时期」

口径：
  · 信号日收盘后产生信号，次日开盘买入（真实可成交），持有 N 日收盘卖出
  · 基准 = 同日全市场等权次日收益，超额 = 信号收益 − 当日基准
  · 扣除双边交易成本 0.15%
  · 剔除次日开盘涨幅 > 9%（一字板买不进）

已知偏差（必须声明）：
  · 幸存者偏差：样本是当前仍存活的股票，退市股未纳入，会高估收益
  · 日线代理与盘中实时信号不完全等价（盘中更早、滑点更小，但也更容易假突破）

用法：
  python research.py --fetch            # 拉数据（首次约 2-5 分钟）
  python research.py                    # 跑验证
  python research.py --stocks 800       # 扩大样本
"""
import argparse
import json
import math
import os
import pickle
import random
import statistics
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import config
import feed

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}
KLINE = f"https://{config.EM_HIS_HOST}/api/qt/stock/kline/get"
FFLOW = f"https://{config.EM_HIS_HOST}/api/qt/stock/fflow/daykline/get"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_DAILY = os.path.join(DATA_DIR, "research_daily.pkl")
CACHE_FFLOW = os.path.join(DATA_DIR, "research_fflow.pkl")

COST = 0.15          # 双边交易成本 %（佣金+印花税+滑点，保守）
TRAIN_END = "2023-12-31"   # 样本内截止；之后为样本外


# ============================================================
# 数据层
# ============================================================
def secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_daily(code: str, beg="20200101"):
    """日 K（前复权）。返回 [(date, open, high, low, close, vol, amount, pct, turnover)]"""
    url = (f"{KLINE}?secid={secid(code)}&klt=101&fqt=1&beg={beg}&end=20500101&lmt=10000"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    for attempt in range(3):
        try:
            d = _get(url).get("data") or {}
            out = []
            for k in d.get("klines") or []:
                p = k.split(",")
                if len(p) < 11:
                    continue
                out.append((p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                            float(p[5]), float(p[6]), float(p[8]), float(p[10])))
            return out
        except Exception:
            if attempt == 2:
                return []
    return []


def fetch_fflow(code: str):
    """日级资金流。返回 [(date, main_net, main_pct)]"""
    url = (f"{FFLOW}?secid={secid(code)}&lmt=0&klt=101&fields1=f1,f2,f3,f7"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65")
    for attempt in range(3):
        try:
            d = _get(url).get("data") or {}
            out = []
            for k in d.get("klines") or []:
                p = k.split(",")
                if len(p) < 7:
                    continue
                out.append((p[0], float(p[1]), float(p[6])))
            return out
        except Exception:
            if attempt == 2:
                return []
    return []


def pick_universe(n=500):
    """分层抽样：按当前成交额分 10 层，每层均取，覆盖大中小盘，减少选择偏差。"""
    snaps = feed.fetch_all(quiet=True, top_pages=60, timeout=30)
    ranked = sorted(snaps.values(), key=lambda s: -s.amount)
    if not ranked:
        return []
    layers = 10
    per = max(1, n // layers)
    out = []
    size = len(ranked) // layers
    for i in range(layers):
        seg = ranked[i * size:(i + 1) * size] if i < layers - 1 else ranked[i * size:]
        if len(seg) > per:
            seg = random.Random(42 + i).sample(seg, per)
        out.extend(s.code for s in seg)
    return out


def build_cache(n_stocks=500, force=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE_DAILY) and not force:
        with open(CACHE_DAILY, "rb") as f:
            d = pickle.load(f)
        print(f"[缓存] 命中 {CACHE_DAILY}：{len(d['daily'])} 只")
        return d

    codes = pick_universe(n_stocks)
    print(f"[拉取] 分层抽样 {len(codes)} 只，日 K + 资金流 ...")
    t0 = time.perf_counter()
    daily, fflow = {}, {}

    def work(code):
        return code, fetch_daily(code), fetch_fflow(code)

    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for code, dl, ff in ex.map(work, codes):
            if len(dl) > 200:
                daily[code] = dl
            if ff:
                fflow[code] = ff
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(codes)}  用时 {time.perf_counter()-t0:.0f}s")

    d = {"daily": daily, "fflow": fflow, "built": time.strftime("%Y-%m-%d %H:%M")}
    with open(CACHE_DAILY, "wb") as f:
        pickle.dump(d, f)
    print(f"[缓存] 已存 {CACHE_DAILY}：日K {len(daily)} 只 / 资金流 {len(fflow)} 只")
    return d


# ============================================================
# 特征层：把日 K 转成可算信号的数组
# ============================================================
def prep(code, rows):
    """返回 dict of numpy-free lists：dates, open, high, low, close, vol, pct, turnover"""
    dates = [r[0] for r in rows]
    return {
        "dates": dates,
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "vol": [r[5] for r in rows],
        "amount": [r[6] for r in rows],
        "pct": [r[7] for r in rows],
        "turnover": [r[8] for r in rows],
    }


def vol_ratio(vol, i, win=5):
    """量比 = 当日量 / 前 win 日均量"""
    if i < win:
        return None
    avg = sum(vol[i - win:i]) / win
    return vol[i] / avg if avg > 0 else None


# ============================================================
# 策略定义：返回 True/False（在第 i 天是否产生信号）
# ============================================================
def sig_breakout(f, i):
    """放量突破：涨幅 3~8%、量比≥2.5、收盘贴当日最高、换手 3~15%"""
    pct = f["pct"][i]
    if not (3.0 <= pct <= 8.0):
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < 2.5:
        return False
    if not (3.0 <= f["turnover"][i] <= 15.0):
        return False
    rng = f["high"][i] - f["low"][i]
    if rng <= 0:
        return False
    if (f["close"][i] - f["low"][i]) / rng < 0.85:      # 收盘在当日振幅 85% 以上
        return False
    return True


def sig_pullback(f, i):
    """回踩反包：日内最低跌破 -2%，收盘回到 +2% 以上，量比≥1.5"""
    if i < 1:
        return False
    pre = f["close"][i - 1]
    if pre <= 0:
        return False
    dip = (f["low"][i] / pre - 1) * 100
    if dip > -2.0:
        return False
    if f["pct"][i] < 2.0:
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < 1.5:
        return False
    rng = f["high"][i] - f["low"][i]
    if rng <= 0 or (f["close"][i] - f["low"][i]) / rng < 0.75:
        return False
    return True


def sig_capital(f, i, fflow_map, code):
    """主力抢筹：主力净流入占比 ≥15%，涨幅 0~7%"""
    ff = fflow_map.get(code)
    if not ff:
        return False
    d = f["dates"][i]
    rec = ff.get(d)
    if rec is None:
        return False
    main_net, main_pct = rec
    if main_pct < 15.0:
        return False
    if not (0.0 <= f["pct"][i] <= 7.0):
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < 1.5:
        return False
    return True


# ---------- 候选新信号（用同一套框架检验） ----------
def sig_shrink_pullback(f, i):
    """缩量回踩：跌 1~3% 且量比 < 0.8（没人抛，洗盘特征）"""
    if not (-3.0 <= f["pct"][i] <= -1.0):
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr > 0.8:
        return False
    return True


def sig_low_open_up(f, i):
    """低开高走：开盘跌 < -1%，收盘涨 > 1%，放量"""
    if i < 1:
        return False
    pre = f["close"][i - 1]
    if pre <= 0:
        return False
    if (f["open"][i] / pre - 1) * 100 > -1.0:
        return False
    if f["pct"][i] < 1.0:
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < 1.5:
        return False
    return True


def sig_close_at_high(f, i):
    """收盘即最高：涨幅 2~6%，量比 1.5~3（有量但不过热）"""
    rng = f["high"][i] - f["low"][i]
    if rng <= 0:
        return False
    if (f["high"][i] - f["close"][i]) / rng > 0.02:
        return False
    if not (2.0 <= f["pct"][i] <= 6.0):
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or not (1.5 <= vr <= 3.0):
        return False
    return True


def sig_consolidation(f, i):
    """强势整理：前 3 日累涨 > 8%，当日缩量微涨 0~3%（洗盘不破）"""
    if i < 4:
        return False
    base = f["close"][i - 4]
    if base <= 0:
        return False
    cum = (f["close"][i - 1] / base - 1) * 100
    if cum < 8.0:
        return False
    if not (0.0 <= f["pct"][i] <= 3.0):
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr > 1.2:
        return False
    return True


def sig_gap_up_hold(f, i):
    """跳空高开不回落：开盘涨 > 1%，收盘仍涨 > 1%，最低不破昨收"""
    if i < 1:
        return False
    pre = f["close"][i - 1]
    if pre <= 0:
        return False
    if (f["open"][i] / pre - 1) * 100 < 1.0:
        return False
    if f["pct"][i] < 1.0:
        return False
    if f["low"][i] < pre:
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < 1.2:
        return False
    return True


# ============================================================
# 回测引擎
# ============================================================
def run_signals(data, sig_fn, horizons=(1, 3, 5), entry="next_open"):
    """对每个信号，记录持有 N 日的收益。

    entry="next_open"  次日开盘买入（默认，日线口径）
    entry="same_close" 当日收盘买入（≈盘中 14:50 尾盘买入，实盘口径）

    返回 [(date, code, {h: ret})]，已扣交易成本。
    """
    daily = data["daily"]
    fflow_raw = data.get("fflow", {})
    fflow_map = {}
    for code, rows in fflow_raw.items():
        fflow_map[code] = {r[0]: (r[1], r[2]) for r in rows}

    out = []
    for code, rows in daily.items():
        f = prep(code, rows)
        n = len(rows)
        for i in range(n - 1):
            try:
                hit = sig_fn(f, i, fflow_map, code) if sig_fn is sig_capital else sig_fn(f, i)
            except Exception:
                continue
            if not hit:
                continue

            if entry == "next_open":
                px = f["open"][i + 1]
                # 一字板买不进：次日开盘涨幅 > 9%
                if f["close"][i] > 0 and (px / f["close"][i] - 1) * 100 > 9.0:
                    continue
            else:
                px = f["close"][i]
            if px <= 0:
                continue

            # 两个口径的卖出点都以「信号日之后第 h 个交易日收盘」为准，
            # 差别只在买入价：次日开盘 vs 当日收盘
            rets = {}
            for h in horizons:
                j = i + h
                if j >= n:
                    continue
                c = f["close"][j]
                if c <= 0:
                    continue
                rets[h] = (c / px - 1) * 100 - COST
            if rets:
                out.append((f["dates"][i], code, rets))
    return out


def market_benchmark(data, horizons=(1, 3, 5)):
    """每个交易日的全样本等权收益：{date: {h: mean_ret}}"""
    daily = data["daily"]
    acc = defaultdict(lambda: defaultdict(list))
    for code, rows in daily.items():
        f = prep(code, rows)
        n = len(rows)
        for i in range(n - 1):
            o = f["open"][i + 1]
            if o <= 0:
                continue
            for h in horizons:
                j = i + h
                if j >= n or f["close"][j] <= 0:
                    continue
                acc[f["dates"][i]][h].append((f["close"][j] / o - 1) * 100 - COST)
    return {d: {h: statistics.mean(v) for h, v in hs.items() if v}
            for d, hs in acc.items()}


def evaluate(signals, bench, horizon=1, label=""):
    """超额收益 + 统计显著性。"""
    rows = []
    for date, code, rets in signals:
        if horizon not in rets:
            continue
        b = bench.get(date, {}).get(horizon)
        if b is None:
            continue
        rows.append((date, rets[horizon], rets[horizon] - b))

    if not rows:
        return None

    rets = [r[1] for r in rows]
    excess = [r[2] for r in rows]
    n = len(excess)
    mean_ex = statistics.mean(excess)
    sd = statistics.pstdev(excess) if n > 1 else 0
    t = mean_ex / (sd / math.sqrt(n)) if sd > 0 and n > 1 else 0
    win = sum(1 for x in excess if x > 0) / n * 100

    # bootstrap 95% 置信区间
    rng = random.Random(7)
    boot = []
    for _ in range(2000):
        s = [excess[rng.randrange(n)] for _ in range(n)]
        boot.append(statistics.mean(s))
    boot.sort()
    m = len(boot)
    ci_lo = boot[max(0, int(m * 0.025))]
    ci_hi = boot[min(m - 1, int(m * 0.975) - 1)]

    return {
        "label": label, "n": n,
        "mean_ret": statistics.mean(rets),
        "mean_ex": mean_ex, "t": t, "win": win,
        "ci": (ci_lo, ci_hi),
        # 样本少于 30 个不给结论
        "significant": n >= 30 and t >= 2.0 and ci_lo > 0,
        "rows": rows,
    }


def by_year(rows, min_n=20):
    g = defaultdict(list)
    for date, _ret, ex in rows:
        g[date[:4]].append(ex)
    return {y: (len(v), statistics.mean(v)) for y, v in sorted(g.items()) if len(v) >= min_n}


def sig_pullback_param(f, i, dip_th=-2.0, rec_th=2.0, vr_th=1.5):
    """参数化的回踩反包，用于稳健性扫描。"""
    if i < 1:
        return False
    pre = f["close"][i - 1]
    if pre <= 0:
        return False
    if (f["low"][i] / pre - 1) * 100 > dip_th:
        return False
    if f["pct"][i] < rec_th:
        return False
    vr = vol_ratio(f["vol"], i)
    if vr is None or vr < vr_th:
        return False
    rng = f["high"][i] - f["low"][i]
    if rng <= 0 or (f["close"][i] - f["low"][i]) / rng < 0.75:
        return False
    return True


def robust_pullback(data, bench):
    """参数稳健性：如果只有某一个参数组合显著，那就是过拟合。

    真正有效的信号，应该在一片参数区域里都显著。
    """
    dips = (-1.5, -2.0, -2.5, -3.0)
    recs = (1.5, 2.0, 2.5, 3.0)
    vrs = (1.2, 1.5, 2.0)

    print(f"\n{'='*104}")
    print("第 3 层 · 参数稳健性（样本外 2024-2026，T+1 超额收益 / t 值）")
    print(f"{'='*104}")
    print(pad("回踩\\反包", 12) + "".join(pad(f"≥{r}", 25, ">") for r in recs))
    print("-" * 104)

    cells = {}
    for dip in dips:
        line = pad(f"≤{dip}%", 12)
        for rec in recs:
            # 每个 (dip, rec) 取 vr 三档里最保守的
            vals = []
            for vr in vrs:
                sigs = []
                for code, rows in data["daily"].items():
                    f = prep(code, rows)
                    n = len(rows)
                    for i in range(n - 1):
                        if not sig_pullback_param(f, i, dip, rec, vr):
                            continue
                        o = f["open"][i + 1]
                        if o <= 0:
                            continue
                        if f["close"][i] > 0 and (o / f["close"][i] - 1) * 100 > 9.0:
                            continue
                        j = i + 1
                        if j >= n or f["close"][j] <= 0:
                            continue
                        rets = {1: (f["close"][j] / o - 1) * 100 - COST}
                        sigs.append((f["dates"][i], code, rets))
                sigs = [s for s in sigs if s[0] > TRAIN_END]
                r = evaluate(sigs, bench, 1)
                if r:
                    vals.append(r)
            if not vals:
                line += pad("-", 25, ">")
                continue
            worst = min(vals, key=lambda x: x["mean_ex"])
            cells[(dip, rec)] = worst
            mark = "✅" if worst["significant"] else ("·" if worst["mean_ex"] > 0 else "×")
            line += pad(f"{worst['mean_ex']:+.2f}%/t{worst['t']:+.1f}{mark}", 25, ">")
        print(line)
    print("-" * 104)

    ok = [k for k, v in cells.items() if v["significant"]]
    total = len(cells)
    print(f"\n  显著组合 {len(ok)}/{total}  →  "
          f"{'✅ 稳健，不是单点拟合' if len(ok) >= total * 0.6 else '⚠ 稳健性不足，有过拟合风险'}")
    if ok:
        print(f"  显著区间示例：{ok[:6]}")
    print("  判定：取三档量比中最差的一个，仍显著才算通过（保守口径）")


# ============================================================
# 报告
# ============================================================
def pad(s, n, align="<"):
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))
    d = n - w
    if d <= 0:
        return str(s)
    return str(s) + " " * d if align == "<" else " " * d + str(s)


def report(res, title):
    if not res:
        print(f"  {title}: 无样本")
        return
    star = "✅" if res["significant"] else "❌"
    print(f"  {star} {pad(title, 16)} n={res['n']:<6} "
          f"收益 {res['mean_ret']:+.2f}%  超额 {res['mean_ex']:+.2f}%  "
          f"t={res['t']:+.2f}  胜率 {res['win']:.1f}%  "
          f"95%CI [{res['ci'][0]:+.2f}, {res['ci'][1]:+.2f}]")
    yrs = by_year(res["rows"])
    if yrs:
        parts = [f"{y}:{m:+.2f}%({n})" for y, (n, m) in yrs.items()]
        print(f"    分年：{'  '.join(parts)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="强制重拉数据")
    ap.add_argument("--stocks", type=int, default=500)
    ap.add_argument("--robust", action="store_true", help="只跑回踩反包的参数稳健性扫描")
    ap.add_argument("--search", action="store_true", help="搜索候选新信号")
    args = ap.parse_args()

    data = build_cache(args.stocks, force=args.fetch)
    if not data["daily"]:
        print("没有数据。")
        return

    if args.robust:
        dates = sorted({r[0] for rows in data["daily"].values() for r in rows})
        print(f"\n样本：{len(data['daily'])} 只 / {dates[0]} → {dates[-1]}")
        print("计算基准 ...")
        bench = market_benchmark(data)
        robust_pullback(data, bench)
        return

    if args.search:
        dates = sorted({r[0] for rows in data["daily"].values() for r in rows})
        print(f"\n样本：{len(data['daily'])} 只 / {dates[0]} → {dates[-1]}")
        print("计算基准 ...")
        bench = market_benchmark(data)
        print(f"\n{'='*104}")
        print("候选信号搜索（全样本 + 样本外双门槛，T+1）")
        print(f"{'='*104}")
        cands = [
            ("缩量回踩", sig_shrink_pullback),
            ("低开高走", sig_low_open_up),
            ("收盘即最高", sig_close_at_high),
            ("强势整理", sig_consolidation),
            ("跳空不回落", sig_gap_up_hold),
            ("回踩反包(基线)", sig_pullback),
        ]
        print("\n【A】次日开盘买入（日线口径）")
        passed = []
        for name, fn in cands:
            sigs = run_signals(data, fn, entry="next_open")
            full = evaluate(sigs, bench, 1)
            oos = evaluate([s for s in sigs if s[0] > TRAIN_END], bench, 1)
            if not full or not oos:
                print(f"  ·  {pad(name,14)} 样本不足")
                continue
            ok = full["significant"] and oos["significant"]
            mark = "✅" if ok else "❌"
            print(f"  {mark} {pad(name,14)} "
                  f"全样本 n={full['n']:<5} 超额 {full['mean_ex']:+.2f}% t={full['t']:+.2f} | "
                  f"样本外 n={oos['n']:<5} 超额 {oos['mean_ex']:+.2f}% t={oos['t']:+.2f}")
            if ok:
                passed.append(name)
        print(f"\n  通过双门槛：{', '.join(passed) if passed else '无'}")

        # 关键：实盘是「今天尾盘买」，不是「明天开盘买」
        print("\n【B】当日收盘买入（≈14:50 尾盘买入，实盘口径）")
        passed_b = []
        for name, fn in cands:
            sigs = run_signals(data, fn, entry="same_close")
            full = evaluate(sigs, bench, 1)
            oos = evaluate([s for s in sigs if s[0] > TRAIN_END], bench, 1)
            if not full or not oos:
                continue
            ok = full["significant"] and oos["significant"]
            mark = "✅" if ok else "❌"
            print(f"  {mark} {pad(name,14)} "
                  f"全样本 n={full['n']:<5} 超额 {full['mean_ex']:+.2f}% t={full['t']:+.2f} | "
                  f"样本外 n={oos['n']:<5} 超额 {oos['mean_ex']:+.2f}% t={oos['t']:+.2f}")
            if ok:
                passed_b.append(name)
        print(f"\n  通过双门槛：{', '.join(passed_b) if passed_b else '无'}")
        print("\n  门槛：全样本 t≥2 且样本外 t≥2 且 CI 下界>0")
        return

    dates = sorted({r[0] for rows in data["daily"].values() for r in rows})
    print(f"\n样本：{len(data['daily'])} 只 / {dates[0]} → {dates[-1]} / {len(dates)} 个交易日")
    print(f"交易成本：双边 {COST}%　样本内截止：{TRAIN_END}")

    print("\n计算基准 ...")
    bench = market_benchmark(data)

    print(f"\n{'='*104}")
    print("第 1 层 · 全样本（2020-2026）")
    print(f"{'='*104}")
    results = {}
    for name, fn in (("放量突破", sig_breakout), ("回踩反包", sig_pullback),
                     ("主力抢筹", sig_capital)):
        sigs = run_signals(data, fn)
        for h in (1, 3, 5):
            r = evaluate(sigs, bench, h, f"{name} T+{h}")
            if r:
                results[f"{name}_T{h}"] = r
                report(r, f"{name} T+{h}")

    print(f"\n{'='*104}")
    print("第 2 层 · 样本外（2024-2026）")
    print(f"{'='*104}")
    for name, fn in (("放量突破", sig_breakout), ("回踩反包", sig_pullback),
                     ("主力抢筹", sig_capital)):
        sigs = [s for s in run_signals(data, fn) if s[0] > TRAIN_END]
        r = evaluate(sigs, bench, 1, f"{name} T+1")
        if r:
            report(r, f"{name} 样本外 T+1")

    print(f"\n{'='*104}")
    print("第 3 层 · 结论")
    print(f"{'='*104}")
    passed = [k for k, v in results.items() if v["significant"]]
    if passed:
        print(f"  通过显著性检验（t≥2 且 CI 下界>0）：{', '.join(passed)}")
    else:
        print("  ❌ 没有任何策略通过显著性检验——现有策略逻辑不成立，需要重做")
    print("\n  判定标准：t ≥ 2 且 bootstrap 95% CI 下界 > 0（即超额收益显著大于 0）")
    print("  已知偏差：幸存者偏差（退市股未纳入）会高估收益，实际表现更差")


if __name__ == "__main__":
    main()
