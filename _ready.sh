#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 时间 ==="
TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z'
echo
echo "=== 版本 / 服务 ==="
git log --oneline -1
echo "active:   $(systemctl is-active intraday-watch)"
echo "enabled:  $(systemctl is-enabled intraday-watch)"
echo
echo "=== 交易日判定 ==="
TZ=Asia/Shanghai python3 -c "import trading_calendar as tc; print('今天是否交易日:', tc.is_trading_day())"
echo
echo "=== 日线历史覆盖 ==="
TZ=Asia/Shanghai python3 -c "
import history
h = history.DailyHistory()
h.refresh()
print(h.stats())
# 检查关键天数
import collections
c = collections.Counter(len(v) for v in h.data.values())
print('分布:', dict(sorted(c.items(), reverse=True)[:6]))
n20 = sum(1 for v in h.data.values() if len(v) >= 20)
print(f'历史>=20天的股票: {n20} 只 (MA20/平台突破需要)')
"
echo
echo "=== 数据库 ==="
TZ=Asia/Shanghai python3 -c "
import store
store.init()
print(store.stats_overview())
"
echo
echo "=== 最近日志 ==="
journalctl -u intraday-watch --no-pager -o cat -n 6
