# -*- coding: utf-8 -*-
"""策略层：把「优质股」翻译成可计算的条件。

每条策略签名统一：(snap, hist, ctx) -> Hit | None
  snap  当前快照
  hist  该股的价格环形缓冲（用于算 3 分钟涨速）
  ctx   全市场上下文（板块统计等）

新增策略只要写一个函数 + @strategy 装饰器，run.py 自动纳入。
"""
from dataclasses import dataclass, field

import config


@dataclass
class Hit:
    rule_id: str
    rule_name: str
    score: float
    tags: list[str] = field(default_factory=list)
    reason: str = ""


REGISTRY = []


def strategy(fn):
    REGISTRY.append(fn)
    return fn


# ---------------- 策略 1：放量突破 ----------------
@strategy
def s1_breakout(s, hist, ctx):
    """日内高位 + 放量 + 主力净流入。不追涨停，留空间。"""
    p = config.S1
    if not (p["min_pct"] <= s.pct <= p["max_pct"]):
        return None
    if s.vol_ratio < p["min_vol_ratio"]:
        return None
    if not (p["min_turnover"] <= s.turnover <= p["max_turnover"]):
        return None
    if s.high <= 0 or s.price < s.high * p["near_high"]:
        return None
    if s.mf_net <= 0:
        return None

    score = 60.0
    score += min(15.0, (s.vol_ratio - p["min_vol_ratio"]) * 5)
    score += min(10.0, s.day_position * 10)
    score += min(10.0, s.mf_pct / 3)
    score += min(5.0, s.pct / p["max_pct"] * 5)

    return Hit(
        rule_id="breakout",
        rule_name="放量突破",
        score=score,
        tags=["放量", "日内高位"],
        reason=f"量比{s.vol_ratio:.1f} 换手{s.turnover:.1f}% 贴日高{s.price:.2f}",
    )


# ---------------- 策略 2：主力抢筹 ----------------
@strategy
def s2_capital(s, hist, ctx):
    """真金白银：主力净流入绝对额 + 占比双高。"""
    p = config.S2
    if s.mf_net < p["min_mf_net"] or s.mf_pct < p["min_mf_pct"]:
        return None
    if not (p["min_pct"] <= s.pct <= p["max_pct"]):
        return None
    if s.vol_ratio < p["min_vol_ratio"]:
        return None
    if s.float_cap < p["min_float_cap"]:
        return None

    score = 58.0
    score += min(20.0, s.mf_net / 5e8 * 20)
    score += min(15.0, (s.mf_pct - p["min_mf_pct"]) * 0.75)
    score += min(7.0, s.pct)

    return Hit(
        rule_id="capital",
        rule_name="主力抢筹",
        score=score,
        tags=["主力", f"净占{s.mf_pct:.0f}%"],
        reason=f"主力净流入{s.mf_net/1e8:.2f}亿 占成交{s.mf_pct:.1f}%",
    )


# ---------------- 策略 3：急拉启动 ----------------
@strategy
def s3_surge(s, hist, ctx):
    """3 分钟涨速，第一时间发现启动，不等涨完。"""
    p = config.S3
    if s.pct > p["max_pct"]:
        return None
    if s.vol_ratio < p["min_vol_ratio"]:
        return None

    speed = hist.speed(s.code, p["window_seconds"])
    # 环形缓冲样本不足时，退回东财自带的涨速字段
    if speed is None:
        speed = s.speed
    if speed is None or speed < p["min_speed"]:
        return None

    score = 55.0
    score += min(25.0, (speed - p["min_speed"]) * 12)
    score += min(12.0, (s.vol_ratio - p["min_vol_ratio"]) * 4)
    score += min(8.0, s.day_position * 8)

    return Hit(
        rule_id="surge",
        rule_name="急拉启动",
        score=score,
        tags=[f"{p['window_seconds']//60}分钟急拉"],
        reason=f"{p['window_seconds']//60}分钟涨{speed:.2f}% 量比{s.vol_ratio:.1f}",
    )


# ---------------- 策略 4：回踩反包 ----------------
@strategy
def s4_pullback(s, hist, ctx):
    """日内先杀后拉，洗盘结束的形态。必须拉到日内高位才算反包成功。"""
    p = config.S4
    dip = (s.low / s.preclose - 1) * 100 if s.preclose else 0
    if dip > p["dip_below"]:
        return None                      # 没有回踩过，不算
    if s.pct < p["recover_above"]:
        return None                      # 没拉回来
    if s.vol_ratio < p["min_vol_ratio"]:
        return None
    if s.mf_net < p["min_mf_net"]:
        return None                      # 资金没真转正
    if s.day_position < p["min_day_pos"]:
        return None                      # 拉回但没到高位，属于弱反抽

    score = 42.0
    score += min(12.0, abs(dip) * 2.5)
    score += min(12.0, s.pct * 1.2)
    score += min(10.0, s.day_position * 10)

    return Hit(
        rule_id="pullback",
        rule_name="回踩反包",
        score=score,
        tags=["洗盘反包"],
        reason=f"最低{dip:.1f}% → 现{s.pct:+.1f}% 资金转正{s.mf_net/1e8:.2f}亿",
    )


# ---------------- 板块共振（后处理，不是独立选股） ----------------
def sector_resonance(hits: list[tuple], snaps: dict) -> dict[str, int]:
    """统计每个板块命中了几只，返回 {sector: 命中数}。"""
    cnt: dict[str, int] = {}
    for code, hit in hits:
        sec = snaps[code].sector
        cnt[sec] = cnt.get(sec, 0) + 1
    return cnt


def apply_resonance(scored: list[dict], sector_cnt: dict[str, int]) -> list[dict]:
    """多策略共振 + 板块共振 双重加成。"""
    for row in scored:
        # 多策略同时命中
        if len(row["hits"]) > 1:
            row["score"] *= config.RESONANCE_BONUS
            row["tags"].append(f"{len(row['hits'])}策略共振")

        # 板块共振：同板块 >= N 只命中
        peers = sector_cnt.get(row["sector"], 1) - 1
        if peers >= config.S5["min_peers"]:
            row["score"] += config.S5["bonus"]
            row["tags"].append(f"{row['sector']}×{peers + 1}")

        row["score"] = round(min(100.0, row["score"]), 1)
    return scored
