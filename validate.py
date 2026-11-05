# -*- coding: utf-8 -*-
"""实盘 vs 回测 对比：验证策略在真实盘中是否还成立。

为什么需要这个：
  回测再严谨也只是「历史上成立」。research.py 的 10 条信号全部来自
  批量假设测试（data mining），有二次挖掘和多重检验的残余风险。
  唯一的真正检验是：实盘跑一段，看真实表现和回测预期是否一致。

口径（与回测严格对齐，才能比）：
  信号日 D 盘中触发 → 次日 D+1 开盘买入 → D+1 收盘卖出
  超额 = 该笔收益 − 当日全市场等权「次日开→收」收益
  扣 0.15% 双边成本

数据来源：
  实盘信号  candidates 表（每轮扫描的全量候选，不受推送限流影响）
  收益      daily 表（收盘快照 + 历史回填）
  基准      daily 表当日全市场

用法：
  python validate.py                # 看最近 60 天
  python validate.py --days 120
  python validate.py --min-n 20     # 调整判定所需最小样本
"""
import argparse
import datetime
import statistics
import sys
import unicodedata
from collections import defaultdict

import store

COST = 0.15

# research.py 的验证结果（967 只 / 2020-2026 / 扣成本后的 T+1 超额）
BACKTEST_EXPECT = {
    "limit_hold":         {"name": "涨停次日",   "full": 0.69, "oos": 0.59, "entry": "next_open"},
    "limit_down_rebound": {"name": "跌停反弹",   "full": 0.69, "oos": 0.82, "entry": "next_open"},
    "high_vol_doji":      {"name": "放量十字星", "full": 0.34, "oos": 0.34, "entry": "next_open"},
    "first_up_after_drop": {"name": "连跌首阳",  "full": 0.28, "oos": 0.36, "entry": "next_open"},
    "pullback":           {"name": "回踩反包",   "full": 0.22, "oos": 0.27, "entry": "next_open"},
    "low_open_up":        {"name": "低开高走",   "full": 0.22, "oos": 0.27, "entry": "next_open"},
    "ma20_reclaim":       {"name": "站上MA20",   "full": 0.10, "oos": 0.10, "entry": "next_open"},
    "jump_hold":          {"name": "跳空不回落", "full": 0.45, "oos": 0.69, "entry": "close"},
    "platform_break":     {"name": "平台突破",   "full": 0.23, "oos": 0.42, "entry": "close"},
    "three_white":        {"name": "三连阳",     "full": 0.16, "oos": 0.39, "entry": "close"},
}


def pad(s, n, align="<"):
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))
    d = n - w
    if d <= 0:
        return str(s)
    return str(s) + " " * d if align == "<" else " " * d + str(s)


def load_daily():
    """{code: [(date, open, close), ...]} 升序"""
    out = defaultdict(list)
    with store.conn() as c:
        for r in c.execute(
                "SELECT code, trade_date, open, close FROM daily ORDER BY code, trade_date"):
            out[r["code"]].append((r["trade_date"], r["open"], r["close"]))
    return out


def build_benchmark(daily):
    """{date: 当日全市场「次日开→收」等权收益%}"""
    acc = defaultdict(list)
    for code, rows in daily.items():
        for i in range(len(rows) - 1):
            d, _o, _c = rows[i]
            nxt = rows[i + 1]
            if nxt[1] > 0 and nxt[2] > 0:
                acc[d].append((nxt[2] / nxt[1] - 1) * 100)
    return {d: statistics.mean(v) for d, v in acc.items() if len(v) >= 100}


def load_live_signals(since):
    """当日每只票每策略只取第一次命中（盘中会重复记录几十次）。"""
    with store.conn() as c:
        rows = list(c.execute(
            """SELECT c.trade_date, c.code, c.primary_rule, c.price, c.score
               FROM candidates c
               JOIN (SELECT trade_date, code, primary_rule, MIN(ts) AS mts
                     FROM candidates WHERE trade_date >= ?
                     GROUP BY trade_date, code, primary_rule) m
                 ON c.trade_date=m.trade_date AND c.code=m.code
                AND c.primary_rule=m.primary_rule AND c.ts=m.mts""",
            (since,)))
    return [dict(r) for r in rows]


def compute(signals, daily, bench):
    """给每条信号算 T+1 收益与超额（口径：次日开盘买 → 次日收盘卖）。"""
    idx = {}
    for code, rows in daily.items():
        idx[code] = {r[0]: i for i, r in enumerate(rows)}

    out = []
    for s in signals:
        rows = daily.get(s["code"])
        if not rows:
            continue
        pos = idx[s["code"]].get(s["trade_date"])
        if pos is None or pos + 1 >= len(rows):
            continue
        nxt = rows[pos + 1]
        o, c = nxt[1], nxt[2]
        if o <= 0 or c <= 0:
            continue
        b = bench.get(s["trade_date"])
        if b is None:
            continue
        ret = (c / o - 1) * 100 - COST
        out.append({
            "date": s["trade_date"], "code": s["code"],
            "rule": s["primary_rule"], "ret": ret, "excess": ret - b,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=20, help="判定所需最小样本")
    args = ap.parse_args()

    store.init()
    since = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()

    daily = load_daily()
    if not daily:
        print("daily 表为空。先跑 `python history.py --backfill`。")
        return
    bench = build_benchmark(daily)
    signals = load_live_signals(since)

    dates = sorted({s["trade_date"] for s in signals})
    print(f"\n{'='*104}")
    print(f"实盘 vs 回测（{since} 起 / 实盘信号 {len(signals)} 条 / "
          f"覆盖 {len(dates)} 个交易日 / 基准日 {len(bench)} 天）")
    if dates:
        print(f"实盘日期：{dates[0]} → {dates[-1]}")
    print(f"口径：次日开盘买 → 次日收盘卖，扣 {COST}% 双边成本，超额 = 收益 − 当日全市场等权")
    print(f"{'='*104}")

    results = compute(signals, daily, bench)
    if not results:
        print("\n还没有可对比的实盘样本（信号需要次日行情才能算收益）。")
        print("等明天收盘后再跑一次。")
        return

    g = defaultdict(list)
    for r in results:
        g[r["rule"]].append(r["excess"])

    print(pad("信号", 14) + pad("实盘n", 7, ">") + pad("实盘超额", 10, ">")
          + pad("回测预期", 10, ">") + pad("偏差", 9, ">")
          + pad("实盘胜率", 10, ">") + "  判定")
    print("-" * 104)

    verdicts = []
    for rule, meta in sorted(BACKTEST_EXPECT.items(),
                             key=lambda kv: -kv[1]["full"]):
        vals = g.get(rule) or []
        expect = meta["full"]
        if not vals:
            print(pad(meta["name"], 14) + pad("0", 7, ">") + pad("-", 10, ">")
                  + pad(f"{expect:+.2f}%", 10, ">") + pad("-", 9, ">")
                  + pad("-", 10, ">") + "  未触发")
            continue

        n = len(vals)
        live = statistics.mean(vals)
        delta = live - expect
        win = sum(1 for v in vals if v > 0) / n * 100

        if n < args.min_n:
            verdict = f"样本不足（需 {args.min_n}）"
        elif live <= 0:
            verdict = "❌ 淘汰：实盘为负"
        elif delta < -0.5:
            verdict = "⚠ 存疑：远低于回测"
        else:
            verdict = "✅ 保留：与回测一致"

        print(pad(meta["name"], 14) + pad(n, 7, ">")
              + pad(f"{live:+.2f}%", 10, ">")
              + pad(f"{expect:+.2f}%", 10, ">")
              + pad(f"{delta:+.2f}pp", 9, ">")
              + pad(f"{win:.1f}%", 10, ">") + f"  {verdict}")
        verdicts.append((meta["name"], n, live, delta, verdict))

    print("-" * 104)

    total = len(results)
    if total:
        allv = [r["excess"] for r in results]
        print(f"\n  合计 {total} 条实盘信号，平均超额 {statistics.mean(allv):+.2f}%，"
              f"胜率 {sum(1 for v in allv if v > 0) / total * 100:.1f}%")

    # 汇总
    done = [v for v in verdicts if not v[4].startswith("样本不足")]
    if not done:
        print(f"\n  结论：所有信号都还没积累到 {args.min_n} 个实盘样本，无法判定。")
        print(f"  按每天 3~5 个信号估算，需要约 {max(1, args.min_n // 4)} 个交易日。")
    else:
        keep = [v for v in done if v[4].startswith("✅")]
        drop = [v for v in done if v[4].startswith("❌")]
        warn = [v for v in done if v[4].startswith("⚠")]
        print(f"\n  结论：保留 {len(keep)} / 存疑 {len(warn)} / 淘汰 {len(drop)}")
        for name, n, live, delta, _ in drop:
            print(f"    ❌ {name}  实盘 {live:+.2f}%（n={n}）→ 从 strategies.py 删除")
        for name, n, live, delta, _ in warn:
            print(f"    ⚠ {name}  实盘 {live:+.2f}% vs 回测预期，偏差 {delta:+.2f}pp")


if __name__ == "__main__":
    main()
