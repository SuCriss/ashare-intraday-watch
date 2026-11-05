# -*- coding: utf-8 -*-
"""在目标服务器上跑：验证东财 push2delay 是否可完整替代 push2。"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "http://quote.eastmoney.com/"}
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f2,f3,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f22,f62,f100,f184"

HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com"]


def page(host, pn, pz=100, fid="f12"):
    u = (f"https://{host}/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid={fid}"
         f"&fs={FS}&fields={FIELDS}&pz={pz}&pn={pn}")
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


for host in HOSTS:
    print(f"\n{'='*66}\n{host}\n{'='*66}")
    try:
        t0 = time.perf_counter()
        d = page(host, 1)
        ms = (time.perf_counter() - t0) * 1000
        total = d["data"]["total"]
        rows = d["data"]["diff"]
        print(f"  首页 OK: total={total} 本页={len(rows)} {ms:.0f}ms")
    except Exception as e:
        print(f"  首页失败: {type(e).__name__}: {e}")
        continue

    # 字段完整性
    r0 = rows[0]
    print(f"  字段样例: {r0}")
    missing = [k for k in ("f8", "f10", "f62", "f100") if k not in r0 or r0[k] in (None, "-")]
    print(f"  关键字段缺失: {missing or '无'}")

    # 分页完整性
    t0 = time.perf_counter()
    all_rows = []
    try:
        with ThreadPoolExecutor(max_workers=30) as ex:
            for res in ex.map(lambda p: page(host, p), range(1, 61)):
                all_rows += res["data"]["diff"] or []
    except Exception as e:
        print(f"  分页失败: {type(e).__name__}: {e}")
        continue
    ms = (time.perf_counter() - t0) * 1000
    codes = [r["f12"] for r in all_rows]
    print(f"  60 页并发30: {len(all_rows)} 行 / 唯一 {len(set(codes))} / {ms:.0f}ms")

    # 成交额排序前 30 页
    t0 = time.perf_counter()
    top = []
    try:
        with ThreadPoolExecutor(max_workers=20) as ex:
            for res in ex.map(lambda p: page(host, p, 100, "f6"), range(1, 31)):
                top += res["data"]["diff"] or []
    except Exception as e:
        print(f"  Top 分页失败: {e}")
        continue
    ms = (time.perf_counter() - t0) * 1000
    amts = sorted([r["f6"] for r in top if isinstance(r.get("f6"), (int, float))], reverse=True)
    print(f"  Top3000: {len(top)} 行 / {ms:.0f}ms / 成交额下限 {amts[-1]/1e8:.2f}亿")

# 历史 K 线（backtest 用）
print(f"\n{'='*66}\n历史 K 线\n{'='*66}")
for host in ("push2his.eastmoney.com", "push2hisdelay.eastmoney.com"):
    u = (f"https://{host}/api/qt/stock/kline/get?secid=1.000001&klt=101&fqt=1"
         "&beg=20260101&end=20261231&fields1=f1&fields2=f51,f53")
    try:
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        n = len(d["data"]["klines"]) if d.get("data") else 0
        print(f"  {host}: OK {n} 根")
    except Exception as e:
        print(f"  {host}: 失败 {type(e).__name__}: {e}")

# 5 分钟 K 线
print(f"\n{'='*66}\n5 分钟 K 线（backtest 用）\n{'='*66}")
for host in ("push2his.eastmoney.com", "push2hisdelay.eastmoney.com"):
    u = (f"https://{host}/api/qt/stock/kline/get?secid=0.300857&klt=5&fqt=1"
         "&beg=0&end=20500101&lmt=100&fields1=f1&fields2=f51,f53")
    try:
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        n = len(d["data"]["klines"]) if d.get("data") else 0
        print(f"  {host}: OK {n} 根")
    except Exception as e:
        print(f"  {host}: 失败 {type(e).__name__}: {e}")
