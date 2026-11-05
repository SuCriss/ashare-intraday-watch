#!/usr/bin/env bash
# 部署 A 股盘中盯盘到本机（服务器端执行）
set -euo pipefail

APP_DIR=/opt/ashare-intraday-watch
REPO=https://github.com/SuCriss/ashare-intraday-watch.git
SVC=intraday-watch

echo "=== 1. 拉代码 ==="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --all -q
  git -C "$APP_DIR" reset --hard origin/main -q
  echo "  已更新：$(git -C "$APP_DIR" log --oneline -1)"
else
  git clone -q "$REPO" "$APP_DIR"
  echo "  已克隆：$(git -C "$APP_DIR" log --oneline -1)"
fi

echo
echo "=== 2. 配置 .env ==="
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'ENVEOF'
# ===== 推送渠道：填哪个用哪个，留空的自动跳过 =====
FEISHU_WEBHOOK=
FEISHU_SECRET=
DINGTALK_WEBHOOK=
DINGTALK_SECRET=
WECOM_WEBHOOK=
TG_TOKEN=
TG_CHAT_ID=
BARK_URL=
CUSTOM_WEBHOOK=
AT_ALL=0

# ===== 海外服务器数据源（push2delay 实测比 push2 稳）=====
EM_HOST=push2delay.eastmoney.com
POLL_SECONDS=15
ENVEOF
  chmod 600 "$APP_DIR/.env"
  echo "  已生成 $APP_DIR/.env（渠道留空，需填 Webhook）"
else
  echo "  已存在，保留原配置"
fi

echo
echo "=== 3. 手动跑一轮验证 ==="
cd "$APP_DIR"
TZ=Asia/Shanghai EM_HOST=push2delay.eastmoney.com POLL_SECONDS=15 \
  timeout 120 python3 run.py --once 2>&1 | tail -20

echo
echo "=== 4. 安装 systemd 服务 ==="
cat > /etc/systemd/system/$SVC.service <<SVCEOF
[Unit]
Description=A-share intraday watch (screener + multi-channel push)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=TZ=Asia/Shanghai
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/python3 run.py --loop --push
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable -q $SVC
systemctl restart $SVC
sleep 10

echo
echo "=== 5. 服务状态 ==="
systemctl is-active $SVC && echo "  ✅ active" || echo "  ❌ 未启动"
echo
echo "--- 最近日志 ---"
journalctl -u $SVC -n 20 --no-pager -o cat
