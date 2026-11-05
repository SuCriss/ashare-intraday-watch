#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== positions 状态分布 ==="
python3 - <<'PY'
import store
store.init()
with store.conn() as c:
    for r in c.execute("SELECT trade_date, state, confirmed, COUNT(*) n FROM positions GROUP BY trade_date, state, confirmed ORDER BY trade_date"):
        print(f"  {r['trade_date']}  state={r['state']:<10} confirmed={r['confirmed']}  {r['n']} 只")
PY
echo
echo "=== 收盘相关日志 ==="
journalctl -u intraday-watch --since "14:55" --no-pager -o cat > /tmp/j.log 2>&1
grep -c "观察名单" /tmp/j.log || echo "  观察名单出现 0 次"
grep "收盘" /tmp/j.log | head -5
echo
echo "=== 今日推送次数 ==="
python3 -c "import store; store.init(); print(' pushes:', sum(store.pushes_today('2026-11-05').values(), (0,0))[0] if store.pushes_today('2026-11-05') else 0)"
