# -*- coding: utf-8 -*-
"""全市场快照采集：东财 clist 分页（主）+ 新浪批量（备）+ ulist 批量（快查）。

实测（2026-11-05 盘中）：
  东财 60 页并发 30 → 849ms
  东财 60 页并发 45 → 527ms   ← 采用
  东财 60 页并发 60 → 1253ms  ← 被限流，反而更慢
  新浪 8 批并发 8    → 1108ms  ← 备源，字段少（无量比/主力/行业）
  东财 ulist 批量    → 163ms   ← 指定代码快查

排序键必须用 fid=f12（代码）。用 fid=f3（涨幅）翻页时排名在变，
实测 60 页有 184 条重复/漏项，会漏掉正在拉升的股票。
"""
import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import config

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://quote.eastmoney.com/",
}

# 沪深主板 + 创业板 + 科创板 + 北交所
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

FIELDS = ",".join([
    "f2", "f3", "f5", "f6", "f8", "f10", "f12", "f14", "f15", "f16", "f17", "f18",
    "f20", "f21", "f22", "f62", "f100", "f184",
])

# host 可在 config 里切换（海外服务器用 push2delay 更稳）
BASE = f"https://{config.EM_HOST}/api/qt/clist/get"
ULIST = f"https://{config.EM_HOST}/api/qt/ulist.np/get"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
UNIVERSE = os.path.join(DATA_DIR, "universe.json")


@dataclass
class Snap:
    code: str
    name: str
    price: float
    pct: float
    vol: float
    amount: float
    turnover: float
    vol_ratio: float
    high: float
    low: float
    open: float
    preclose: float
    total_cap: float
    float_cap: float
    speed: float
    mf_net: float
    sector: str
    mf_pct: float

    @property
    def board(self) -> str:
        c = self.code
        if c.startswith(("300", "301")):
            return "gem"
        if c.startswith("688"):
            return "star"
        if c.startswith(("8", "4", "92")):
            return "bj"
        return "main"

    @property
    def limit_pct(self) -> float:
        """当日涨跌停幅度（%）。ST 主板 5%，创业板/科创板 ST 仍 20%，北交所 30%。"""
        is_st = "ST" in self.name.upper()
        if self.board == "bj":
            return 30.0
        if self.board in ("gem", "star"):
            return 20.0
        return 5.0 if is_st else 10.0

    @property
    def is_limit_up(self) -> bool:
        return self.pct >= self.limit_pct - 0.3

    @property
    def day_position(self) -> float:
        rng = self.high - self.low
        return 1.0 if rng <= 0 else (self.price - self.low) / rng


def _num(v, default=0.0) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return default


def _build(row) -> Snap | None:
    code = str(row.get("f12") or "")
    if len(code) != 6:
        return None
    price = _num(row.get("f2"))
    preclose = _num(row.get("f18"))
    if price <= 0 or preclose <= 0:
        return None
    return Snap(
        code=code,
        name=str(row.get("f14") or ""),
        price=price,
        pct=_num(row.get("f3")),
        vol=_num(row.get("f5")),
        amount=_num(row.get("f6")),
        turnover=_num(row.get("f8")),
        vol_ratio=_num(row.get("f10")),
        high=_num(row.get("f15")) or price,
        low=_num(row.get("f16")) or price,
        open=_num(row.get("f17")) or price,
        preclose=preclose,
        total_cap=_num(row.get("f20")),
        float_cap=_num(row.get("f21")),
        speed=_num(row.get("f22")),
        mf_net=_num(row.get("f62")),
        sector=str(row.get("f100") or "未分类"),
        mf_pct=_num(row.get("f184")),
    )


# ---------------- 东财主链路 ----------------
def _fetch_page(pn: int, page_size: int):
    url = (f"{BASE}?po=1&np=1&fltt=2&invt=2&fid=f12&fs={FS}"
           f"&fields={FIELDS}&pz={page_size}&pn={pn}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _safe_page(pn, page_size):
    for _ in range(2):                 # 单页重试一次
        try:
            d = _fetch_page(pn, page_size)
            return (d.get("data") or {}).get("diff") or []
        except Exception:
            continue
    return None


def _fetch_full(page_size=100, max_pages=70, workers=45, quiet=False) -> dict[str, Snap]:
    """全量分页（60 页）。只在首次建代码清单、或双通道都挂时用。
    实测 60 请求会触发东财限流，延迟在 0.9~3.4s 之间抖，不适合每轮跑。"""
    try:
        first = _fetch_page(1, page_size)
    except Exception as e:
        if not quiet:
            print(f"[采集] 东财首页失败：{e}")
        return {}

    total = (first.get("data") or {}).get("total") or 0
    pages = min(max_pages, max(1, (total + page_size - 1) // page_size))

    rows = list((first.get("data") or {}).get("diff") or [])
    failed = []
    if pages > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_safe_page, p, page_size): p for p in range(2, pages + 1)}
            for f in as_completed(futs):
                res = f.result()
                if res is None:
                    failed.append(futs[f])
                else:
                    rows.extend(res)
    if failed:
        for p in list(failed):
            res = _safe_page(p, page_size)
            if res is not None:
                rows.extend(res)
                failed.remove(p)

    out: dict[str, Snap] = {}
    for r in rows:
        s = _build(r)
        if s:
            out[s.code] = s
    if not quiet:
        print(f"[采集] 全量通道 {len(out)} 只（失败页 {len(failed)}）")
    return out


def _fetch_top(pages=20, page_size=100, workers=30) -> dict[str, Snap]:
    """东财按成交额排序取前 N 页。20 页 = Top2000，本机实测 349ms。
    Top2000 的成交额下限约 1.5 亿，天然覆盖 MIN_AMOUNT=1 亿的策略门槛。

    海外服务器上东财单页要 5~6s，所以一次性打满并发（1 批打完），
    并对失败页单独重试一次。"""
    def page(pn):
        url = (f"{BASE}?po=1&np=1&fltt=2&invt=2&fid=f6&fs={FS}"
               f"&fields={FIELDS}&pz={page_size}&pn={pn}")
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.loads(r.read())
                return (d.get("data") or {}).get("diff") or []
            except Exception:
                if attempt == 1:
                    return []
        return []

    out: dict[str, Snap] = {}
    with ThreadPoolExecutor(max_workers=min(workers, pages)) as ex:
        for rows in ex.map(page, range(1, pages + 1)):
            for r in rows:
                s = _build(r)
                if s:
                    out[s.code] = s
    return out


def _universe_stale(max_age_days=7) -> bool:
    """代码清单超过 N 天就重建一次，避免漏掉新上市股票。"""
    if not os.path.exists(UNIVERSE):
        return True
    try:
        with open(UNIVERSE, encoding="utf-8") as f:
            ts = json.load(f).get("ts") or 0
        return (__import__("time").time() - ts) > max_age_days * 86400
    except Exception:
        return True


# 上一轮成功的快照，用于单通道失败时补齐缺失代码（只补缺失，不覆盖已有价格）
_LAST: dict[str, Snap] = {}


def fetch_all(page_size=100, max_pages=70, workers=45, quiet=False,
              top_pages=20, timeout=None, retry=None) -> dict[str, Snap]:
    """双通道并行，取长补短：

      通道 A  新浪全市场   8 请求 / 831ms  → 全市场价量、涨跌家数
      通道 B  东财 Top3000 30 请求 / 549ms → 量比/换手/主力/行业 完整字段

    合并时东财覆盖新浪（同代码字段更全）。总耗时 ≈ max(两通道) ≈ 0.85s，
    请求数 38 个（原来 60 个），大幅降低被限流的概率。

    上游按请求量限流：连续高强度请求后两通道都会劣化到 5s+。
    所以单通道超时后会重试一次，仍失败则用上一轮数据补齐缺失代码（不覆盖已有价格，
    避免拿陈旧价格去做止损判断）。
    """
    # 首次运行 / 代码清单过期：必须先全量建清单，否则新浪备源无从下手
    if _universe_stale():
        full = _fetch_full(page_size, max_pages, workers, quiet)
        if len(full) > 4000:
            _save_universe(list(full))
        if full:
            _LAST.clear()
            _LAST.update(full)
            return full

    timeout = timeout if timeout is not None else config.FETCH_TIMEOUT
    retry = retry if retry is not None else config.FETCH_RETRY

    def _try(fn, label):
        for attempt in range(retry + 1):
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(fn).result(timeout=timeout)
            except Exception as e:
                if attempt == retry and not quiet:
                    print(f"[采集] {label} 通道 {retry + 1} 次均失败：{type(e).__name__}")
        return {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sina = ex.submit(_try, fetch_all_sina, "新浪")
        f_em = ex.submit(_try, lambda: _fetch_top(top_pages, page_size), "东财")
        sina = f_sina.result()
        em = f_em.result()

    # 东财没拿到足够数据 → 退回全量分页补（代价高，只在最后手段用）
    if len(em) < top_pages * page_size * 0.5:
        if not quiet:
            print(f"[采集] 东财 Top 通道只拿到 {len(em)} 只，退回全量分页")
        em = _fetch_full(page_size, max_pages, workers, quiet)

    out = dict(sina)
    out.update(em)                      # 东财字段更全，覆盖同代码

    # 兜底：只补「本轮完全缺失」的代码，不覆盖已有价格
    missing = 0
    if _LAST:
        for code, s in _LAST.items():
            if code not in out:
                out[code] = s
                missing += 1

    if len(out) >= 4000:
        _LAST.clear()
        _LAST.update(out)

    if not quiet:
        extra = f" + 上轮补 {missing}" if missing else ""
        print(f"[采集] 新浪 {len(sina)} + 东财 {len(em)} → 合并 {len(out)} 只{extra}")
    return out


# ---------------- 新浪备源 ----------------
def _sina_batch(codes: list[str]):
    req = urllib.request.Request(
        "http://hq.sinajs.cn/list=" + ",".join(codes),
        headers={**UA, "Referer": "https://finance.sina.com.cn"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("gbk", "replace")


def _parse_sina(body: str) -> dict[str, Snap]:
    """新浪字段：0名称 1开 2昨收 3现价 4最高 5最低 6买一 7卖一 8量(股) 9额(元)。
    没有量比/换手/主力/行业 —— 备源只保证价格链路可用。"""
    out = {}
    for line in body.splitlines():
        if '="' not in line:
            continue
        try:
            head, rest = line.split('="', 1)
            code = head.split("_")[-1][2:]          # sh600000 → 600000
            p = rest.rstrip('";').split(",")
            if len(p) < 10:
                continue
            name = p[0]
            preclose = float(p[2] or 0)
            price = float(p[3] or 0)
            if preclose <= 0 or price <= 0:
                continue
            out[code] = Snap(
                code=code, name=name, price=price,
                pct=(price / preclose - 1) * 100,
                vol=float(p[8] or 0) / 100,          # 股 → 手
                amount=float(p[9] or 0),
                turnover=0.0, vol_ratio=0.0,
                high=float(p[4] or price), low=float(p[5] or price),
                open=float(p[1] or price), preclose=preclose,
                total_cap=0.0, float_cap=0.0, speed=0.0,
                mf_net=0.0, sector="未分类", mf_pct=0.0,
            )
        except Exception:
            continue
    return out


def _load_universe() -> list[str]:
    if not os.path.exists(UNIVERSE):
        return []
    try:
        with open(UNIVERSE, encoding="utf-8") as f:
            return json.load(f).get("codes") or []
    except Exception:
        return []


def _save_universe(codes: list[str]):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UNIVERSE, "w", encoding="utf-8") as f:
            json.dump({"codes": sorted(codes), "ts": __import__("time").time()}, f)
    except Exception:
        pass


def _sina_prefix(code: str) -> str:
    """新浪代码前缀：沪 sh / 深 sz / 北交所 bj。北交所写成 sz 会全部丢。"""
    if code.startswith("6"):
        return "sh"
    if code.startswith(("8", "4", "92")):
        return "bj"
    return "sz"


def fetch_all_sina() -> dict[str, Snap]:
    """新浪全市场：8 批 × 800 只并发，实测 831ms。需要本地代码清单。"""
    codes = _load_universe()
    if not codes:
        print("[采集] 新浪备源缺少代码清单（data/universe.json），需先成功跑一次东财")
        return []
    pref = [_sina_prefix(c) + c for c in codes]
    chunks = [pref[i:i + 800] for i in range(0, len(pref), 800)]
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for body in ex.map(_sina_batch, chunks):
            out.update(_parse_sina(body))
    return out


# ---------------- 指定代码快查 ----------------
def fetch_codes(codes: list[str]) -> dict[str, Snap]:
    """东财 ulist 批量查指定代码，实测 5 只 163ms。持仓监控/收盘快照用。"""
    if not codes:
        return {}
    secids = [("1." if c.startswith("6") else "0.") + c for c in codes]
    out = {}
    for i in range(0, len(secids), 100):
        chunk = secids[i:i + 100]
        url = (f"{ULIST}?fltt=2&invt=2&secids={','.join(chunk)}"
               "&fields=f2,f3,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f22,f62,f100,f184")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            for row in (d.get("data") or {}).get("diff") or []:
                s = _build(row)
                if s:
                    out[s.code] = s
        except Exception:
            continue
    return out
