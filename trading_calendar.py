# -*- coding: utf-8 -*-
"""A 股交易日历。

2026 年休市日由上证指数日 K 反推得到（2026-11-05 实测，202 个已过交易日），
不是手工猜的。首次运行会从东财再校准一次并缓存到 data/calendar.json。
"""
import datetime as dt
import json
import os
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE = os.path.join(DATA_DIR, "calendar.json")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}

# 工作日但休市（周末之外）。2026 段由指数日 K 反推，2027 元旦为预排。
HOLIDAYS = {
    "2026-01-01", "2026-01-02",                                      # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-23",                                      # 春节
    "2026-04-06",                                                    # 清明
    "2026-05-01", "2026-05-04", "2026-05-05",                        # 劳动节
    "2026-06-19",                                                    # 端午
    "2026-09-25",                                                    # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05",
    "2026-10-06", "2026-10-07",                                      # 国庆
    "2027-01-01",                                                    # 元旦（预排）
}

# 已确认的交易日（从东财拉到的真实日 K 日期），优先于规则判断
_CONFIRMED: set[str] = set()


def _load_cache():
    global HOLIDAYS, _CONFIRMED
    if not os.path.exists(CACHE):
        return
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        HOLIDAYS |= set(d.get("holidays", []))
        _CONFIRMED |= set(d.get("confirmed", []))
    except Exception:
        pass


def _save_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({
            "holidays": sorted(HOLIDAYS),
            "confirmed": sorted(_CONFIRMED),
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        }, f, ensure_ascii=False, indent=2)


def refresh(years=3, quiet=False):
    """从东财拉上证指数日 K，用真实交易日覆盖日历。失败不抛异常。"""
    global _CONFIRMED
    beg = f"{dt.date.today().year - years}0101"
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid=1.000001&klt=101&fqt=1&beg={beg}&end=20500101&lmt=5000"
           "&fields1=f1&fields2=f51")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())["data"]
        days = {k.split(",")[0] for k in data["klines"]}
    except Exception as e:
        if not quiet:
            print(f"[日历] 刷新失败（用内置表）：{e}")
        return False

    _CONFIRMED |= days
    # 反推：过去的工作日里，不在交易日集合中的就是休市日
    today = dt.date.today()
    start = dt.date.fromisoformat(min(days))
    end = dt.date.fromisoformat(max(days))
    d = start
    added = 0
    while d <= end:
        if d.weekday() < 5:
            s = d.isoformat()
            if s not in days and s not in HOLIDAYS:
                HOLIDAYS.add(s)
                added += 1
        d += dt.timedelta(days=1)
    _save_cache()
    if not quiet:
        print(f"[日历] 校准完成：{len(days)} 个交易日，新增 {added} 个休市日，缓存 {CACHE}")
    return True


def is_trading_day(d=None) -> bool:
    """d 可以是 date / datetime / 'YYYY-MM-DD' / None(今天)。"""
    if d is None:
        d = dt.date.today()
    if isinstance(d, dt.datetime):
        d = d.date()
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    if d.weekday() >= 5:
        return False
    s = d.isoformat()
    if s in _CONFIRMED:
        return True
    return s not in HOLIDAYS


def prev_trading_day(d=None, n=1):
    d = d or dt.date.today()
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    while n > 0:
        d -= dt.timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def next_trading_day(d=None, n=1):
    d = d or dt.date.today()
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    while n > 0:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def recent_trading_days(n=30, end=None):
    """最近 n 个交易日（含 end），升序返回字符串日期。"""
    end = end or dt.date.today()
    if isinstance(end, str):
        end = dt.date.fromisoformat(end)
    out, d = [], end
    while len(out) < n:
        if is_trading_day(d):
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return sorted(out)


_load_cache()


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        refresh()
    print(f"今天 {dt.date.today()} 是否交易日：{is_trading_day()}")
    print(f"最近 10 个交易日：{recent_trading_days(10)}")
    print(f"内置休市日 {len(HOLIDAYS)} 个")
