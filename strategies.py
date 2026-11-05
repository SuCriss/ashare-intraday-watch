# -*- coding: utf-8 -*-
"""策略层：只保留经过 6 年日线大样本 + 样本外 + 参数稳健性三重验证的信号。

验证框架见 research.py。结论（2020-2026，294 只，1657 个交易日）：

  信号              次日开盘买(T+1)        当日尾盘买(T+1)
  跳空不回落        +0.19% t=4.28 ✅      +0.43% t=7.93 ✅
  回踩反包          +0.29% t=2.71 ✅      -0.20% t=-1.70 ❌
  低开高走          +0.21% t=2.63 ✅      -0.42% t=-4.61 ❌
  放量突破          -0.03% t=-0.19 ❌     —              已删除
  主力抢筹          -0.63% t=-1.45 ❌     —              已删除（样本也不足）
  急拉启动          —                     日线无法验证     已删除
  缩量回踩          -0.03% t=-2.08 ❌     —              已删除

关键结论：**回踩反包和低开高走必须次日开盘买，尾盘买是亏的**（这些票隔夜平均低开）；
**跳空不回落尾盘买反而更强**（隔夜继续高开）。所以每条信号都带 entry 字段，
推送时告诉用户该怎么执行。

新增信号只要写个函数 + @strategy，然后必须先用 research.py 验证过再上线。
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


def strategy(fn):
    REGISTRY.append(fn)
    return fn


def vol_ratio_of(s, hist=None):
    """量比。上游（东财 f10）有就直接用；没有（新浪备源）返回 0，该票不参与信号。"""
    return s.vol_ratio if s.vol_ratio > 0 else 0.0


# ---------------- 1. 跳空不回落（唯一双口径都有效） ----------------
@strategy
def jump_hold(s, hist, ctx):
    """高开且不回补缺口：今开涨 >1%，现价仍在昨收上方，涨幅 >1%。

    验证：次日开盘买 +0.19% t=4.28；尾盘买 +0.43% t=7.93（最强）。
    经济含义：隔夜有真实买盘，开盘缺口不补 = 承接强。
    """
    p = config.SIG_JUMP
    if s.preclose <= 0:
        return None
    open_pct = (s.open / s.preclose - 1) * 100
    if open_pct < p["min_open_pct"]:
        return None
    if s.pct < p["min_pct"]:
        return None
    if s.price < s.preclose:            # 回补了缺口，不算
        return None
    vr = vol_ratio_of(s, hist)
    if vr < p["min_vol_ratio"]:
        return None

    score = 58.0
    score += min(14.0, (open_pct - p["min_open_pct"]) * 5)
    score += min(12.0, s.pct * 1.2)
    score += min(10.0, (vr - p["min_vol_ratio"]) * 4)
    score += min(8.0, s.day_position * 8)

    return Hit(
        rule_id="jump_hold",
        rule_name="跳空不回落",
        score=score,
        entry="close",                  # 尾盘买更好
        tags=["缺口未补", f"高开{open_pct:.1f}%"],
        reason=f"高开{open_pct:.1f}%未回补 现{s.pct:+.1f}% 量比{vr:.1f}",
    )


# ---------------- 2. 回踩反包（必须次日开盘买） ----------------
@strategy
def pullback(s, hist, ctx):
    """日内先杀后拉：最低跌破 -2%，现价回到 +2% 以上且贴日内高位。

    验证：次日开盘买 +0.29% t=2.71（样本外 +0.48% t=3.74）；
          尾盘买 -0.20%（隔夜平均低开，尾盘接货等于接在缺口上）。
    """
    p = config.SIG_PULLBACK
    if s.preclose <= 0:
        return None
    dip = (s.low / s.preclose - 1) * 100
    if dip > p["dip_below"]:
        return None
    if s.pct < p["recover_above"]:
        return None
    vr = vol_ratio_of(s, hist)
    if vr < p["min_vol_ratio"]:
        return None
    if s.day_position < p["min_day_pos"]:
        return None

    score = 56.0
    score += min(14.0, abs(dip) * 2.5)
    score += min(14.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)

    return Hit(
        rule_id="pullback",
        rule_name="回踩反包",
        score=score,
        entry="next_open",
        tags=["洗盘反包"],
        reason=f"最低{dip:.1f}% → 现{s.pct:+.1f}% 量比{vr:.1f}",
    )


# ---------------- 3. 低开高走（必须次日开盘买） ----------------
@strategy
def low_open_up(s, hist, ctx):
    """低开后拉起：今开跌 < -1%，现价涨 > 1%，放量。

    验证：次日开盘买 +0.21% t=2.63（样本外 +0.37% t=3.17）；
          尾盘买 -0.42%（同样隔夜低开）。
    """
    p = config.SIG_LOW_OPEN
    if s.preclose <= 0:
        return None
    open_pct = (s.open / s.preclose - 1) * 100
    if open_pct > p["max_open_pct"]:
        return None
    if s.pct < p["min_pct"]:
        return None
    vr = vol_ratio_of(s, hist)
    if vr < p["min_vol_ratio"]:
        return None

    score = 54.0
    score += min(16.0, abs(open_pct) * 3)
    score += min(14.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)
    score += min(8.0, (vr - p["min_vol_ratio"]) * 3)

    return Hit(
        rule_id="low_open_up",
        rule_name="低开高走",
        score=score,
        entry="next_open",
        tags=[f"低开{open_pct:.1f}%拉起"],
        reason=f"开盘{open_pct:.1f}% → 现{s.pct:+.1f}% 量比{vr:.1f}",
    )


# ---------------- 板块共振（未经独立验证，只做展示不加分） ----------------
def sector_resonance(hits: list[tuple], snaps: dict) -> dict[str, int]:
    cnt: dict[str, int] = {}
    for code, hit in hits:
        sec = snaps[code].sector
        cnt[sec] = cnt.get(sec, 0) + 1
    return cnt


def apply_resonance(scored: list[dict], sector_cnt: dict[str, int]) -> list[dict]:
    """多策略共振仍加分（同一天多个信号命中同一只，逻辑上更可信）；
    板块共振只做标签展示——它没有通过独立验证，不给分数。"""
    for row in scored:
        if len(row["hits"]) > 1:
            row["score"] *= config.RESONANCE_BONUS
            row["tags"].append(f"{len(row['hits'])}策略共振")

        peers = sector_cnt.get(row["sector"], 1) - 1
        if peers >= 2:
            row["tags"].append(f"{row['sector']}×{peers + 1}")

        row["score"] = round(min(100.0, row["score"]), 1)
    return scored
