# -*- coding: utf-8 -*-
"""用实时快照预演收盘观察名单（只渲染，不发送、不归档）。"""
import datetime
import feed
import notify
import store

store.init()
today = datetime.date.today().isoformat()

print("[1] 拉快照 ...")
snaps = feed.fetch_all(quiet=True, top_pages=30, timeout=30)
print(f"    {len(snaps)} 只")

print("[2] 读待确认 ...")
pend = store.load_pending(today)
print(f"    {len(pend)} 只")

rows = []
for p in pend:
    s = snaps.get(p["code"])
    sp = p["signal_price"] or 0
    if s is None or sp <= 0:
        continue
    rows.append({
        "code": p["code"], "name": p["name"],
        "signal_price": sp, "close": s.price,
        "pnl": (s.price / sp - 1) * 100,
        "peak_pnl": ((p["peak"] or sp) / sp - 1) * 100,
        "low_pnl": ((p["low"] or sp) / sp - 1) * 100,
    })

print(f"[3] 可渲染 {len(rows)} 只\n")
rows.sort(key=lambda r: -r["pnl"])
card = notify.build_watchlist_card(rows, today)
print("标题:", card.title)
print("颜色:", card.color)
for s in card.sections:
    print(f"  {s.head}")
    print(f"    {s.body}")
    print(f"    {s.note}")
print("\n底部:", card.footer)

print("\n[4] 已确认的持仓（会告警的）:")
with store.conn() as c:
    conf = list(c.execute(
        "SELECT code,name,signal_price,state FROM positions WHERE confirmed=1"))
print(f"    {len(conf)} 只 {[r['code'] for r in conf]}")
