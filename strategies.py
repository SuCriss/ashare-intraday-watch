# -*- coding: utf-8 -*-
"""策略层：只保留经过 6 年日线大样本 + 样本外 + Bonferroni 多重检验校正的信号。

验证框架见 research.py。样本 967 只 / 1657 个交易日 / 2020-2026，扣 0.15% 双边成本。
Bonferroni 门槛 t ≥ 2.94（15 个假设），另要求样本外 t ≥ 2 且两边 CI 下界 > 0。

通过验证的信号（按次日开盘买 T+1 超额排序）：

  信号           全样本超额   t      样本外超额  t      采用执行方式
  涨停次日        +0.69%   9.74    +0.59%   6.15   次日开盘买
  跌停反弹        +0.69%   4.44    +0.82%   5.68   次日开盘买
  放量十字星      +0.34%   5.65    +0.34%   3.86   次日开盘买
  连跌首阳        +0.28%   5.09    +0.36%   5.34   次日开盘买
  回踩反包        +0.22%   4.13    +0.27%   3.69   次日开盘买
  低开高走        +0.22%   4.93    +0.27%   4.12   次日开盘买
  跳空不回落      +0.15%   6.67    +0.16%   4.75   尾盘买(+0.45% t=15.4)
  站上MA20        +0.10%   6.99    +0.10%   4.72   次日开盘买
  平台突破        +0.03%   1.13    +0.04%   1.14   尾盘买(+0.23% t=7.97)
  三连阳          -0.03%  -2.31    -0.01%  -0.76   尾盘买(+0.16% t=9.70)

被淘汰（不准上线）：
  放量突破 -0.06% t=-0.75 / 长下影线 -0.02% t=-1.94 / 缩量整理 -0.09% t=-3.06
  新高回踩 +0.04% t=2.35(样本外转负) / 低开回补(仅样本外显著)

纪律：任何新信号必须先在 research.py 里通过双门槛 + Bonferroni，否则不准进这里。
"""
from dataclasses import dataclass, field

import config


@dataclass
class Hit:
    rule_id: str
    rule_name: str
    score: float
    entry: str = "next_open"     # next_open=次日开盘买 / close=当日尾盘买
    tags: list[str] = field(default_factory=list)
    reason: str = ""


REGISTRY = []

# rule_id → 中文名。存库和日志用 id，展示一律走这里，避免卡片上出现英文。
RULE_NAMES = {
    "limit_hold": "涨停次日",
    "limit_down_rebound": "跌停反弹",
    "high_vol_doji": "放量十字星",
    "first_up_after_drop": "连跌首阳",
    "pullback": "回踩反包",
    "low_open_up": "低开高走",
    "ma20_reclaim": "站上MA20",
    "jump_hold": "跳空不回落",
    "platform_break": "平台突破",
    "three_white": "三连阳",
}


def rule_name(rule_id: str) -> str:
    """英文 id → 中文名。未知 id 原样返回，不崩。"""
    if not rule_id:
        return ""
    return RULE_NAMES.get(rule_id, rule_id)


def strategy(fn):
    REGISTRY.append(fn)
    return fn


def vr_of(s):
    """量比。上游有就用，没有（新浪备源）返回 0，该票不参与信号。"""
    return s.vol_ratio if s.vol_ratio > 0 else 0.0


def limit_pct_of(code):
    return 20.0 if code.startswith(("300", "301", "688")) else 10.0


def amp(s):
    """当日振幅 %"""
    return (s.high - s.low) / s.low * 100 if s.low > 0 else 0.0


# ==================== 次日开盘买（7 条） ====================
@strategy
def limit_hold(s, hist, ctx):
    """涨停次日不破：昨日涨停，今日涨 0~5% 且未跌破昨收。

    验证：+0.69% t=9.74 / 样本外 +0.59% t=6.15（最强信号之一）
    """
    p = config.SIG_LIMIT_HOLD
    bars = hist.bars(s.code, 1)
    if not bars:
        return None
    yp = bars[-1][5]
    if yp < limit_pct_of(s.code) - 0.3:
        return None
    if not (0.0 <= s.pct <= p["max_pct"]):
        return None
    if s.price < s.preclose:
        return None

    score = 62.0
    score += min(15.0, (yp - limit_pct_of(s.code) + 10) * 2)
    score += min(12.0, s.pct * 1.5)
    score += min(8.0, s.day_position * 8)

    return Hit("limit_hold", "涨停次日", score, "next_open",
               ["昨日涨停", f"昨涨{yp:.1f}%"],
               f"昨涨停{yp:.1f}% 今{s.pct:+.1f}% 未破昨收")


@strategy
def limit_down_rebound(s, hist, ctx):
    """跌停反弹：昨日跌停，今日涨 > 2%。

    验证：+0.69% t=4.44 / 样本外 +0.82% t=5.68（样本外最强）
    """
    p = config.SIG_LIMIT_DOWN
    bars = hist.bars(s.code, 1)
    if not bars:
        return None
    yp = bars[-1][5]
    if yp > -(limit_pct_of(s.code) - 0.3):
        return None
    if s.pct < p["min_pct"]:
        return None

    score = 60.0
    score += min(14.0, abs(yp) - limit_pct_of(s.code) + 15)
    score += min(14.0, s.pct * 1.2)
    score += min(8.0, s.day_position * 8)

    return Hit("limit_down_rebound", "跌停反弹", score, "next_open",
               ["昨日跌停", f"昨跌{yp:.1f}%"],
               f"昨跌停{yp:.1f}% 今{s.pct:+.1f}%")


@strategy
def high_vol_doji(s, hist, ctx):
    """放量十字星：振幅 > 5%，实体 < 振幅 20%，量比 > 2。

    验证：+0.34% t=5.65 / 样本外 +0.34% t=3.86
    """
    p = config.SIG_DOJI
    a = amp(s)
    if a < p["min_amp"]:
        return None
    rng = s.high - s.low
    if rng <= 0 or abs(s.price - s.open) / rng > p["max_body"]:
        return None
    vr = vr_of(s)
    if vr < p["min_vol_ratio"]:
        return None

    score = 58.0
    score += min(14.0, (a - p["min_amp"]) * 2)
    score += min(12.0, (vr - p["min_vol_ratio"]) * 3)
    score += min(10.0, s.day_position * 10)

    return Hit("high_vol_doji", "放量十字星", score, "next_open",
               ["多空激烈", f"振幅{a:.1f}%"],
               f"振幅{a:.1f}% 实体极小 量比{vr:.1f}")


@strategy
def first_up_after_drop(s, hist, ctx):
    """连跌首阳：前 3 日累计跌 > 6%，今日涨 > 2%。

    验证：+0.28% t=5.09 / 样本外 +0.36% t=5.34
    """
    p = config.SIG_FIRST_UP
    bars = hist.bars(s.code, 3)
    if not bars or len(bars) < 3:
        return None
    base = bars[0][4] if len(bars) == 3 else bars[-3][4]
    # 前 3 日 = bars[-3] 收盘 → bars[-1] 收盘
    base = bars[-3][4]
    if base <= 0:
        return None
    cum = (bars[-1][4] / base - 1) * 100
    if cum > p["max_cum"]:
        return None
    if s.pct < p["min_pct"]:
        return None

    score = 57.0
    score += min(16.0, abs(cum) * 1.5)
    score += min(14.0, s.pct * 1.2)
    score += min(8.0, s.day_position * 8)

    return Hit("first_up_after_drop", "连跌首阳", score, "next_open",
               ["超跌反弹", f"前3日{cum:.1f}%"],
               f"前3日{cum:.1f}% → 今{s.pct:+.1f}%")


@strategy
def pullback(s, hist, ctx):
    """回踩反包：日内最低 ≤ -2%，现价 ≥ +2%，贴日内高位，量比 ≥1.5。

    验证：+0.22% t=4.13 / 样本外 +0.27% t=3.69
    注意：尾盘买是 -0.23%（隔夜平均低开），必须次日开盘买。
    """
    p = config.SIG_PULLBACK
    if s.preclose <= 0:
        return None
    dip = (s.low / s.preclose - 1) * 100
    if dip > p["dip_below"]:
        return None
    if s.pct < p["recover_above"]:
        return None
    vr = vr_of(s)
    if vr < p["min_vol_ratio"]:
        return None
    if s.day_position < p["min_day_pos"]:
        return None

    score = 56.0
    score += min(14.0, abs(dip) * 2.5)
    score += min(14.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)

    return Hit("pullback", "回踩反包", score, "next_open",
               ["洗盘反包"], f"最低{dip:.1f}% → 现{s.pct:+.1f}% 量比{vr:.1f}")


@strategy
def low_open_up(s, hist, ctx):
    """低开高走：今开跌 < -1%，现价涨 > 1%，量比 ≥1.5。

    验证：+0.22% t=4.93 / 样本外 +0.27% t=4.12
    注意：尾盘买是 -0.37%，必须次日开盘买。
    """
    p = config.SIG_LOW_OPEN
    if s.preclose <= 0:
        return None
    open_pct = (s.open / s.preclose - 1) * 100
    if open_pct > p["max_open_pct"]:
        return None
    if s.pct < p["min_pct"]:
        return None
    vr = vr_of(s)
    if vr < p["min_vol_ratio"]:
        return None

    score = 54.0
    score += min(16.0, abs(open_pct) * 3)
    score += min(14.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)
    score += min(8.0, (vr - p["min_vol_ratio"]) * 3)

    return Hit("low_open_up", "低开高走", score, "next_open",
               [f"低开{open_pct:.1f}%拉起"],
               f"开盘{open_pct:.1f}% → 现{s.pct:+.1f}% 量比{vr:.1f}")


@strategy
def ma20_reclaim(s, hist, ctx):
    """站上 20 日线：昨收在下方，现价站上，量比 ≥1.2。

    验证：+0.10% t=6.99 / 样本外 +0.10% t=4.72（超额小但极其稳定）
    """
    p = config.SIG_MA20
    ma = hist.ma(s.code, 20)
    bars = hist.bars(s.code, 1)
    if ma is None or not bars:
        return None
    prev_close = bars[-1][4]
    if prev_close >= ma or s.price < ma:
        return None
    vr = vr_of(s)
    if vr < p["min_vol_ratio"]:
        return None

    score = 50.0
    score += min(12.0, (s.price / ma - 1) * 100 * 3)
    score += min(10.0, (vr - p["min_vol_ratio"]) * 4)
    score += min(8.0, s.day_position * 8)

    return Hit("ma20_reclaim", "站上MA20", score, "next_open",
               ["收复20日线"], f"昨收{prev_close:.2f} < MA20 {ma:.2f} ≤ 现价{s.price:.2f}")


# ==================== 尾盘买（3 条） ====================
@strategy
def jump_hold(s, hist, ctx):
    """跳空不回落：今开涨 >1%，现价仍在昨收上方，涨幅 >1%，量比 ≥1.2。

    验证：次日开盘买 +0.15% t=6.67；**尾盘买 +0.45% t=15.42**（最强组合）
    原因：这类票隔夜继续高开，尾盘买能多吃一段。
    """
    p = config.SIG_JUMP
    if s.preclose <= 0:
        return None
    open_pct = (s.open / s.preclose - 1) * 100
    if open_pct < p["min_open_pct"]:
        return None
    if s.pct < p["min_pct"]:
        return None
    if s.price < s.preclose:
        return None
    vr = vr_of(s)
    if vr < p["min_vol_ratio"]:
        return None

    score = 58.0
    score += min(14.0, (open_pct - p["min_open_pct"]) * 5)
    score += min(12.0, s.pct * 1.2)
    score += min(10.0, (vr - p["min_vol_ratio"]) * 4)
    score += min(8.0, s.day_position * 8)

    return Hit("jump_hold", "跳空不回落", score, "close",
               ["缺口未补", f"高开{open_pct:.1f}%"],
               f"高开{open_pct:.1f}%未回补 现{s.pct:+.1f}% 量比{vr:.1f}")


@strategy
def platform_break(s, hist, ctx):
    """平台突破：现价创 20 日新高，温和放量 1.5~3。

    验证：尾盘买 +0.23% t=7.97 / 样本外 +0.42% t=9.27
    （次日开盘买只有 +0.03% t=1.13，不显著 → 必须尾盘买）
    """
    p = config.SIG_PLATFORM
    h20 = hist.high_n(s.code, 20)
    if h20 is None or s.price <= h20:
        return None
    vr = vr_of(s)
    if not (p["min_vr"] <= vr <= p["max_vr"]):
        return None

    score = 55.0
    score += min(15.0, (s.price / h20 - 1) * 100 * 5)
    score += min(10.0, s.day_position * 10)
    score += min(8.0, (vr - p["min_vr"]) * 3)

    return Hit("platform_break", "平台突破", score, "close",
               ["20日新高"], f"突破20日高点{h20:.2f} 量比{vr:.1f}")


@strategy
def three_white(s, hist, ctx):
    """三连阳：前两日收阳，今日继续收阳且涨幅大于昨日。

    验证：尾盘买 +0.16% t=9.70 / 样本外 +0.39% t=15.57
    （次日开盘买 -0.03%，不显著 → 必须尾盘买）
    """
    bars = hist.bars(s.code, 3)
    if not bars or len(bars) < 3:
        return None
    b1, b2 = bars[-2], bars[-1]
    if b1[4] <= b1[1] or b2[4] <= b2[1]:      # 前两日必须收阳
        return None
    if s.price <= s.open:                      # 今日也收阳
        return None
    if s.pct <= b2[5] or b2[5] <= 0:           # 涨幅递增
        return None

    score = 52.0
    score += min(12.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)

    return Hit("three_white", "三连阳", score, "close",
               ["连阳加速"], f"昨{b2[5]:+.1f}% → 今{s.pct:+.1f}%")


# ---------------- 信号族（来自 research.py --corr 的相关性检验） ----------------
# 相关性矩阵显示 10 条信号不是 10 个独立因子，有 2 族明显相关：
#   日内反转族  回踩反包 ↔ 低开高走 r=0.43（低开回补 r=0.55，已淘汰）
#   超跌反弹族  连跌首阳 ↔ 跌停反弹 r=0.51
# 同族信号同时命中同一只票，本质是同一个信号被数了两次，不能算「共振」。
FAMILY = {
    "pullback": "reversal",
    "low_open_up": "reversal",
    "jump_hold": "gap",
    "limit_hold": "momentum",
    "limit_down_rebound": "oversold",
    "first_up_after_drop": "oversold",
    "high_vol_doji": "volatility",
    "ma20_reclaim": "trend",
    "platform_break": "breakout",
    "three_white": "breakout",
}


def sector_resonance(hits, snaps):
    cnt: dict[str, int] = {}
    for code, hit in hits:
        sec = snaps[code].sector
        cnt[sec] = cnt.get(sec, 0) + 1
    return cnt


def apply_resonance(scored, sector_cnt):
    for row in scored:
        # 只有「不同族」的信号同时命中才算共振；同族是重复计数
        fams = {FAMILY.get(h.rule_id, h.rule_id) for h in row["hits"]}
        row["fams"] = len(fams)
        if len(fams) > 1:
            row["score"] *= config.RESONANCE_BONUS
            row["tags"].append(f"{len(fams)}族共振")

        peers = sector_cnt.get(row["sector"], 1) - 1
        if peers >= 2:
            row["tags"].append(f"{row['sector']}×{peers + 1}")
        row["score"] = round(min(100.0, row["score"]), 1)
    return scored
