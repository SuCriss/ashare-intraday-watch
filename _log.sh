#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 当前时间 ==="
TZ=Asia/Shanghai date '+%H:%M:%S %Z'
echo "=== 服务 ==="
systemctl is-active intraday-watch
echo
echo "=== 最近 8 分钟日志 ==="
journalctl -u intraday-watch --since "8 minutes ago" --no-pager -o cat > /tmp/l.log 2>&1
echo "行数: $(wc -l < /tmp/l.log)"
echo "--- 最后 30 行 ---"
tail -n 30 /tmp/l.log
echo
echo "=== 东财可用性 ==="
curl -s -m 8 -o /dev/null -w "push2delay: http=%{http_code} t=%{time_total}s\n" \
  -A "Mozilla/5.0" "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=3&fid=f12&fs=m:0+t:6&fields=f2,f12" || echo "push2delay: 失败"
