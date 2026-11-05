# -*- coding: utf-8 -*-
"""集中配置。改这里就能调策略，不用动代码。"""
import os

# ---------- 推送渠道 ----------
# 每个渠道一个 dict，type 必填。webhook/secret 从环境变量读，
# 留空的渠道会被自动跳过，不会报错。想加新渠道在 notify.py 写个类 + 这里加一行。
#
# 支持的 type：feishu / dingtalk / wecom / telegram / bark / webhook / console
CHANNELS = [
    # 飞书自定义机器人：群设置 → 群机器人 → 添加 → 自定义机器人
    {"type": "feishu",
     "webhook": os.getenv("FEISHU_WEBHOOK", ""),
     "secret": os.getenv("FEISHU_SECRET", "")},

    # 钉钉自定义机器人：群设置 → 智能群助手 → 添加机器人 → 自定义
    # 安全设置选「加签」就把密钥填 DINGTALK_SECRET，选「自定义关键词」则留空
    {"type": "dingtalk",
     "webhook": os.getenv("DINGTALK_WEBHOOK", ""),
     "secret": os.getenv("DINGTALK_SECRET", "")},

    # 企业微信群机器人：群设置 → 群机器人 → 添加
    {"type": "wecom", "webhook": os.getenv("WECOM_WEBHOOK", "")},

    # Telegram Bot：@BotFather 建 bot 拿 token，chat_id 用 @userinfobot 查
    {"type": "telegram",
     "token": os.getenv("TG_TOKEN", ""),
     "chat_id": os.getenv("TG_CHAT_ID", "")},

    # Bark（iOS）：App 里复制 URL，形如 https://api.day.app/xxxxxxxx
    {"type": "bark", "url": os.getenv("BARK_URL", "")},

    # 自定义 Webhook：POST 结构化 JSON，方便接自建服务 / n8n / 中转
    {"type": "webhook", "url": os.getenv("CUSTOM_WEBHOOK", "")},
]

# 是否 @所有人（仅飞书/钉钉支持，默认关，别刷屏）
FEISHU_AT_ALL = os.getenv("AT_ALL", "0") == "1"

# 兼容旧变量名
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")

# ---------- 数据源 host ----------
# 海外服务器上 push2.eastmoney.com 会变慢或间歇 502，push2delay 更稳（实测美国 VPS：
# delay 6.1s / push2 9.4s，字段完全一致，价格与新浪腾讯同步，不是延迟行情）。
# 国内本机用默认 push2 最快（1.5s）。用 EM_HOST 环境变量切换。
EM_HOST = os.getenv("EM_HOST", "push2.eastmoney.com")
EM_HIS_HOST = os.getenv("EM_HIS_HOST", "push2his.eastmoney.com")

# ---------- 采集 ----------
# 注意：上游会按请求量限流。实测连续高强度请求后，两通道延迟从 0.8s 劣化到 5s+。
# 因为策略只依赖上游自带的 f10(量比)/f22(涨速) 字段，本地环形缓冲只作兜底，
# 10s 采一轮完全够用（3 分钟涨速仍有 18 个采样点），请求量比 2s 降 5 倍。
# 海外服务器上采集本身要 5~7s，用 POLL_SECONDS=15 更合适。
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
FETCH_WORKERS = 45        # 分页并发。实测：30→849ms，45→527ms，60→1253ms(被限流)
PAGE_SIZE = 100           # 东财单页上限，改大无效
MAX_PAGES = 70            # 5927 只 / 100 ≈ 60 页，留余量
TOP_PAGES = 30            # 东财成交额 Top N 页（30 页=3000 只，成交额下限约 0.79 亿，盖住 1 亿门槛）
FETCH_TIMEOUT = 8         # 单通道超时（秒），超时后重试一次，仍失败则用上一轮兜底
MIN_ROWS_OK = 4000        # 东财返回行数低于此值判定链路异常，切新浪备源

# ---------- 止损 / 止盈 ----------
# 止损：backtest.py --optimize-stop 实测（200 只 / 30 日 / 465 信号）
#   -2.0% 平均收益 +0.62%，最差单笔 -2.00%，收益/尾部 0.312
#   不止损 平均收益 +0.72%，最差单笔 -16.40%，收益/尾部 0.044
#   → 只损失 0.10pp 期望，换来尾部风险削减 88%，比值提升 7.1 倍
#
# 止盈：backtest.py --optimize-take-profit 实测（457 信号）
#   纯持有(不设止盈) +0.45% | 当前 5%/2% +0.29% | 最优 10%/1% +0.62%
#   → 止盈的边际价值远小于止损。5%/2% 会砍掉上涨尾部，比纯持有还差 0.16pp。
#   取 10%/1.5%：+0.53%，比尖锐的 10%/1% 稳健（1% 回撤在实盘会被日内噪音反复扫）。
#   想更简单可以把 TAKE_PROFIT_PCT 设为 None，纯持有到次日收盘。
WATCH_ENABLED = True
STOP_LOSS_PCT = -2.0      # 相对信号价，跌破即告警（实测最优风险调整点）
TAKE_PROFIT_PCT = 10.0    # 涨到此幅度才进入移动止盈，避免砍掉大涨
TRAIL_DRAWDOWN = 1.5      # 移动止盈：从最高点回撤多少触发
NEXT_DAY_STOP_PCT = -5.0  # 次日相对信号价跌破此值，直接判「该走了」

# ---------- 推送节奏 ----------
PUSH_INTERVAL = 300       # 汇总推送间隔（秒）。飞书自定义机器人限 100 条/分钟、5 条/秒
TOP_N = 5                 # 每张卡片最多推几只
MIN_SCORE = 65            # 低于此分不推
COOLDOWN_SECONDS = 1800   # 同一只股票两次推送最小间隔
MAX_PUSH_PER_CODE = 2     # 同一只股票每日最多推几次

# ---------- 风控过滤（硬性） ----------
MIN_AMOUNT = 1.0e8        # 成交额下限 1 亿，流动性
MIN_FLOAT_CAP = 30e8      # 流通市值下限 30 亿
MAX_FLOAT_CAP = 3000e8    # 流通市值上限 3000 亿
MAX_TURNOVER = 25.0       # 换手率上限，超过算过热
MAX_PCT = 9.0             # 涨幅上限，接近涨停不追
MIN_PCT = -3.0            # 跌幅下限，不做逆势
EXCLUDE_BJ = True         # 排除北交所（流动性差）
EXCLUDE_ST = True         # 排除 ST / *ST / 退市

# ---------- 策略参数 ----------
# 1. 放量突破
S1 = dict(min_pct=3.0, max_pct=8.0, min_vol_ratio=2.5,
          min_turnover=3.0, max_turnover=15.0, near_high=0.995)

# 2. 主力抢筹
S2 = dict(min_mf_net=5.0e7, min_mf_pct=15.0, min_pct=0.0, max_pct=7.0,
          min_vol_ratio=1.5, min_float_cap=50e8)

# 3. 急拉启动
S3 = dict(window_seconds=180, min_speed=1.5, min_vol_ratio=2.0, max_pct=9.0)

# 4. 回踩反包
S4 = dict(dip_below=-2.0, recover_above=2.0, min_vol_ratio=1.5,
          min_mf_net=2.0e7, min_day_pos=0.75)

# 5. 板块共振：同板块命中数 >= N 时给板块内最强股加分
S5 = dict(min_peers=2, bonus=10.0)

# 共振加成：多策略同时命中
RESONANCE_BONUS = 1.2
