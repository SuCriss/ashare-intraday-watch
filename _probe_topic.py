# -*- coding: utf-8 -*-
"""探测：个股→概念板块映射 + 业绩预告批量拉取。"""
import json
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/"}
OK, NO = "✅", "❌"


def get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


print("=" * 78)
print("1. 个股所属板块（slist，多种参数试）")
print("=" * 78)
tries = [
    ("spt=3 + fields", "https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2&fields=f12,f13,f14,f3&secid=0.300750&pn=1&pz=50"),
    ("spt=3 无 fltt", "https://push2.eastmoney.com/api/qt/slist/get?spt=3&fields=f12,f13,f14,f3&secid=0.300750&pn=1&pz=50"),
    ("spt=1", "https://push2.eastmoney.com/api/qt/slist/get?spt=1&fltt=2&invt=2&fields=f12,f13,f14,f3&secid=0.300750&pn=1&pz=50"),
]
for name, url in tries:
    try:
        d = get(url)
        data = d.get("data")
        print(f"  {OK} {name}: type(data)={type(data).__name__}  {json.dumps(data, ensure_ascii=False)[:220]}")
    except Exception as e:
        print(f"  {NO} {name}: {type(e).__name__}: {e}")

print()
print("=" * 78)
print("2. 概念板块列表 + 成分股反查")
print("=" * 78)
try:
    d = get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=500&po=1&np=1"
            "&fltt=2&invt=2&fid=f3&fs=m:90+t:3&fields=f12,f14,f3,f104,f105,f128")
    boards = (d.get("data") or {}).get("diff") or []
    print(f"  {OK} 概念板块总数: {(d.get('data') or {}).get('total')}  本页 {len(boards)}")
    for b in boards[:3]:
        print(f"     {b.get('f12')} {b.get('f14')} 涨{b.get('f3')}% 成分{b.get('f104')}只 领涨{b.get('f128')}")
    if boards:
        bk = boards[0]["f12"]
        t0 = time.perf_counter()
        d2 = get(f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=200&po=1&np=1"
                 f"&fltt=2&invt=2&fid=f3&fs=b:{bk}&fields=f12,f14")
        cons = (d2.get("data") or {}).get("diff") or []
        print(f"  {OK} 板块 {bk} 成分股 {len(cons)} 只 / {(time.perf_counter()-t0)*1000:.0f}ms")
        print(f"     样例: {[c.get('f14') for c in cons[:5]]}")
except Exception as e:
    print(f"  {NO} {type(e).__name__}: {e}")

print()
print("=" * 78)
print("3. 业绩预告批量（datacenter）")
print("=" * 78)
for rpt in ("2026-09-30", "2026-06-30"):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_PUBLIC_OP_NEWPREDICT&columns=ALL"
           "&pageSize=500&pageNumber=1&sortColumns=NOTICE_DATE&sortTypes=-1"
           f"&filter=(REPORT_DATE%3D%27{rpt}%27)")
    try:
        d = get(url, headers={**UA, "Referer": "https://data.eastmoney.com/"})
        res = d.get("result") or {}
        rows = res.get("data") or []
        print(f"  {OK} {rpt}: 总 {res.get('count')} 条, 本页 {len(rows)}")
        if rows:
            r0 = rows[0]
            keys = ["SECURITY_CODE", "SECURITY_NAME_ABBR", "PREDICT_TYPE",
                    "PREDICT_CONTENT", "FORECAST_STATE", "ADD_AMP_LOWER",
                    "ADD_AMP_UPPER", "NOTICE_DATE"]
            for k in keys:
                if k in r0:
                    print(f"     {k} = {r0[k]}")
    except Exception as e:
        print(f"  {NO} {rpt}: {type(e).__name__}: {e}")
