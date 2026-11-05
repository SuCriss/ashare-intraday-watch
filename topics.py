# -*- coding: utf-8 -*-
"""题材层：个股所属概念板块 + 板块当日强度。

数据源（实测可用）：
  板块列表  clist?fs=m:90+t:3  → 531 个概念板块，带当日涨幅、成分数、领涨股
  个股板块  slist?spt=3&secid=  → 该股全部所属板块（行业+概念），带当日涨幅

用法：
  只对**进入候选的股票**查个股板块（每轮几只到几十只），不查全市场 5571 只。
  结果按天缓存，同一只票一天只查一次。
"""
import json
import time
import urllib.request
from datetime import date

import config

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}

# 非题材板块：指数成分、地域、状态标签、资金面榜单。
# 它们涨幅高不代表有题材驱动，计入「板块共振」会把噪音当成信号。
NOISE_EXACT = {
    "富时罗素", "MSCI中国", "标普道琼斯A股", "沪股通", "深股通", "融资融券",
    "转融券标的", "机构重仓", "基金重仓", "QFII重仓", "社保重仓", "券商重仓",
    "保险重仓", "信托重仓", "证金持股", "汇金持股", "养老金", "基金增仓",
    "基金减仓", "深成500", "上证50", "中证500", "沪深300", "创业板综",
    "科创50", "次新股", "破净股", "高送转", "低价股", "高价股", "ST板块",
    "壳资源", "股权转让", "参股新三板", "预盈预增", "预亏预减",
}
NOISE_PREFIX = ("昨日", "今日", "最近", "连续", "历史", "本周", "本月")
NOISE_SUBSTR = ("板块", "热股", "多板", "做市", "重仓", "增仓", "减仓",
                "MSCI", "富时", "标普", "股通", "融资融券", "转融券", "标的")


def is_noise(name: str) -> bool:
    if not name:
        return True
    if name in NOISE_EXACT:
        return True
    if name.startswith(NOISE_PREFIX):
        return True
    return any(k in name for k in NOISE_SUBSTR)


def _get(url, timeout=10, retries=2):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(0.4)
    raise last


def secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


class TopicMap:
    """概念板块映射。板块列表每 5 分钟刷新，个股映射按天缓存。"""

    def __init__(self):
        self.boards: dict[str, dict] = {}      # bk -> {name, pct, count, leader}
        self.stock: dict[str, list] = {}       # code -> [{bk, name, pct}]
        self._board_ts = 0.0
        self._stock_date = None
        self._fail = 0

    # ---------- 板块列表 ----------
    def refresh_boards(self, force=False):
        if not force and time.time() - self._board_ts < 300 and self.boards:
            return len(self.boards)
        # pz 上限 100，531 个概念板块要翻 6 页
        boards: dict[str, dict] = {}
        try:
            first = _get(f"https://{config.EM_HOST}/api/qt/clist/get"
                         "?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3"
                         "&fields=f12,f14,f3,f104,f128", timeout=12, retries=2)
            total = (first.get("data") or {}).get("total") or 0
            pages = min(10, max(1, (total + 99) // 100))
            rows = list((first.get("data") or {}).get("diff") or [])
            for pn in range(2, pages + 1):
                try:
                    d = _get(f"https://{config.EM_HOST}/api/qt/clist/get"
                             f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                             "&fs=m:90+t:3&fields=f12,f14,f3,f104,f128",
                             timeout=12, retries=1)
                    rows.extend((d.get("data") or {}).get("diff") or [])
                except Exception:
                    continue

            for r in rows:
                if isinstance(r, dict) and r.get("f12"):
                    boards[str(r["f12"])] = {
                        "name": str(r.get("f14") or ""),
                        "pct": float(r.get("f3") or 0),
                        "count": int(r.get("f104") or 0),
                        "leader": str(r.get("f128") or ""),
                    }
            if boards:
                self.boards = boards
                self._board_ts = time.time()
                self._fail = 0
        except Exception as e:
            self._fail += 1
            if self._fail <= 2:
                print(f"[题材] 板块列表失败：{type(e).__name__}")
        return len(self.boards)

    # ---------- 个股所属板块 ----------
    def fetch_stock(self, code: str) -> list:
        today = date.today().isoformat()
        if self._stock_date != today:
            self.stock.clear()
            self._stock_date = today
        if code in self.stock:
            return self.stock[code]
        url = (f"https://{config.EM_HOST}/api/qt/slist/get?spt=3&fltt=2&invt=2"
               f"&fields=f12,f13,f14,f3&secid={secid(code)}&pn=1&pz=60")
        out = []
        try:
            d = _get(url, timeout=8, retries=1)
            diff = (d.get("data") or {}).get("diff") or {}
            if isinstance(diff, dict):
                diff = list(diff.values())
            for r in diff:
                # f13=90 是板块，其他是股票/指数
                if int(r.get("f13") or 0) != 90:
                    continue
                out.append({
                    "bk": str(r.get("f12") or ""),
                    "name": str(r.get("f14") or ""),
                    "pct": float(r.get("f3") or 0),
                })
        except Exception:
            pass
        self.stock[code] = out
        return out

    # ---------- 判断 ----------
    def topic_score(self, code: str) -> tuple[float, list[str]]:
        """题材加分：所属板块越强，加分越多。返回 (加分, 标签列表)。"""
        if not config.TOPIC_ENABLED:
            return 0.0, []
        p = config.TOPIC
        tops = [t for t in self.fetch_stock(code) if not is_noise(t["name"])]
        if not tops:
            return 0.0, []

        strong = [t for t in tops if t["pct"] >= p["strong_pct"]]
        hot = [t for t in tops if t["pct"] >= p["hot_pct"]]

        bonus = 0.0
        tags = []
        if strong:
            # 有板块当日大涨 → 明确的题材驱动
            best = max(strong, key=lambda t: t["pct"])
            bonus += p["strong_bonus"]
            tags.append(f"{best['name']}+{best['pct']:.1f}%")
            if len(strong) >= 2:
                bonus += p["multi_bonus"]
                tags.append(f"{len(strong)}板块共振")
        elif hot:
            best = max(hot, key=lambda t: t["pct"])
            bonus += p["hot_bonus"]
            tags.append(f"{best['name']}+{best['pct']:.1f}%")
        return bonus, tags

    def summary(self, code: str, limit=3) -> str:
        tops = sorted(self.fetch_stock(code), key=lambda t: -t["pct"])[:limit]
        return " / ".join(f"{t['name']}{t['pct']:+.1f}%" for t in tops)

    def strong_boards(self, threshold=3.0, limit=10) -> list[tuple[str, float]]:
        return sorted(((v["name"], v["pct"]) for v in self.boards.values()
                       if v["pct"] >= threshold), key=lambda x: -x[1])[:limit]
