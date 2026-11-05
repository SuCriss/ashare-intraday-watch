# -*- coding: utf-8 -*-
"""确认推送标的的买入状态。

推送出去的票默认是 pending（只跟踪不告警）。你实际买入了才用这个脚本确认，
确认后服务才会对它做止损/止盈告警。

用法：
  python3 mark.py --list                    # 看今天推了哪些、还没确认
  python3 mark.py 688037                    # 确认买入，成本价 = 推送价
  python3 mark.py 688037 --price 222.50     # 确认买入，成本价 = 实际成交价
  python3 mark.py 688037 688521             # 一次确认多只
  python3 mark.py --all                     # 确认今日全部（成本价用推送价）
  python3 mark.py 688037 --skip             # 明确不买，移出监控
"""
import argparse
import datetime
import sys

import config
import store


def _fmt(v, w=8, dec=2):
    return f"{v:>{w}.{dec}f}"


def cmd_list(trade_date):
    pend = store.load_pending(trade_date)
    with store.conn() as c:
        conf = [dict(r) for r in c.execute(
            "SELECT code,name,signal_price,state FROM positions "
            "WHERE trade_date=? AND confirmed=1", (trade_date,))]
    print(f"\n=== {trade_date} 推送跟踪 ===")
    if pend:
        print(f"\n待确认（{len(pend)} 只，不会告警）：")
        print(f"  {'代码':<8}{'名称':<10}{'信号价':>9}{'峰值':>9}{'低点':>9}")
        for p in pend:
            print(f"  {p['code']:<8}{p['name']:<10}{_fmt(p['signal_price'])}"
                  f"{_fmt(p['peak'] or 0)}{_fmt(p['low'] or 0)}")
        print(f"\n  确认买入：python3 mark.py {' '.join(p['code'] for p in pend[:3])}"
              f"{' ...' if len(pend) > 3 else ''}")
        print(f"  全部确认：python3 mark.py --all")
    else:
        print("\n待确认：无")
    if conf:
        print(f"\n已确认（{len(conf)} 只，正在监控）：")
        for c_ in conf:
            print(f"  {c_['code']:<8}{c_['name']:<10}成本 {_fmt(c_['signal_price'])}"
                  f"  状态 {c_['state']}")
    else:
        print("\n已确认：无")
    print()


def cmd_confirm(codes, price, trade_date, skip):
    ok, fail = [], []
    for code in codes:
        if skip:
            n = store.skip_position(code, trade_date)
            (ok if n else fail).append(code)
            continue
        r = store.confirm_position(code, price, trade_date)
        (ok if r else fail).append(code)

    if ok:
        verb = "已跳过" if skip else "已确认买入"
        print(f"\n✅ {verb} {len(ok)} 只：{', '.join(ok)}")
        if not skip:
            print(f"   成本价：{'统一 ' + str(price) if price else '沿用推送价'}")
            print("   服务会在 60 秒内自动同步，之后触发止损/止盈才告警。")
    if fail:
        print(f"\n⚠ 未找到 {len(fail)} 只：{', '.join(fail)}")
        print("   只认今天或更早推送过的票，检查代码是否正确。")
    print()


def main():
    ap = argparse.ArgumentParser(description="确认推送标的的买入状态")
    ap.add_argument("codes", nargs="*", help="股票代码（6 位），可多个")
    ap.add_argument("--price", type=float, help="实际成交价，不填用推送价")
    ap.add_argument("--all", action="store_true", help="确认今日全部待确认")
    ap.add_argument("--skip", action="store_true", help="明确不买，移出监控")
    ap.add_argument("--list", action="store_true", help="列出今日待确认/已确认")
    ap.add_argument("--date", default=None, help="交易日，默认今天")
    args = ap.parse_args()

    store.init()
    trade_date = args.date or datetime.date.today().isoformat()

    if args.list or (not args.codes and not args.all):
        cmd_list(trade_date)
        return

    codes = list(args.codes)
    if args.all:
        codes = [p["code"] for p in store.load_pending(trade_date)]
        if not codes:
            print("今天没有待确认的标的。")
            return
        if args.price:
            print("--all 不能配 --price（各票成本价不同）")
            sys.exit(1)

    cmd_confirm(codes, args.price, trade_date, args.skip)


if __name__ == "__main__":
    main()
