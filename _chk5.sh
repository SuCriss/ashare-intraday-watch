#!/usr/bin/env bash
cd /opt/ashare-intraday-watch || exit 1
echo "=== 版本 ==="
git log --oneline -1
echo
echo "=== 策略加载 ==="
TZ=Asia/Shanghai python3 -c "import strategies; print([f.__name__ for f in strategies.REGISTRY])"
echo
echo "=== 历史缓存 ==="
TZ=Asia/Shanghai python3 -c "import history; h=history.DailyHistory(); h.refresh(); print(h.stats())"
echo
echo "=== 服务 ==="
systemctl is-active intraday-watch
