# -*- coding: utf-8 -*-
"""事件层：业绩预告。

数据源：东财 datacenter RPT_PUBLIC_OP_NEWPREDICT（实测可用）
  字段 PREDICT_TYPE（预增/扭亏/预减/首亏…）、ADD_AMP_LOWER/UPPER（变动幅度）
  2026Q3 全市场 534 条，一次拉完

规则：
  负面（预亏/首亏/续亏/预减/略减）→ 排除
  正面（预增/略增/扭亏/续盈）      → 加分，幅度越大越多
  无预告                            → 中性通过
"""
import json
import time
import urllib.request
from datetime import date, timedelta

import config

UA = {"User-Agent": "Mozilla/5.0",
      "Referer": "https://data.eastmoney.com/"}
BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"

NEGATIVE = {"预亏", "首亏", "续亏", "预减", "略减", "增亏"}
POSITIVE = {"预增", "略增", "扭亏", "续盈"}

_cache: dict[str, dict] = {}
_cache_date = None


def _recent_periods(n=2):
    """最近 n 个已披露的报告期（YYYY-MM-DD）。"""
    today = date.today()
    out = []
    for y in (today.year, today.year - 1):
        for md in ("12-31", "09-30", "06-30", "03-31"):
            d = date(y, int(md[:2]), int(md[3:]))
            if d < today:
                out.append(d.isoformat())
    return sorted(out, reverse=True)[:n]


def _fetch_period(period: str, page_size=500, max_pages=15) -> list[dict]:
    rows = []
    for pn in range(1, max_pages + 1):
        url = (f"{BASE}?reportName=RPT_PUBLIC_OP_NEWPREDICT&columns=ALL"
               f"&pageSize={page_size}&pageNumber={pn}"
               "&sortColumns=NOTICE_DATE&sortTypes=-1"
               f"&filter=(REPORT_DATE%3D%27{period}%27)")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            res = d.get("result") or {}
            batch = res.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            if len(rows) >= (res.get("count") or 0):
                break
        except Exception as e:
            print(f"[事件] {period} 第 {pn} 页失败：{type(e).__name__}")
            break
    return rows


def load(force=False) -> dict[str, dict]:
    """返回 {code: {type, state, amp_lower, amp_upper, date}}。按天缓存。"""
    global _cache, _cache_date
    today = date.today().isoformat()
    if not force and _cache_date == today and _cache:
        return _cache
    if not config.EVENT_ENABLED:
        return {}

    out = {}
    for period in _recent_periods(2):
        for r in _fetch_period(period):
            code = str(r.get("SECURITY_CODE") or "")
            if not code:
                continue
            # 同一只票取最新一期
            if code in out:
                continue
            out[code] = {
                "type": str(r.get("PREDICT_TYPE") or ""),
                "state": str(r.get("FORECAST_STATE") or ""),
                "amp_lower": float(r.get("ADD_AMP_LOWER") or 0),
                "amp_upper": float(r.get("ADD_AMP_UPPER") or 0),
                "date": str(r.get("NOTICE_DATE") or "")[:10],
                "period": period,
            }
    _cache = out
    _cache_date = today
    print(f"[事件] 业绩预告已加载 {len(out)} 只（{_recent_periods(2)}）")
    return out


def check(s, fc: dict) -> tuple[bool, str]:
    """负面预告排除。"""
    if not config.EVENT_ENABLED:
        return True, ""
    rec = fc.get(s.code)
    if not rec:
        return True, ""
    t = rec["type"]
    if t in NEGATIVE:
        return False, f"业绩{t}"
    return True, ""


def bonus(s, fc: dict) -> tuple[float, str]:
    """正面预告加分。"""
    if not config.EVENT_ENABLED:
        return 0.0, ""
    rec = fc.get(s.code)
    if not rec:
        return 0.0, ""
    p = config.EVENT
    t = rec["type"]
    if t not in POSITIVE:
        return 0.0, ""

    amp = max(abs(rec["amp_lower"]), abs(rec["amp_upper"]))
    b = p["base_bonus"]
    if amp >= p["big_amp"]:
        b += p["big_bonus"]
    tag = f"业绩{t}" + (f"{rec['amp_lower']:+.0f}%" if rec["amp_lower"] else "")
    return b, tag


def apply(snaps: dict, fc: dict) -> tuple[dict, dict]:
    out, rejected = {}, {}
    for code, s in snaps.items():
        ok, why = check(s, fc)
        if ok:
            out[code] = s
        else:
            rejected[why] = rejected.get(why, 0) + 1
    return out, rejected
