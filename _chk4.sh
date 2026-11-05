#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
python3 - <<'PY'
import store
store.init()
with store.conn() as c:
    rows = c.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    codes = c.execute("SELECT COUNT(DISTINCT code) FROM daily").fetchone()[0]
    dates = c.execute("SELECT COUNT(DISTINCT trade_date) FROM daily").fetchone()[0]
print(f"daily 行数={rows}  股票数={codes}  日期数={dates}")
PY
echo "--- 回填进程 ---"
ps -eo pid,etime,cmd | grep "history.py" | grep -v grep || echo "  已结束"
echo "--- 服务 ---"
systemctl is-active intraday-watch
