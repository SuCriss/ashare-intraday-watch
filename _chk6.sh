#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 版本 / 服务 ==="
git log --oneline -1
systemctl is-active intraday-watch
echo
echo "=== 五层漏斗 ==="
TZ=Asia/Shanghai python3 run.py --once > /tmp/once.log 2>&1
grep -E "环境|采集|过滤" /tmp/once.log | head -5
echo
echo "=== 候选（若有）==="
grep -E "^[0-9]{6}  " /tmp/once.log | head -5
echo "(以上为空 = 今天没有满足全部条件的标的)"
