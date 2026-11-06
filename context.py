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

# 腾讯备源代码（东财 push2his 从海外间歇不可达）
TENCENT_IDX = {
    "1.000001": "sh000001",   # 上证
    "0.399001": "sz399001",   # 深成
    "0.399006": "sz399006",   # 创业板
    "1.000688": "sh000688",   # 科创50
}


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
        if self._fetch_quote_em():
            self._stale = False
            return
        # 东财挂了 → 腾讯兜底
        if self._fetch_quote_tencent():
            self._stale = False
            return
        self._stale = bool(self.idx)
        print(f"[环境] 指数行情失败（{'沿用上轮' if self.idx else '无数据'})")

    def _fetch_quote_em(self) -> bool:
        secids = ",".join(INDICES)
        url = (f"https://{config.EM_HOST}/api/qt/ulist.np/get?fltt=2&invt=2"
               f"&secids={secids}&fields=f12,f14,f2,f3,f6")
        try:
            d = _get_retry(url, timeout=8, retries=1)
            rows = (d.get("data") or {}).get("diff") or []
            if not rows:
                return False
            for r in rows:
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
            return bool(self.idx)
        except Exception:
            return False

    def _fetch_quote_tencent(self) -> bool:
        """腾讯指数行情备源。字段布局与个股一致：[3]现价 [4]昨收 [32]涨跌幅 [37]成交额(万)"""
        codes = ",".join(f"{v}" for v in TENCENT_IDX.values())
        url = f"http://qt.gtimg.cn/q={codes}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("gbk", "replace")
            got = 0
            for line in body.split("\n"):
                if "v_" not in line or '"' not in line:
                    continue
                f = line.split('"')[1].split("~")
                if len(f) < 40:
                    continue
                key = next((k for k, v in TENCENT_IDX.items()
                            if v.endswith(f[2])), None)
                if not key:
                    continue
                self.idx[key] = {
                    "name": INDICES[key],
                    "price": float(f[3] or 0),
                    "pct": float(f[32] or 0),
                    "amount": float(f[37] or 0) * 1e4,
                }
                got += 1
            return got > 0
        except Exception:
            return False

    def _fetch_ma20(self):
        for secid in (SH, CYB):
            ma = self._ma20_eastmoney(secid)
            if ma is None:
                ma = self._ma20_tencent(secid)   # 东财 push2his 从海外间歇不可达
            if ma is not None:
                self.ma20[secid] = ma
            else:
                print(f"[环境] {INDICES.get(secid)} MA20 失败"
                      f"（{'沿用上轮' if secid in self.ma20 else '无数据'}）")

    def _ma20_eastmoney(self, secid):
        url = (f"https://{config.EM_HIS_HOST}/api/qt/stock/kline/get"
               f"?secid={secid}&klt=101&fqt=1&beg=0&end=20500101&lmt=30"
               "&fields1=f1&fields2=f51,f53")
        try:
            d = _get_retry(url, timeout=8, retries=2)
            kl = (d.get("data") or {}).get("klines") or []
            closes = [float(k.split(",")[1]) for k in kl][-20:]
            return sum(closes) / 20 if len(closes) >= 20 else None
        except Exception:
            return None

    def _ma20_tencent(self, secid):
        """腾讯日 K 备源。实测与东财 MA20 完全一致（上证 3701.53）。"""
        code = TENCENT_IDX.get(secid)
        if not code:
            return None
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={code},day,,,30,qfq")
        try:
            d = _get_retry(url, timeout=8, retries=2)
            data = (d.get("data") or {}).get(code) or {}
            kl = data.get("day") or data.get("qfqday") or []
            closes = [float(x[2]) for x in kl][-20:]
            return sum(closes) / 20 if len(closes) >= 20 else None
        except Exception:
            return None

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
        # 注意：两个指数都跌破 MA20 只有 -2 分，不应该直接停推——
        # 跌破 0.3% 和跌破 5% 是两回事。停推需要宽度也确认（score ≤ -3）。
        p = config.REGIME
        if score >= 2:
            state, label = "risk_on", "进攻"
            factor = p["risk_on_factor"]
        elif score <= -3:
            state, label = "risk_off", "防守"
            factor = p["risk_off_factor"]
        elif score <= -2:
            state, label = "weak", "谨慎"
            factor = p["weak_factor"]
        else:
            state, label = "neutral", "中性"
            factor = p["neutral_factor"]

        return {"state": state, "label": label, "factor": factor,
                "score": score, "reasons": reasons}

    def summary(self) -> str:
        parts = []
        for secid, name in INDICES.items():
            q = self.idx.get(secid)
            if q:
                parts.append(f"{name}{q['pct']:+.2f}%")
        return "　".join(parts)
