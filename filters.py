# -*- coding: utf-8 -*-
"""三层筛选：基本面硬过滤 → 资金面确认 → 组合约束。

设计原则：
  · 硬过滤（排除器）比加分项更有价值——先排掉明显不该碰的，再谈选优
  · 每条规则都要能说清「为什么排除」，并且能统计排掉了多少
  · 金融股（银行/保险/证券）的负债率天然很高，豁免负债率检查

研究依据：
  回测只能证明「形态有微弱优势」，不能证明「这家公司值得买」。
  基本面过滤的作用是**降低踩雷概率**，不是提高收益——
  一个形态再好但连续亏损的公司，不该出现在推荐里。
"""
import config


# ---------------- 基本面 ----------------
FINANCIAL_SECTORS = ("银行", "保险", "证券", "多元金融", "信托")


def is_financial(s) -> bool:
    return any(k in s.sector for k in FINANCIAL_SECTORS)


def fundamental_rules():
    """返回 [(名称, 判定函数, 是否适用函数)]"""
    p = config.FUND
    return [
        ("ST/退市",
         lambda s: not s.is_st,
         lambda s: True),
        ("次新股",
         lambda s: s.listed_days >= p["min_listed_days"],
         lambda s: True),
        ("亏损",
         lambda s: (s.pe_ttm > 0 if s.pe_ttm else s.pe > 0),
         lambda s: True),
        ("估值过高",
         lambda s: 0 < (s.pe_ttm or s.pe) <= p["max_pe"],
         lambda s: not is_financial(s)),
        ("业绩崩塌",
         lambda s: s.rev_yoy > p["min_rev_yoy"],
         lambda s: s.rev_yoy != 0),
        ("负债过高",
         lambda s: s.debt_ratio <= p["max_debt_ratio"],
         lambda s: s.debt_ratio > 0 and not is_financial(s)),
    ]


def fundamental_check(s):
    """返回 (是否通过, 不通过的原因)。"""
    # 没有财务数据（新浪备源）→ 无法判断，按「不通过」处理：
    # 宁可漏掉，也不把一只财务未知的票推给用户
    if not s.has_fund:
        return False, "无财务数据"
    for name, ok_fn, apply_fn in fundamental_rules():
        try:
            if not apply_fn(s):
                continue
            if not ok_fn(s):
                return False, name
        except Exception:
            continue
    return True, ""


def apply_fundamental(snaps: dict) -> tuple[dict, dict]:
    """返回 (通过的快照, 各规则排除计数)"""
    if not config.FUND_ENABLED:
        return snaps, {}
    out, rejected = {}, {}
    for code, s in snaps.items():
        ok, why = fundamental_check(s)
        if ok:
            out[code] = s
        else:
            rejected[why] = rejected.get(why, 0) + 1
    return out, rejected


# ---------------- 资金面 ----------------
def capital_check(s) -> tuple[bool, str]:
    """当日资金面：大额净流出直接排除，净流入给加分。"""
    p = config.CAP
    if not config.CAP_ENABLED:
        return True, ""
    # 大额净流出（超过阈值且占比为负）→ 排除
    if s.mf_net < -p["max_outflow"] and s.mf_pct < 0:
        return False, "主力大额流出"
    return True, ""


def apply_capital(snaps: dict) -> tuple[dict, dict]:
    out, rejected = {}, {}
    for code, s in snaps.items():
        ok, why = capital_check(s)
        if ok:
            out[code] = s
        else:
            rejected[why] = rejected.get(why, 0) + 1
    return out, rejected


def capital_bonus(s) -> tuple[float, str]:
    """资金面加分：主力净占比越高越好。"""
    p = config.CAP
    if not config.CAP_ENABLED:
        return 0.0, ""
    if s.mf_pct >= p["strong_pct"]:
        return p["bonus"], f"主力占比{s.mf_pct:.0f}%"
    if s.mf_pct >= p["ok_pct"]:
        return p["bonus"] * 0.5, f"主力占比{s.mf_pct:.0f}%"
    return 0.0, ""


# ---------------- 组合约束 ----------------
def apply_combination(rows: list[dict], max_per_sector=None) -> list[dict]:
    """同板块最多 N 只，避免一次推 5 只半导体。rows 需已按分数降序。"""
    cap = max_per_sector or config.COMB["max_per_sector"]
    out, per_sector = [], {}
    for r in rows:
        sec = r.get("sector") or "未分类"
        if per_sector.get(sec, 0) >= cap:
            continue
        per_sector[sec] = per_sector.get(sec, 0) + 1
        out.append(r)
    return out


def diagnose(snaps: dict) -> str:
    """一行诊断：基本面/资金面各排掉了多少。"""
    _, rf = apply_fundamental(snaps)
    _, rc = apply_capital(snaps)
    parts = []
    if rf:
        parts.append("基本面排除 " + "，".join(f"{k}{v}" for k, v in
                                              sorted(rf.items(), key=lambda x: -x[1])))
    if rc:
        parts.append("资金面排除 " + "，".join(f"{k}{v}" for k, v in rc.items()))
    return "；".join(parts) if parts else "无排除"
