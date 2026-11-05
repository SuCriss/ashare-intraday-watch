#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 当前北京时间 ==="
TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z'
echo
echo "=== 代码版本 ==="
git log --oneline -1
echo
echo "=== 服务状态 ==="
systemctl is-active intraday-watch
systemctl is-enabled intraday-watch
echo
echo "=== 文件是否到位 ==="
ls -1 *.py *.sh 2>/dev/null | tr '\n' ' '
echo
echo
echo "=== 15:00 之后收盘动作 ==="
journalctl -u intraday-watch --since "14:58" --no-pager -o cat | grep -E "收盘|观察名单" | tail -8
echo
echo "=== 最近日志（20 行）==="
journalctl -u intraday-watch --no-pager -o cat -n 20
echo
echo "=== 持仓状态统计 ==="
python3 - <<'PY'
import store
store.init()
with store.conn() as c:
    for row in c.execute("SELECT state, confirmed, COUNT(*) n FROM positions GROUP BY state, confirmed"):
        print(f"  state={row['state']:<10} confirmed={row['confirmed']}  {row['n']} 只")
PY
