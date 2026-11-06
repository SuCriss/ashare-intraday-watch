#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "时间: $(TZ=Asia/Shanghai date '+%H:%M:%S')"
echo
echo "=== 指数实时（腾讯）==="
curl -s -m 8 "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688" | iconv -f gbk -t utf-8 2>/dev/null | while IFS= read -r line; do
  [ -z "$line" ] && continue
  echo "$line" | awk -F'~' '{printf "  %-8s 现价=%-10s 昨收=%-10s 涨跌=%s%%\n", $2, $4, $5, $33}'
done
echo
echo "=== 服务最近 3 轮 ==="
journalctl -u intraday-watch --since "3 minutes ago" --no-pager -o cat > /tmp/l2.log 2>&1
grep -E "扫描|环境|采集】|采集\]" /tmp/l2.log | tail -9
echo
echo "=== 市场宽度（最近一轮）==="
grep "扫描 " /tmp/l2.log | tail -1
echo
echo "=== 推送状态 ==="
grep -E "暂停推送|已发送" /tmp/l2.log | tail -2
