#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
git fetch -q origin
git reset --hard origin/main -q
systemctl restart intraday-watch
sleep 12
echo "版本: $(git log --oneline -1)"
echo "服务: $(systemctl is-active intraday-watch)"
echo
echo "=== 实时日志 ==="
journalctl -u intraday-watch --since "1 minute ago" --no-pager -o cat | tail -12
