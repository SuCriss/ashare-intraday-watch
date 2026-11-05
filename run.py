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
from collections import deque
from datetime import datetime

# Windows 控制台默认 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import feed
import notify
import store
import strategies as st
import trading_calendar as tc
import watch as watch_mod


# ---------------- 价格环形缓冲 ----------------
class History:
    """每只股票最近 N 个快照的价格，用来算 N 分钟涨速。"""

    def __init__(self, maxlen=240):
        self.maxlen = maxlen
        self.buf: dict[str, deque] = {}

    def push(self, snaps: dict):
        now = time.time()
        for code, s in snaps.items():
            d = self.buf.get(code)
            if d is None:
                d = self.buf[code] = deque(maxlen=self.maxlen)
            d.append((now, s.price))

    def speed(self, code: str, seconds: int):
        """返回最近 seconds 秒的涨速（%）。样本不足返回 None。"""
        d = self.buf.get(code)
        if not d or len(d) < 3:
            return None
        now = time.time()
        base = None
        for ts, px in d:
            if now - ts <= seconds:
                base = px
                break
        if base is None or base <= 0:
            return None
        cur = d[-1][1]
        span = now - d[0][0]
        if span < seconds * 0.5:      # 缓冲还没覆盖到窗口，不可信
            return None
        return (cur / base - 1) * 100


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
def scan(snaps: dict, hist: History) -> list[dict]:
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
                "reason": "",
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
        for h in others:
            row["tags"].extend(h.tags)

    scored = list(agg.values())
    sector_cnt = st.sector_resonance(raw, snaps)
    scored = st.apply_resonance(scored, sector_cnt)
    scored.sort(key=lambda r: -r["score"])
    return scored


# ---------------- 推送节奏控制 ----------------
class Pusher:
    """控制推送节奏 + 保证一张卡片里形态不重复。"""

    def __init__(self):
        self.last_push: dict[str, float] = {}
        self.push_count: dict[str, int] = {}

    def _ok(self, r, now) -> bool:
        code = r["code"]
        if r["score"] < config.MIN_SCORE:
            return False
        if self.push_count.get(code, 0) >= config.MAX_PUSH_PER_CODE:
            return False
        if now - self.last_push.get(code, 0) < config.COOLDOWN_SECONDS:
            return False
        return True

    def pick(self, scored: list[dict]) -> list[dict]:
        now = time.time()
        out: list[dict] = []
        # 单形态最多占 60%，避免一张卡片全是「回踩反包」
        cap = max(1, int(config.TOP_N * 0.6 + 0.5))
        per_rule: dict[str, int] = {}

        for r in scored:
            if len(out) >= config.TOP_N:
                break
            if not self._ok(r, now):
                continue
            rule = r.get("primary", "")
            if per_rule.get(rule, 0) >= cap:
                continue
            per_rule[rule] = per_rule.get(rule, 0) + 1
            out.append(r)

        # 不够 TOP_N 时放宽形态限制补足
        if len(out) < config.TOP_N:
            for r in scored:
                if len(out) >= config.TOP_N:
                    break
                if r in out or not self._ok(r, now):
                    continue
                out.append(r)

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
def one_round(hist: History, pusher: Pusher | None = None, push=False, dry=False,
              watcher: "watch_mod.Watcher | None" = None):
    t0 = time.perf_counter()
    snaps = feed.fetch_all(config.PAGE_SIZE, config.MAX_PAGES,
                           config.FETCH_WORKERS, top_pages=config.TOP_PAGES,
                           timeout=config.FETCH_TIMEOUT)
    t1 = time.perf_counter()
    hist.push(snaps)
    scored = scan(snaps, hist)
    bd = breadth(snaps)

    now = datetime.now()
    clock = f"{now:%H:%M:%S}"
    today = now.date().isoformat()
    print(f"[采集] {len(snaps)} 只 / {1000*(t1-t0):.0f}ms  "
          f"候选 {len(scored)} 只  {clock}")

    pushed: list[dict] = []
    if push and pusher:
        rows = pusher.pick(scored)
        if rows:
            card = notify.build_signal_card(rows, bd, time.time())
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

    hist = History()
    pusher = Pusher()
    watcher = watch_mod.Watcher()
    watcher.load()
    interval = args.interval or config.POLL_SECONDS

    if args.once or not args.loop:
        snaps, scored, bd = one_round(hist, pusher, push=args.push, dry=args.dry_run,
                                      watcher=watcher)
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
                                           config.FETCH_WORKERS)
                    store.upsert_daily([
                        (s.code, today, s.open, s.high, s.low, s.price, s.pct, s.amount)
                        for s in snaps.values()])
                    synced_date = today
                    print(f"[收盘] 已存 {len(snaps)} 只全市场收盘快照，明天可算覆盖率")
                except Exception as e:
                    print(f"[收盘] 快照失败：{e}")

            if not in_session(now):
                print(f"[{now:%H:%M:%S}] 非交易时段，休眠 60s")
                time.sleep(60)
                continue
            do_push = (time.time() - last_push) >= config.PUSH_INTERVAL
            snaps, scored, bd = one_round(hist, pusher, push=(do_push and args.push),
                                          dry=args.dry_run, watcher=watcher)
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
