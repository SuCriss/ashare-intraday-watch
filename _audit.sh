#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 1. 未来 8 天交易日判定 ==="
TZ=Asia/Shanghai python3 - <<'PY'
import datetime as dt
import trading_calendar as tc
d = dt.date.today()
for i in range(9):
    day = d + dt.timedelta(days=i)
    wk = "一二三四五六日"[day.weekday()]
    print(f"  {day} 周{wk}  {'交易' if tc.is_trading_day(day) else '休市'}")
PY

echo
echo "=== 2. systemd 自愈配置 ==="
systemctl show intraday-watch -p Restart -p RestartUSec -p ExecStart --no-pager | sed 's/^/  /'
echo "  enabled: $(systemctl is-enabled intraday-watch)"

echo
echo "=== 3. .env 密钥是否持久 ==="
ls -l .env 2>/dev/null | sed 's/^/  /'
grep -c 'FEISHU_WEBHOOK=https' .env 2>/dev/null | sed 's/^/  FEISHU_WEBHOOK 已配置: /'

echo
echo "=== 4. 今日推送统计 ==="
TZ=Asia/Shanghai python3 - <<'PY'
import store, datetime
store.init()
today = datetime.date.today().isoformat()
p = store.pushes_today(today)
print(f"  今日推送记录: {sum(n for n,_ in p.values()) if p else 0} 条 / {len(p)} 只")
PY

echo
echo "=== 5. 今日日志中的推送结果 ==="
journalctl -u intraday-watch --since "3 hours ago" --no-pager -o cat > /tmp/today.log 2>&1
echo "  推送成功: $(grep -c '已发送' /tmp/today.log) 次"
echo "  推送失败: $(grep -c '失败' /tmp/today.log) 次"
echo "  暂停推送: $(grep -c '暂停推送' /tmp/today.log) 次"
echo "  异常堆栈: $(grep -c 'Traceback' /tmp/today.log) 次"
