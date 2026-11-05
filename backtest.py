# -*- coding: utf-8 -*-
"""用 5 分钟 K 线回测价量策略 + 止损参数扫描。

思路：把 5 分钟 bar 当成「每 5 分钟一次盘中扫描」，逐日重放。
能复现：放量突破 / 急拉启动 / 回踩反包（纯价量）。
不能复现：主力抢筹（历史分钟资金流接口不通，跳过）。

用法：
  python backtest.py                          # 基础回测：200 只 / 30 日
  python backtest.py --optimize-stop          # 止损参数扫描（本次交付重点）
  python backtest.py --stocks 400 --days 40 --optimize-stop
"""
import argparse
import json
import statistics
import unicodedata
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import config
import feed
import trading_calendar as tc

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}
KLINE = f"https://{config.EM_HIS_HOST}/api/qt/stock/kline/get"

# 止损档位：None = 不止损
STOP_LEVELS = (-1.0, -1.5, -2.0, -2.5, -3.0, -4.0, -5.0, -7.0, None)


# ---------------- 显示宽度对齐（中文占两格） ----------------
def _w(s) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def pad(s, n, align="<") -> str:
    s = str(s)
    d = n - _w(s)
    if d <= 0:
        return s
    return s + " " * d if align == "<" else " " * d + s


# ---------------- 数据 ----------------
def secid(code: str) -> str:
    """东财 secid：沪市 1.，深市/北交所 0.。"""
    return ("1." if code.startswith("6") else "0.") + code


def fetch_5min(code: str, lmt=2000):
    """返回 {date: [(time, open, close, high, low, vol, amount, pct, turnover), ...]}
    海外服务器上 push2his 会间歇 unreachable，重试 3 次。"""
    url = (f"{KLINE}?secid={secid(code)}&klt=5&fqt=1&beg=0&end=20500101&lmt={lmt}"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
    data = {}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read()).get("data") or {}
            break
        except Exception:
            if attempt == 2:
                return {}
            continue

    by_day = defaultdict(list)
    for k in data.get("klines") or []:
        p = k.split(",")
        if len(p) < 11:
            continue
        by_day[p[0][:10]].append((
            p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]),
            float(p[5]), float(p[6]), float(p[8]), float(p[10]),
        ))
    return dict(by_day)


def cum_vol(bars):
    out, s = [], 0.0
    for b in bars:
        s += b[5]
        out.append(s)
    return out


# ---------------- 重放 ----------------
def scan_one(code: str, by_day: dict):
    """重放一只股票，返回信号列表（带信号之后的 bar 路径，供止损模拟用）。"""
    dates = sorted(by_day)
    if len(dates) < 6:
        return []

    cum = {d: cum_vol(by_day[d]) for d in dates}
    out = []

    for j in range(5, len(dates)):
        d = dates[j]
        bars = by_day[d]
        if len(bars) < 30:              # 半天停牌，跳过
            continue

        prev = [cum[dates[k]] for k in range(j - 5, j)]
        cv = cum[d]
        day_high = 0.0
        fired: dict[str, int] = {}      # rule -> 命中 bar 下标

        for i, b in enumerate(bars):
            _, o, c, h, l, v, amt, pct, turnover = b
            day_high = max(day_high, h)

            base = [p[i] for p in prev if len(p) > i]
            if not base:
                continue
            avg = sum(base) / len(base)
            vol_ratio = cv[i] / avg if avg > 0 else 0

            if ("breakout" not in fired and 3.0 <= pct <= 8.0
                    and vol_ratio >= 2.5 and 3.0 <= turnover <= 15.0
                    and day_high > 0 and c >= day_high * 0.995):
                fired["breakout"] = i

            if "surge" not in fired and i >= 3 and pct <= 9.0 and vol_ratio >= 2.0:
                if (c / bars[i - 3][2] - 1) * 100 >= 2.0:
                    fired["surge"] = i

            if "pullback" not in fired and i > 0 and pct > -100:
                pre = c / (1 + pct / 100)
                if pre > 0:
                    dip = (min(x[4] for x in bars[: i + 1]) / pre - 1) * 100
                    if dip <= -2.0 and pct >= 2.0 and vol_ratio >= 1.5:
                        fired["pullback"] = i

        if not fired:
            continue

        nxt = by_day.get(dates[j + 1]) if j + 1 < len(dates) else None
        for rule, i in fired.items():
            out.append({
                "code": code,
                "date": d,
                "rule": rule,
                "price": bars[i][2],                                    # 信号价
                "post": [(x[3], x[4], x[2]) for x in bars[i + 1:]],     # 当日剩余 (high, low, close)
                "nxt": [(x[3], x[4], x[2]) for x in nxt] if nxt else [],  # 次日 (high, low, close)
                "day_close": bars[-1][2],
                "next_open": nxt[0][1] if nxt else None,
                "next_close": nxt[-1][2] if nxt else None,
            })
    return out


# ---------------- 止损模拟 ----------------
def simulate(sig: dict, stop_pct, horizon="next", entry="signal"):
    """按给定止损档位走一遍。

    stop_pct  None = 不止损
    horizon   day = 当日收盘平仓 / next = 次日收盘平仓
    entry     signal = 按信号价买 / next_open = 次日开盘买

    返回 (收益率%, 是否触发止损) 或 None（数据不足）
    """
    if entry == "signal":
        e = sig["price"]
        path = sig["post"] if horizon == "day" else sig["post"] + sig["nxt"]
    else:                                   # 次日开盘买
        if not sig["next_open"]:
            return None
        e = sig["next_open"]
        path = sig["nxt"]

    if e <= 0:
        return None

    final = sig["day_close"] if horizon == "day" else sig["next_close"]
    if final is None:
        return None

    if stop_pct is not None:
        sp = e * (1 + stop_pct / 100)
        for _high, low, _close in path:
            if low <= sp:
                # 假设按止损价成交（乐观：忽略跳空与跌停封死）
                return (sp / e - 1) * 100, True
    return (final / e - 1) * 100, False


# ---------------- 止盈 / 移动止盈模拟 ----------------
def simulate_tp(sig: dict, stop_pct, tp_pct, dd_pct, horizon="next"):
    """止损 + 止盈 + 移动止盈 三件套走一遍。

    stop_pct  硬止损（相对信号价）
    tp_pct    止盈激活线：涨到 entry*(1+tp) 后进入移动止盈
    dd_pct    移动止盈回撤：从峰值跌 dd% 离场
    None      表示该环节关闭

    返回 (收益率%, 离场原因)  原因 ∈ stop / trail / close
    同一根 bar 内既触止损又触止盈时按最坏情况算（先止损）。
    """
    e = sig["price"]
    path = sig["post"] if horizon == "day" else sig["post"] + sig["nxt"]
    final = sig["day_close"] if horizon == "day" else sig["next_close"]
    if e <= 0 or final is None:
        return None

    stop_price = e * (1 + stop_pct / 100) if stop_pct is not None else None
    tp_price = e * (1 + tp_pct / 100) if tp_pct is not None else None
    peak = e
    activated = tp_pct is None        # 没设止盈 = 一开始就按回撤管理

    for high, low, _close in path:
        # 1) 止损优先（保守假设）
        if stop_price is not None and low <= stop_price:
            return (stop_price / e - 1) * 100, "stop"
        # 2) 更新峰值
        if high > peak:
            peak = high
        # 3) 止盈激活
        if not activated and high >= tp_price:
            activated = True
        # 4) 移动止盈
        if activated and dd_pct is not None:
            trail = peak * (1 - dd_pct / 100)
            if low <= trail:
                return (trail / e - 1) * 100, "trail"

    return (final / e - 1) * 100, "close"


def describe(rets):
    """一组收益率 → 统计描述。"""
    if not rets:
        return None
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    srt = sorted(rets)
    return {
        "n": n,
        "mean": statistics.mean(rets),
        "win": len(wins) / n * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pl_ratio": abs(avg_win / avg_loss) if avg_loss else float("inf"),
        "worst": srt[0],
        "p05": srt[max(0, int(n * 0.05) - 1)],
    }


# ---------------- 基础回测 ----------------
def run(n_stocks=200, days=30):
    signals, bench, n_used = _collect(n_stocks, days)

    print(f"\n{'='*94}")
    print(f"5 分钟 K 线回测（{tc.recent_trading_days(days)[0]} 起，"
          f"{n_used} 只样本，{len(signals)} 个信号）")
    print(f"基准线（同池同区间 次日开→收 全样本均值）= {bench:+.2f}%")
    print(f"{'='*94}")
    print(pad("策略", 12) + pad("信号数", 8, ">") + pad("当日剩余", 11, ">")
          + pad("次日收益", 11, ">") + pad("次日胜率", 11, ">")
          + pad("次日可交易", 13, ">") + pad("超额", 10, ">"))
    print("-" * 94)

    g = defaultdict(list)
    for s in signals:
        r_day = simulate(s, None, "day", "signal")
        r_next = simulate(s, None, "next", "signal")
        t = simulate(s, None, "next", "next_open")
        if r_day and r_next and t:
            g[s["rule"]].append((r_day[0], r_next[0], t[0]))

    for rule in ("breakout", "surge", "pullback"):
        rs = g.get(rule) or []
        if not rs:
            continue
        same = [x[0] for x in rs]
        nxt = [x[1] for x in rs]
        trd = [x[2] for x in rs]
        win = sum(1 for v in nxt if v > 0) / len(nxt) * 100
        print(pad(rule, 12) + pad(len(rs), 8, ">")
              + pad(f"{statistics.mean(same):.2f}%", 11, ">")
              + pad(f"{statistics.mean(nxt):.2f}%", 11, ">")
              + pad(f"{win:.1f}%", 11, ">")
              + pad(f"{statistics.mean(trd):.2f}%", 13, ">")
              + pad(f"{statistics.mean(trd) - bench:+.2f}%", 10, ">"))
    print("-" * 94)
    print("当日剩余 = 信号价 → 当日收盘；次日收益 = 信号价 → 次日收盘；")
    print("次日可交易 = 次日开盘买 → 次日收盘卖；超额 = 次日可交易 − 基准线。")


# ---------------- 止损扫描（本次交付重点） ----------------
def optimize(n_stocks=200, days=30):
    signals, bench, n_used = _collect(n_stocks, days)
    if not signals:
        print("没有信号。")
        return

    cutoff = tc.recent_trading_days(days)[0]
    print(f"\n{'#'*96}")
    print(f"# 止损参数扫描")
    print(f"# 样本：{n_used} 只 / {len(signals)} 个信号 / {cutoff} 起")
    print(f"# 基准：同池同区间 次日开→收 = {bench:+.2f}%")
    print(f"# 口径：信号价买入，次日收盘平仓，期间最低价触及止损价即离场")
    print(f"{'#'*96}")

    # ---- 表 1：全样本，逐档止损 ----
    print(f"\n【表 1】全样本 · 信号价买入 → 次日收盘")
    _print_stop_table(signals, horizon="next", entry="signal")

    # ---- 表 2：次日开盘买入（可交易口径） ----
    print(f"\n【表 2】次日开盘买入 → 次日收盘（真实可成交口径）")
    _print_stop_table(signals, horizon="next", entry="next_open")

    # ---- 表 3：当日收盘平仓 ----
    print(f"\n【表 3】信号价买入 → 当日收盘（只做日内）")
    _print_stop_table(signals, horizon="day", entry="signal")

    # ---- 表 4：每个策略的最优止损 ----
    print(f"\n【表 4】分策略最优止损（口径：信号价买入 → 次日收盘）")
    by_rule = defaultdict(list)
    for s in signals:
        by_rule[s["rule"]].append(s)

    print(pad("策略", 12) + pad("最优止损", 10) + pad("期望收益", 11, ">")
          + pad("胜率", 9, ">") + pad("止损触发率", 12, ">")
          + pad("不止损期望", 12, ">") + pad("改善", 10, ">"))
    print("-" * 96)
    for rule in ("breakout", "surge", "pullback"):
        ss = by_rule.get(rule) or []
        if len(ss) < 5:
            print(pad(rule, 12) + pad(f"样本仅 {len(ss)} 条，跳过", 40))
            continue
        best, best_mean, base_mean = None, -1e9, None
        rows = []
        for sp in STOP_LEVELS:
            rets = [r for r in (simulate(s, sp, "next", "signal") for s in ss) if r]
            d = describe([x[0] for x in rets])
            if not d:
                continue
            trig = sum(1 for x in rets if x[1]) / len(rets) * 100
            rows.append((sp, d, trig))
            if sp is None:
                base_mean = d["mean"]
            if d["mean"] > best_mean:
                best, best_mean = sp, d["mean"]
        if best_mean <= -1e8 or base_mean is None:
            continue
        best_row = next(r for r in rows if r[0] == best)
        label = "不止损" if best is None else f"{best:.1f}%"
        print(pad(rule, 12) + pad(label, 10)
              + pad(f"{best_row[1]['mean']:+.2f}%", 11, ">")
              + pad(f"{best_row[1]['win']:.1f}%", 9, ">")
              + pad(f"{best_row[2]:.1f}%", 12, ">")
              + pad(f"{base_mean:+.2f}%", 12, ">")
              + pad(f"{best_row[1]['mean'] - base_mean:+.2f}pp", 10, ">"))

    print("\n注：止损按止损价成交，属乐观假设（忽略跳空低开与跌停封死卖不出）。")
    print("    实盘止损成交价通常更差，真实期望会低于表中数值。")


def _print_stop_table(signals, horizon, entry):
    print(pad("止损档位", 10) + pad("样本", 7, ">") + pad("触发率", 9, ">")
          + pad("平均收益", 11, ">") + pad("胜率", 9, ">")
          + pad("均盈", 9, ">") + pad("均亏", 9, ">")
          + pad("盈亏比", 9, ">") + pad("最差单笔", 11, ">")
          + pad("5%分位", 10, ">") + pad("收益/尾部", 11, ">"))
    print("-" * 111)
    for sp in STOP_LEVELS:
        rets = [r for r in (simulate(s, sp, horizon, entry) for s in signals) if r]
        if not rets:
            continue
        d = describe([x[0] for x in rets])
        trig = sum(1 for x in rets if x[1]) / len(rets) * 100
        label = "不止损" if sp is None else f"{sp:.1f}%"
        # 收益/尾部：每承担 1 个点的最大单笔亏损，换来多少平均收益
        ratio = d["mean"] / abs(d["worst"]) if d["worst"] else float("inf")
        print(pad(label, 10) + pad(d["n"], 7, ">")
              + pad(f"{trig:.1f}%", 9, ">")
              + pad(f"{d['mean']:+.2f}%", 11, ">")
              + pad(f"{d['win']:.1f}%", 9, ">")
              + pad(f"{d['avg_win']:+.2f}%", 9, ">")
              + pad(f"{d['avg_loss']:+.2f}%", 9, ">")
              + pad(f"{d['pl_ratio']:.2f}", 9, ">")
              + pad(f"{d['worst']:+.2f}%", 11, ">")
              + pad(f"{d['p05']:+.2f}%", 10, ">")
              + pad(f"{ratio:.3f}", 11, ">"))
    print("-" * 111)


# ---------------- 止盈 / 移动止盈扫描 ----------------
TP_LEVELS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, None)
DD_LEVELS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, None)


def _tp_label(tp):
    return "不止盈" if tp is None else f"{tp:.1f}%"


def _dd_label(dd):
    return "不回撤" if dd is None else f"{dd:.1f}%"


def optimize_tp(n_stocks=200, days=30, stop_pct=-2.0):
    from collections import Counter

    signals, bench, n_used = _collect(n_stocks, days)
    if not signals:
        print("没有信号。")
        return

    cutoff = tc.recent_trading_days(days)[0]
    print(f"\n{'#'*100}")
    print(f"# 止盈 / 移动止盈 参数扫描")
    print(f"# 样本：{n_used} 只 / {len(signals)} 个信号 / {cutoff} 起 / 基准 {bench:+.2f}%")
    print(f"# 固定：硬止损 {stop_pct:.1f}%，信号价买入 → 次日收盘")
    print(f"# 口径：涨到 +TP% 后进入移动止盈，从峰值回撤 DD% 离场；同一 bar 内按最坏情况算")
    print(f"{'#'*100}")

    # ---- 矩阵 A：期望收益 ----
    print(f"\n【矩阵 A】期望收益（行=止盈激活线，列=峰值回撤）")
    _tp_matrix(signals, stop_pct, "mean")

    # ---- 矩阵 B：胜率 ----
    print(f"\n【矩阵 B】胜率")
    _tp_matrix(signals, stop_pct, "win")

    # ---- 最优组合 ----
    combos = []
    for tp in TP_LEVELS:
        for dd in DD_LEVELS:
            if tp is None and dd is not None:
                continue          # 不止盈时「回撤」等价于无门槛移动止盈，单独一列已覆盖
            rets = [r for r in (simulate_tp(s, stop_pct, tp, dd) for s in signals) if r]
            if not rets:
                continue
            d = describe([x[0] for x in rets])
            combos.append((tp, dd, d, Counter(x[1] for x in rets), len(rets)))
    # 补：不止盈 + 各档回撤（无门槛移动止盈）
    for dd in DD_LEVELS:
        if dd is None:
            continue
        rets = [r for r in (simulate_tp(s, stop_pct, None, dd) for s in signals) if r]
        if not rets:
            continue
        d = describe([x[0] for x in rets])
        combos.append((None, dd, d, Counter(x[1] for x in rets), len(rets)))

    combos.sort(key=lambda c: -c[2]["mean"])

    print(f"\n【表 C】最优组合 Top 8（按期望收益排序）")
    print(pad("排名", 6) + pad("止盈线", 10) + pad("回撤", 10) + pad("期望收益", 11, ">")
          + pad("胜率", 9, ">") + pad("最差单笔", 11, ">")
          + pad("止损离场", 11, ">") + pad("止盈离场", 11, ">")
          + pad("持有到期", 11, ">"))
    print("-" * 100)
    for i, (tp, dd, d, reasons, n) in enumerate(combos[:8], 1):
        print(pad(i, 6) + pad(_tp_label(tp), 10) + pad(_dd_label(dd), 10)
              + pad(f"{d['mean']:+.2f}%", 11, ">")
              + pad(f"{d['win']:.1f}%", 9, ">")
              + pad(f"{d['worst']:+.2f}%", 11, ">")
              + pad(f"{reasons.get('stop',0)/n*100:.1f}%", 11, ">")
              + pad(f"{reasons.get('trail',0)/n*100:.1f}%", 11, ">")
              + pad(f"{reasons.get('close',0)/n*100:.1f}%", 11, ">"))
    print("-" * 100)

    # ---- 对照：当前配置 ----
    cur_tp, cur_dd = 5.0, 2.0
    cur = [r for r in (simulate_tp(s, stop_pct, cur_tp, cur_dd) for s in signals) if r]
    curd = describe([x[0] for x in cur])
    cur_reasons = Counter(x[1] for x in cur)
    best = combos[0]
    print(f"\n【表 D】当前配置 vs 最优")
    print(pad("配置", 22) + pad("期望收益", 11, ">") + pad("胜率", 9, ">")
          + pad("最差单笔", 11, ">") + pad("止盈离场占比", 13, ">"))
    print("-" * 70)
    for label, d, reasons, n in (
        (f"当前 {_tp_label(cur_tp)}/{_dd_label(cur_dd)}", curd, cur_reasons, len(cur)),
        (f"最优 {_tp_label(best[0])}/{_dd_label(best[1])}", best[2], best[3], best[4]),
    ):
        print(pad(label, 22) + pad(f"{d['mean']:+.2f}%", 11, ">")
              + pad(f"{d['win']:.1f}%", 9, ">")
              + pad(f"{d['worst']:+.2f}%", 11, ">")
              + pad(f"{reasons.get('trail',0)/n*100:.1f}%", 13, ">"))
    print("-" * 70)

    # ---- 分策略 ----
    print(f"\n【表 E】分策略最优止盈组合")
    by_rule = defaultdict(list)
    for s in signals:
        by_rule[s["rule"]].append(s)
    print(pad("策略", 12) + pad("最优组合", 18) + pad("期望收益", 11, ">")
          + pad("胜率", 9, ">") + pad("最差单笔", 11, ">"))
    print("-" * 70)
    for rule in ("breakout", "surge", "pullback"):
        ss = by_rule.get(rule) or []
        if len(ss) < 5:
            print(pad(rule, 12) + pad(f"样本仅 {len(ss)} 条，跳过", 30))
            continue
        best_r, best_m = None, -1e9
        for tp in TP_LEVELS:
            for dd in DD_LEVELS:
                if tp is None and dd is not None:
                    continue
                rets = [r for r in (simulate_tp(s, stop_pct, tp, dd) for s in ss) if r]
                if len(rets) < 5:
                    continue
                m = statistics.mean([x[0] for x in rets])
                if m > best_m:
                    best_m, best_r = m, (tp, dd, rets)
        if best_r is None:
            continue
        tp, dd, rets = best_r
        d = describe([x[0] for x in rets])
        print(pad(rule, 12) + pad(f"{_tp_label(tp)}/{_dd_label(dd)}", 18)
              + pad(f"{d['mean']:+.2f}%", 11, ">")
              + pad(f"{d['win']:.1f}%", 9, ">")
              + pad(f"{d['worst']:+.2f}%", 11, ">"))
    print("-" * 70)

    print("\n注：止盈/止损均按触发价成交，属乐观假设（忽略跳空与跌停封死卖不出）。")
    print("    回撤列填「不回撤」= 只靠止盈线、不跟踪峰值，触发后一直持有到次日收盘。")


def _tp_matrix(signals, stop_pct, metric):
    header = pad("止盈\\回撤", 13) + "".join(pad(_dd_label(dd), 11, ">") for dd in DD_LEVELS)
    print(header)
    print("-" * (13 + 11 * len(DD_LEVELS)))
    best_val = -1e9
    best_cell = None
    rows = []
    for tp in TP_LEVELS:
        cells = []
        for dd in DD_LEVELS:
            rets = [r for r in (simulate_tp(s, stop_pct, tp, dd) for s in signals) if r]
            if not rets:
                cells.append(pad("-", 11, ">"))
                continue
            vals = [x[0] for x in rets]
            if metric == "mean":
                v = statistics.mean(vals)
                cells.append(pad(f"{v:+.2f}%", 11, ">"))
            else:
                v = sum(1 for x in vals if x > 0) / len(vals) * 100
                cells.append(pad(f"{v:.1f}%", 11, ">"))
            if v > best_val:
                best_val, best_cell = v, (tp, dd)
        rows.append((tp, cells))
    for tp, cells in rows:
        print(pad(_tp_label(tp), 13) + "".join(cells))
    print("-" * (13 + 11 * len(DD_LEVELS)))
    if best_cell:
        val = f"{best_val:+.2f}%" if metric == "mean" else f"{best_val:.1f}%"
        print(f"  最优：止盈 {_tp_label(best_cell[0])} / 回撤 {_dd_label(best_cell[1])} → {val}")


# ---------------- 公共采集（带本地缓存） ----------------
def _cache_path(n_stocks, days):
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                        f"bt_{n_stocks}_{days}_v2.pkl")


def _collect(n_stocks=200, days=30, use_cache=True):
    """拉样本池 + 5 分钟 K 线 + 重放。结果缓存一天，便于反复扫参数。"""
    import os
    import pickle
    import time as _t

    cp = _cache_path(n_stocks, days)
    if use_cache and os.path.exists(cp):
        try:
            if _t.time() - os.path.getmtime(cp) < 86400:
                with open(cp, "rb") as f:
                    d = pickle.load(f)
                print(f"[回测] 命中缓存 {cp}（{d['n_used']} 只 / {len(d['signals'])} 信号）")
                return d["signals"], d["bench"], d["n_used"]
        except Exception:
            pass

    print("[回测] 拉全市场快照，取成交额前 N 只作为样本池 ...")
    snaps = feed.fetch_all(quiet=True)
    pool = sorted(snaps.values(), key=lambda s: -s.amount)[:n_stocks]
    codes = [s.code for s in pool]
    print(f"[回测] 样本 {len(codes)} 只，拉 5 分钟 K 线 ...")

    data = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for code, by_day in zip(codes, ex.map(fetch_5min, codes)):
            if by_day:
                data[code] = by_day
    print(f"[回测] 拿到 {len(data)} 只的历史 5 分钟 K 线")

    cutoff = tc.recent_trading_days(days)[0]
    signals = []
    for code, by_day in data.items():
        for s in scan_one(code, by_day):
            if s["date"] >= cutoff and s["next_close"] is not None:
                signals.append(s)

    # 基准线：同池同区间，全部「次日开盘买→次日收盘卖」
    base = []
    for by_day in data.values():
        dates = sorted(by_day)
        for j in range(len(dates) - 1):
            if dates[j] < cutoff or not by_day[dates[j]]:
                continue
            nxt = by_day[dates[j + 1]]
            if nxt and nxt[0][1]:
                base.append((nxt[-1][2] / nxt[0][1] - 1) * 100)
    bench = statistics.mean(base) if base else 0.0

    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "wb") as f:
            pickle.dump({"signals": signals, "bench": bench, "n_used": len(data)}, f)
        print(f"[回测] 已缓存到 {cp}")
    except Exception:
        pass
    return signals, bench, len(data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--optimize-stop", action="store_true")
    ap.add_argument("--optimize-take-profit", action="store_true")
    args = ap.parse_args()
    if args.optimize_take_profit:
        optimize_tp(args.stocks, args.days)
    elif args.optimize_stop:
        optimize(args.stocks, args.days)
    else:
        run(args.stocks, args.days)
