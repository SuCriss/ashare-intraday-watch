#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 代码版本 ==="
git log --oneline -1
echo
echo "=== 服务 ==="
systemctl is-active intraday-watch
echo
echo "=== 最近采集 ==="
journalctl -u intraday-watch --no-pager -o cat | grep '\[采集\] 5571' | tail -5
echo
echo "=== 推送记录 ==="
journalctl -u intraday-watch --no-pager -o cat | grep '已发送' | tail -3
echo
echo "=== 报错 ==="
journalctl -u intraday-watch --no-pager -o cat | grep -E '失败|错误|Traceback' | tail -5
echo
echo "=== 当前时间 ==="
TZ=Asia/Shanghai date '+%H:%M:%S %Z'
