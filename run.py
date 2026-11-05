# -*- coding: utf-8 -*-
"""盘中盯盘 → 策略选股 → 飞书推送。主程序。

用法：
  python run.py --once            # 跑一轮，打印选股结果（不推送）
  python run.py --once --push     # 跑一轮并真的推飞书
  python run.py --loop            # 常驻，交易时段内按 PUSH_INTERVAL 汇总推送
  python run.py --loop --dry-run  # 常驻，但只打印卡片不发送

环境变量：
  FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
  FEISHU_SECRET=（机器人开了签名校验才需要）
"""
import argparse
import sys
import time
from datetime import datetime

# Windows 控制台默认 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import context as context_mod
import events
import feed
import filters
import history as history_mod
import notify
import store
import strategies as st
import topics as topics_mod
import trading_calendar as tc
import watch as watch_mod


# ---------------- 日线历史（策略依赖） ----------------
class Hist:
    """包一层 DailyHistory，控制刷新频率（读库有成本，5 分钟一次足够）。"""

    def __init__(self):
        self.h = history_mod.DailyHistory()
        self._last = 0.0

    def ensure(self, force=False):
        if force or time.time() - self._last > 300:
            n = self.h.refresh()
            self._last = time.time()
            if force:
                print(f"[历史] 日线缓存：{self.h.stats()}")
            return n
        return len(self.h.data)

    def __getattr__(self, name):
        return getattr(self.h, name)


# ---------------- 风控过滤 ----------------
def prefilter(snaps: dict) -> dict:
    """硬性剔除：ST、北交所、流动性不足、过热、已涨停。"""
    out = {}
    for code, s in snaps.items():
        if config.EXCLUDE_ST and "ST" in s.name.upper():
            continue
        if config.EXCLUDE_BJ and s.board == "bj":
            continue
        if s.amount < config.MIN_AMOUNT:
            continue
        if not (config.MIN_FLOAT_CAP <= s.float_cap <= config.MAX_FLOAT_CAP):
            continue
        if s.turnover > config.MAX_TURNOVER:
            continue
        if s.is_limit_up:                      # 已涨停，追不进
            continue
        if not (config.MIN_PCT <= s.pct <= config.MAX_PCT):
            continue
        out[code] = s
    return out


# ---------------- 单轮扫描 ----------------
def scan(snaps: dict, hist) -> list[dict]:
    """跑全部策略，打分排序，返回候选列表。"""
    pool = prefilter(snaps)

    raw: list[tuple] = []          # (code, Hit)
    for code, s in pool.items():
        for fn in st.REGISTRY:
            hit = fn(s, hist, pool)
            if hit:
                raw.append((code, hit))

    if not raw:
        return []

    # 按股票聚合（多策略命中 = 共振）
    agg: dict[str, dict] = {}
    for code, hit in raw:
        row = agg.get(code)
        if row is None:
            s = snaps[code]
            row = agg[code] = {
                "code": code, "name": s.name, "price": s.price, "pct": s.pct,
                "sector": s.sector, "score": 0.0, "tags": [], "hits": [],
                "reason": "", "pe": s.pe_ttm or s.pe, "mf_pct": s.mf_pct,
            }
        row["hits"].append(hit)
        row["tags"].append(hit.rule_name)

    for row in agg.values():
        # 取最高分那条作主因，其余作为加成
        best = max(row["hits"], key=lambda h: h.score)
        others = sorted(row["hits"], key=lambda h: -h.score)[1:]
        row["score"] = best.score + sum(h.score * 0.25 for h in others)
        row["reason"] = best.reason
        row["primary"] = best.rule_id          # 主因，用于推送多样性裁剪
        row["entry"] = best.entry              # 执行方式：次日开盘买 / 尾盘买
        for h in others:
            row["tags"].extend(h.tags)

    scored = list(agg.values())
    sector_cnt = st.sector_resonance(raw, snaps)
    scored = st.apply_resonance(scored, sector_cnt)

    # 资金面加分（主力净占比）
    for r in scored:
        s = snaps.get(r["code"])
        if s is None:
            continue
        bonus, tag = filters.capital_bonus(s)
        if bonus:
            r["score"] = round(min(100.0, r["score"] + bonus), 1)
            r["tags"].append(tag)

    # 单族命中等价于随机（实测 +0.01%），只有多族共振才有微弱优势
    scored = [r for r in scored if r.get("fams", 0) >= config.MIN_FAMILIES]
    scored.sort(key=lambda r: -r["score"])
    return scored


# ---------------- 推送节奏控制 ----------------
class Pusher:
    """控制推送节奏 + 保证一张卡片里形态不重复。"""

    def __init__(self):
        self.last_push: dict[str, float] = {}
        self.push_count: dict[str, int] = {}
        self._synced_date = None

    def sync(self, trade_date: str):
        """从库恢复当日推送计数与冷却时间，避免服务重启后重复推同一只。"""
        if self._synced_date == trade_date:
            return
        try:
            for code, (n, ts) in store.pushes_today(trade_date).items():
                self.push_count[code] = n
                self.last_push[code] = ts or 0.0
            if self.push_count:
                print(f"[推送] 已恢复当日推送计数 {len(self.push_count)} 只")
        except Exception as e:
            print(f"[推送] 恢复计数失败：{e}")
        self._synced_date = trade_date

    def _ok(self, r, now) -> bool:
        code = r["code"]
        if r["score"] < config.MIN_SCORE:
            return False
        if self.push_count.get(code, 0) >= config.MAX_PUSH_PER_CODE:
            return False
        if now - self.last_push.get(code, 0) < config.COOLDOWN_SECONDS:
            return False
        return True

    def pick(self, scored: list[dict], max_n: int | None = None) -> list[dict]:
        """选出本轮要推的股票。max_n 由市场环境决定（防守日可能为 0）。"""
        n = max_n if max_n is not None else config.TOP_N
        if n <= 0:
            return []
        now = time.time()
        out: list[dict] = []
        cap = max(1, int(n * 0.6 + 0.5))          # 单形态最多占 60%
        sector_cap = config.COMB["max_per_sector"]  # 同板块最多几只
        per_rule: dict[str, int] = {}
        per_sector: dict[str, int] = {}

        def _take(r) -> bool:
            sec = r.get("sector") or "未分类"
            if per_sector.get(sec, 0) >= sector_cap:
                return False
            per_sector[sec] = per_sector.get(sec, 0) + 1
            out.append(r)
            return True

        for r in scored:
            if len(out) >= n:
                break
            if not self._ok(r, now):
                continue
            rule = r.get("primary", "")
            if per_rule.get(rule, 0) >= cap:
                continue
            if _take(r):
                per_rule[rule] = per_rule.get(rule, 0) + 1

        # 不够时放宽形态限制补足（板块限制仍然生效）
        if len(out) < n:
            for r in scored:
                if len(out) >= n:
                    break
                if r in out or not self._ok(r, now):
                    continue
                _take(r)

        out.sort(key=lambda r: -r["score"])
        return out

    def mark(self, rows: list[dict]):
        now = time.time()
        for r in rows:
            self.last_push[r["code"]] = now
            self.push_count[r["code"]] = self.push_count.get(r["code"], 0) + 1


def breadth(snaps: dict) -> dict:
    up = sum(1 for s in snaps.values() if s.pct > 0)
    down = sum(1 for s in snaps.values() if s.pct < 0)
    zt = sum(1 for s in snaps.values() if s.is_limit_up)
    dt = sum(1 for s in snaps.values() if s.pct <= -(s.limit_pct - 0.3))
    return {"up": up, "down": down, "limit_up": zt, "limit_down": dt, "total": len(snaps)}


# ---------------- 打印 ----------------
def show(scored: list[dict], bd: dict, top=10):
    print(f"\n=== 扫描 {bd['total']} 只 | 上涨 {bd['up']} 下跌 {bd['down']} "
          f"涨停 {bd['limit_up']} 跌停 {bd['limit_down']} ===")
    if not scored:
        print("无符合条件的标的。")
        return
    print(f"{'代码':<8}{'名称':<10}{'涨幅':>8}{'现价':>8}{'评分':>7}  标签")
    print("-" * 78)
    for r in scored[:top]:
        tags = "/".join(dict.fromkeys(r["tags"]))[:34]
        print(f"{r['code']:<8}{r['name']:<10}{r['pct']:>7.2f}%{r['price']:>8.2f}"
              f"{r['score']:>7.1f}  {tags}")
        print(f"        {r['reason']}  |  {r['sector']}")


# ---------------- 主流程 ----------------
def one_round(hist, pusher: Pusher | None = None, push=False, dry=False,
              watcher: "watch_mod.Watcher | None" = None, ctx=None,
              topics: "topics_mod.TopicMap | None" = None, forecasts=None):
    t0 = time.perf_counter()
    snaps = feed.fetch_all(config.PAGE_SIZE, config.MAX_PAGES,
                           config.FETCH_WORKERS, top_pages=config.TOP_PAGES,
                           timeout=config.FETCH_TIMEOUT)
    t1 = time.perf_counter()
    hist.ensure()                       # 日线缓存（5 分钟刷新一次）
    bd = breadth(snaps)                 # 全市场宽度：必须用未过滤的快照算

    # ---- ① 市场环境 ----
    regime = {"state": "neutral", "label": "中性", "factor": 1.0,
              "score": 0, "reasons": []}
    if ctx is not None and config.REGIME_ENABLED:
        ctx.refresh()
        regime = ctx.regime(bd)

    # ---- ② 基本面硬过滤 ----
    pool, rej_fund = filters.apply_fundamental(snaps)
    # ---- ③ 资金面 ----
    pool, rej_cap = filters.apply_capital(pool)
    # ---- ④ 事件（业绩预告）----
    rej_evt = {}
    if forecasts:
        pool, rej_evt = events.apply(pool, forecasts)

    # ---- ⑤ 形态信号 ----
    scored = scan(pool, hist)

    # ---- ⑥ 题材加分（只对候选查板块，每只一天一次）----
    if topics is not None and config.TOPIC_ENABLED and scored:
        topics.refresh_boards()
        for r in scored:
            b, tags = topics.topic_score(r["code"])
            if b:
                r["score"] = round(min(100.0, r["score"] + b), 1)
                r["tags"].extend(tags)

    now = datetime.now()
    clock = f"{now:%H:%M:%S}"
    today = now.date().isoformat()
    print(f"[采集] {len(snaps)} 只 / {1000*(t1-t0):.0f}ms  "
          f"过筛 {len(pool)}  候选 {len(scored)}  {clock}")
    parts = []
    for label, rej in (("基本面", rej_fund), ("资金面", rej_cap), ("事件", rej_evt)):
        if rej:
            parts.append(label + " " + "，".join(
                f"{k}{v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])))
    if parts:
        print("[过滤] " + "；".join(parts))
    print(f"[环境] {regime['label']}({regime['score']:+d})  " +
          "，".join(regime["reasons"][:4]))
    if topics is not None and topics.boards:
        strong = topics.strong_boards(3.0, 5)
        if strong:
            print("[题材] 强势概念：" + "　".join(f"{n}+{p:.1f}%" for n, p in strong))

    pushed: list[dict] = []
    if push and pusher:
        pusher.sync(today)
        max_n = max(0, int(config.TOP_N * regime["factor"] + 0.5))
        if max_n == 0:
            print("[推送] 市场环境防守，本轮暂停推送")
        else:
            rows = pusher.pick(scored, max_n)
            if rows:
                card = notify.build_signal_card(rows, bd, time.time(), regime)
                res = notify.dispatch(card, dry_run=dry)
                if any(v[0] for v in res.values()) or dry:
                    pusher.mark(rows)
                    pushed = rows
                    print(f"[推送] 已发送 {len(rows)} 只 → {len(res)} 个渠道")
            else:
                print("[推送] 本轮无可推标的（未达分数线或都在冷却期）")

    # 止损/止盈监控：每轮都查，不受推送节奏限制（复用同一份快照，零额外请求）
    if watcher is not None:
        alerts = watcher.check(snaps)
        if alerts:
            notify.dispatch(notify.build_alert_card(alerts), dry_run=dry)
            print(f"[告警] {len(alerts)} 条：" +
                  "，".join(f"{a.name}{a.pnl:+.2f}%({a.kind})" for a in alerts))

    # 全量候选落库（含未推送的），覆盖率统计靠它
    try:
        store.save_candidates(today, clock, scored, {r["code"] for r in pushed})
        if pushed:
            store.save_push(today, clock, pushed)
            if watcher is not None:
                watcher.register(pushed, today)
    except Exception as e:
        print(f"[落库] 失败：{type(e).__name__}: {e}")

    return snaps, scored, bd


def in_session(now: datetime) -> bool:
    """是否在 A 股可扫描时段（含集合竞价 9:15-9:25）。交易日由日历判断。"""
    if not tc.is_trading_day(now.date()):
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= hm <= (11 * 60 + 30) or (13 * 60) <= hm <= (15 * 60)


def closing_report(snaps: dict, trade_date: str):
    """收盘观察名单：今日推送但未确认买入的票，收盘表现如何。

    这类票盘中只跟踪不告警，收盘降级成一份战绩，用来验证策略是否有效。
    """
    try:
        pend = store.load_pending(trade_date)
    except Exception as e:
        print(f"[收盘] 读观察名单失败：{e}")
        return
    if not pend:
        return

    rows = []
    for p in pend:
        s = snaps.get(p["code"])
        sp = p["signal_price"] or 0
        if s is None or sp <= 0:
            continue
        rows.append({
            "code": p["code"], "name": p["name"],
            "signal_price": sp, "close": s.price,
            "pnl": (s.price / sp - 1) * 100,
            "peak_pnl": ((p["peak"] or sp) / sp - 1) * 100,
            "low_pnl": ((p["low"] or sp) / sp - 1) * 100,
        })
    if not rows:
        return

    rows.sort(key=lambda r: -r["pnl"])
    notify.dispatch(notify.build_watchlist_card(rows, trade_date))
    print(f"[收盘] 观察名单 {len(rows)} 只已推送（未确认买入，仅记录）")

    # 归档，避免明天重复出现在观察名单里
    # 注意：老库迁移过来的行 state 可能还是 watching，只要 confirmed=0 都算未确认
    with store.conn() as c:
        n = c.execute(
            "UPDATE positions SET state='archived' WHERE trade_date=? AND confirmed=0 "
            "AND state IN ('pending','watching','trailing')", (trade_date,)).rowcount
    if n:
        print(f"[收盘] 已归档 {n} 只未确认标的")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--push", action="store_true", help="真的推飞书")
    ap.add_argument("--dry-run", action="store_true", help="只打印卡片不发送")
    ap.add_argument("--interval", type=float, default=None, help="覆盖轮询间隔（秒）")
    ap.add_argument("--refresh-calendar", action="store_true", help="从东财校准交易日历")
    ap.add_argument("--test-push", action="store_true", help="向所有已配置渠道发一条测试消息")
    args = ap.parse_args()

    if args.test_push:
        notify.test_push(dry_run=args.dry_run)
        return

    store.init()
    if args.refresh_calendar:
        tc.refresh()
    print(f"[启动] {datetime.now():%Y-%m-%d %H:%M}  "
          f"交易日={tc.is_trading_day()}  数据库={store.DB_PATH}")

    hist = Hist()
    hist.ensure(force=True)
    if not hist.h.data:
        print("⚠ 日线缓存为空——涨停次日/MA20/平台突破/连跌首阳/三连阳 都不会触发。")
        print("  先跑一次：python history.py --backfill")
    ctx = context_mod.MarketContext()
    ctx.refresh(force=True)
    if ctx.idx:
        print(f"[环境] {ctx.summary()}")
    topics = topics_mod.TopicMap()
    topics.refresh_boards(force=True)
    print(f"[题材] 概念板块 {len(topics.boards)} 个")
    forecasts = events.load() if config.EVENT_ENABLED else {}
    pusher = Pusher()
    watcher = watch_mod.Watcher()
    watcher.load()
    interval = args.interval or config.POLL_SECONDS

    if args.once or not args.loop:
        snaps, scored, bd = one_round(hist, pusher, push=args.push, dry=args.dry_run,
                                      watcher=watcher, ctx=ctx,
                                      topics=topics, forecasts=forecasts)
        show(scored, bd)
        if args.push and not args.dry_run:
            active = [c.name for c in notify.load_channels() if c.type != "console"]
            if not active:
                print("\n⚠ 没有配置任何推送渠道，消息只打印未发送。")
                print("  在 .env 里填 FEISHU_WEBHOOK / DINGTALK_WEBHOOK / WECOM_WEBHOOK 等，")
                print("  然后用 `python run.py --test-push` 验证。")
        return

    print(f"常驻模式启动：每 {interval}s 采集，每 {config.PUSH_INTERVAL}s 汇总推送。Ctrl+C 退出。")
    last_push = 0.0
    synced_date = None          # 收盘快照只存一次
    while True:
        try:
            now = datetime.now()
            today = now.date().isoformat()

            # 收盘后存一份全市场快照，供 review.py 算覆盖率
            if (tc.is_trading_day(now.date()) and now.hour * 60 + now.minute >= 15 * 60
                    and synced_date != today):
                try:
                    snaps = feed.fetch_all(config.PAGE_SIZE, config.MAX_PAGES,
                                           config.FETCH_WORKERS,
                                           top_pages=config.TOP_PAGES,
                                           timeout=config.FETCH_TIMEOUT)
                    store.upsert_daily([
                        (s.code, today, s.open, s.high, s.low, s.price, s.pct, s.amount)
                        for s in snaps.values()])
                    synced_date = today
                    print(f"[收盘] 已存 {len(snaps)} 只全市场收盘快照，明天可算覆盖率")
                    closing_report(snaps, today)
                except Exception as e:
                    print(f"[收盘] 快照失败：{e}")

            if not in_session(now):
                print(f"[{now:%H:%M:%S}] 非交易时段，休眠 60s")
                time.sleep(60)
                continue
            do_push = (time.time() - last_push) >= config.PUSH_INTERVAL
            snaps, scored, bd = one_round(hist, pusher, push=(do_push and args.push),
                                          dry=args.dry_run, watcher=watcher, ctx=ctx,
                                          topics=topics, forecasts=forecasts)
            if do_push:
                last_push = time.time()
                show(scored, bd, top=config.TOP_N)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n退出。")
            break
        except Exception as e:
            print(f"[错误] {type(e).__name__}: {e}，10s 后重试")
            time.sleep(10)


if __name__ == "__main__":
    main()
