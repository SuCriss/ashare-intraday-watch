# -*- coding: utf-8 -*-
"""市场环境：决定「今天该不该推、推几只」。

三层判断：
  1. 指数趋势   上证/创业板 是否在 20 日线上
  2. 市场宽度   涨跌家数比、涨停跌停比
  3. 综合状态   risk_on / neutral / risk_off → 影响推送门槛和数量

依据：单边下跌日里，所有技术形态信号的历史胜率都会显著下降。
与其在熊市里硬推，不如降低频率。
"""
import json
import time
import urllib.request
from datetime import date

import config

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}

INDICES = {
    "1.000001": "上证指数",
    "0.399001": "深证成指",
    "0.399006": "创业板指",
    "1.000688": "科创50",
}
SH = "1.000001"       # 用上证判断趋势
CYB = "0.399006"      # 创业板判断小盘情绪


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get_retry(url, timeout=12, retries=3):
    """海外服务器上 push2/push2his 会间歇 timeout 或 unreachable。"""
    last = None
    for i in range(retries):
        try:
            return _get(url, timeout)
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(0.8)
    raise last


class MarketContext:
    def __init__(self):
        self.idx: dict[str, dict] = {}      # code -> {name, price, pct, amount}
        self.ma20: dict[str, float] = {}    # code -> MA20
        self._last_quote = 0.0
        self._last_ma = 0.0
        self._ma_date = None
        self._stale = False

    # ---------- 数据 ----------
    def refresh(self, force=False):
        now = time.time()
        if force or now - self._last_quote > 120:
            self._fetch_quote()
            self._last_quote = now
        # MA20 每天算一次就够
        if force or self._ma_date != date.today().isoformat() or not self.ma20:
            self._fetch_ma20()
            self._ma_date = date.today().isoformat()
            self._last_ma = now

    def _fetch_quote(self):
        secids = ",".join(INDICES)
        url = (f"https://{config.EM_HOST}/api/qt/ulist.np/get?fltt=2&invt=2"
               f"&secids={secids}&fields=f12,f14,f2,f3,f6")
        try:
            d = _get_retry(url, timeout=10, retries=3)
            for r in (d.get("data") or {}).get("diff") or []:
                code = str(r.get("f12"))
                key = next((k for k in INDICES if k.endswith(code)), None)
                if not key:
                    continue
                self.idx[key] = {
                    "name": INDICES[key],
                    "price": float(r.get("f2") or 0),
                    "pct": float(r.get("f3") or 0),
                    "amount": float(r.get("f6") or 0),
                }
            self._stale = False
        except Exception as e:
            # 保留上一轮的值，不清空——宁可数据旧一点，也不要整个环境层失效
            self._stale = bool(self.idx)
            print(f"[环境] 指数行情失败（{'沿用上轮' if self.idx else '无数据'}）：{type(e).__name__}")

    def _fetch_ma20(self):
        for secid in (SH, CYB):
            url = (f"https://{config.EM_HIS_HOST}/api/qt/stock/kline/get"
                   f"?secid={secid}&klt=101&fqt=1&beg=0&end=20500101&lmt=30"
                   "&fields1=f1&fields2=f51,f53")
            try:
                d = _get_retry(url, timeout=10, retries=3)
                kl = (d.get("data") or {}).get("klines") or []
                closes = [float(k.split(",")[1]) for k in kl][-20:]
                if len(closes) >= 20:
                    self.ma20[secid] = sum(closes) / 20
            except Exception as e:
                print(f"[环境] {INDICES.get(secid)} MA20 失败"
                      f"（{'沿用上轮' if secid in self.ma20 else '无数据'}）：{type(e).__name__}")

    # ---------- 判断 ----------
    def regime(self, breadth: dict) -> dict:
        """返回 {state, label, factor, reasons}

        factor 用于调整推送数量：1.0 正常 / 0.6 减半 / 0.0 暂停
        """
        reasons = []
        score = 0

        # 1) 指数趋势
        for secid, tag in ((SH, "上证"), (CYB, "创业板")):
            q = self.idx.get(secid)
            ma = self.ma20.get(secid)
            if not q or not ma:
                continue
            if q["price"] >= ma:
                score += 1
                reasons.append(f"{tag}站上MA20")
            else:
                score -= 1
                reasons.append(f"{tag}跌破MA20")

        # 2) 市场宽度
        up, down = breadth.get("up", 0), breadth.get("down", 0)
        zt, dt_ = breadth.get("limit_up", 0), breadth.get("limit_down", 0)
        if up + down > 500:
            ratio = up / max(1, down)
            if ratio >= 1.5:
                score += 1
                reasons.append(f"涨跌比{ratio:.1f}偏多")
            elif ratio <= 0.67:
                score -= 1
                reasons.append(f"涨跌比{ratio:.1f}偏空")
        if dt_ > zt:
            score -= 2
            reasons.append(f"跌停{dt_}>涨停{zt}")
        elif zt >= 30:
            score += 1
            reasons.append(f"涨停{zt}家")

        # 3) 定档
        if score >= 2:
            state, label, factor = "risk_on", "进攻", 1.0
        elif score <= -2:
            state, label, factor = "risk_off", "防守", 0.0
        elif score <= -1:
            state, label, factor = "weak", "谨慎", 0.5
        else:
            state, label, factor = "neutral", "中性", 1.0

        return {"state": state, "label": label, "factor": factor,
                "score": score, "reasons": reasons}

    def summary(self) -> str:
        parts = []
        for secid, name in INDICES.items():
            q = self.idx.get(secid)
            if q:
                parts.append(f"{name}{q['pct']:+.2f}%")
        return "　".join(parts)
