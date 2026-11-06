#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 今日推送明细 ==="
TZ=Asia/Shanghai python3 - <<'PY'
import store, datetime
store.init()
today = datetime.date.today().isoformat()
with store.conn() as c:
    rows = list(c.execute(
        "SELECT code,name,clock,price,pct,score,primary_rule FROM pushes "
        "WHERE trade_date=? ORDER BY ts", (today,)))
print(f"总推送 {len(rows)} 次")
for r in rows:
    print(f"  {r['clock']}  {r['code']} {r['name']:<8} {r['price']:>8.2f} {r['pct']:+6.2f}%  "
          f"score={r['score']:.0f}  {r['primary_rule']}")
PY
echo
echo "=== 今日候选总数 ==="
TZ=Asia/Shanghai python3 -c "
import store, datetime
store.init()
today = datetime.date.today().isoformat()
with store.conn() as c:
    n = c.execute('SELECT COUNT(*) FROM candidates WHERE trade_date=?', (today,)).fetchone()[0]
    k = c.execute('SELECT COUNT(DISTINCT code) FROM candidates WHERE trade_date=?', (today,)).fetchone()[0]
print(f'候选记录 {n} 条 / {k} 只')
"
echo
echo "=== 收盘日报是否已发 ==="
journalctl -u intraday-watch --since "15:00" --no-pager -o cat 2>/dev/null | grep -E "收盘|观察名单" | tail -4 || echo "(时区问题，换个查法)"
journalctl -u intraday-watch --since "20 minutes ago" --no-pager -o cat 2>/dev/null | grep -E "收盘|观察名单" | tail -4
