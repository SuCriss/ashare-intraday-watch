# -*- coding: utf-8 -*-
"""持仓监控：止损 / 移动止盈。

**只有确认买入的才告警。** 推送出去的票先进入 pending 状态，
只跟踪价格不打扰；你确认买入（mark.py）后才转 watching，开始告警。

状态机：
  pending   --确认买入-->  watching
  watching  --跌破 -2%-->  stopped   （止损）
  watching  --涨到 +10%--> trailing  （进入移动止盈）
  trailing  --从峰值回撤 1.5%--> taken （止盈）
  watching/trailing --跨日跌破 -5%--> stopped

未确认的 pending 在收盘时由 run.py 汇总成「观察名单」日报。
"""
from dataclasses import dataclass
from datetime import date
import time

import config
import store
import trading_calendar as tc


@dataclass
class Alert:
    code: str
    name: str
    kind: str            # stop_loss / trail / take_profit / next_day_stop
    signal_price: float
    price: float
    pnl: float           # 相对成本价 %
    peak_pnl: float      # 相对成本价的峰值 %
    reason: str


class Watcher:
    def __init__(self):
        self.pos: dict[str, dict] = {}
        self._last_sync = 0.0

    # ---------- 持久化 ----------
    def load(self, quiet=False):
        """加载最近两个交易日内仍有效的跟踪（含 pending，支持跨日）。"""
        since = tc.prev_trading_day(date.today(), 1).isoformat()
        try:
            for r in store.load_watching(since):
                self.pos[r["code"]] = r
        except Exception as e:
            print(f"[监控] 加载持仓失败：{e}")
        if self.pos and not quiet:
            pend = sum(1 for r in self.pos.values() if not r.get("confirmed"))
            print(f"[监控] 恢复 {len(self.pos)} 只跟踪"
                  f"（已确认 {len(self.pos) - pend} / 待确认 {pend}）")

    def register(self, rows, trade_date):
        """新推送的票进入 pending，只跟踪不告警。"""
        added = 0
        for r in rows:
            if r["code"] in self.pos:
                continue
            rec = {
                "trade_date": trade_date, "code": r["code"], "name": r["name"],
                "signal_price": r["price"], "peak": r["price"], "low": r["price"],
                "state": "pending", "confirmed": 0, "note": "等待确认买入",
            }
            self.pos[r["code"]] = rec
            store.upsert_position(trade_date, r["code"], r["name"], r["price"],
                                  r["price"], "pending", "等待确认买入",
                                  confirmed=0, low=r["price"])
            added += 1
        if added:
            print(f"[监控] 新增待确认 {added} 只（不告警，用 mark.py 确认买入）")

    def reload(self, quiet=False):
        self.pos.clear()
        self.load(quiet=quiet)

    def _save(self, rec):
        store.upsert_position(
            rec["trade_date"], rec["code"], rec["name"], rec["signal_price"],
            rec["peak"], rec["state"], rec.get("note", ""),
            confirmed=rec.get("confirmed", 0), low=rec.get("low"))

    # ---------- 主检查 ----------
    def check(self, snaps: dict) -> list[Alert]:
        """只对已确认买入的持仓告警；pending 仅更新峰值/低点。"""
        if not config.WATCH_ENABLED:
            return []

        # 每 60 秒从库里同步一次，这样 mark.py 确认后服务不用重启就能生效
        if time.time() - self._last_sync > 60:
            self.reload(quiet=True)
            self._last_sync = time.time()

        if not self.pos:
            return []

        today = date.today().isoformat()
        alerts: list[Alert] = []
        dirty = set()

        for code, rec in list(self.pos.items()):
            s = snaps.get(code)
            if s is None or s.price <= 0:
                continue
            cost = rec["signal_price"] or 0
            if cost <= 0:
                continue

            # 峰值 / 低点（pending 也要记，供收盘日报用）
            if s.price > (rec.get("peak") or 0):
                rec["peak"] = s.price
                dirty.add(code)
            if rec.get("low") is None or s.price < rec["low"]:
                rec["low"] = s.price
                dirty.add(code)

            if not rec.get("confirmed"):
                continue                       # 未确认买入 → 不告警

            pnl = (s.price / cost - 1) * 100
            peak_pnl = (rec["peak"] / cost - 1) * 100
            state = rec["state"]
            cross_day = rec["trade_date"] < today

            if state == "watching":
                if cross_day and pnl <= config.NEXT_DAY_STOP_PCT:
                    rec["state"] = "stopped"
                    alerts.append(Alert(code, rec["name"], "next_day_stop", cost,
                                        s.price, pnl, peak_pnl,
                                        f"跨日跌破 {config.NEXT_DAY_STOP_PCT}%"))
                elif pnl <= config.STOP_LOSS_PCT:
                    rec["state"] = "stopped"
                    alerts.append(Alert(code, rec["name"], "stop_loss", cost,
                                        s.price, pnl, peak_pnl,
                                        f"跌破止损位 {config.STOP_LOSS_PCT}%"))
                elif pnl >= config.TAKE_PROFIT_PCT:
                    rec["state"] = "trailing"
                    alerts.append(Alert(code, rec["name"], "take_profit", cost,
                                        s.price, pnl, peak_pnl,
                                        f"达到目标 +{config.TAKE_PROFIT_PCT}%，转移动止盈"))
                else:
                    continue

            elif state == "trailing":
                drawdown = (rec["peak"] - s.price) / rec["peak"] * 100 if rec["peak"] else 0
                if drawdown >= config.TRAIL_DRAWDOWN:
                    rec["state"] = "taken"
                    alerts.append(Alert(code, rec["name"], "trail", cost,
                                        s.price, pnl, peak_pnl,
                                        f"从最高 {rec['peak']:.2f} 回撤 {drawdown:.1f}%"))
                else:
                    continue
            else:
                continue

            self._save(rec)

        for code in dirty - {a.code for a in alerts}:
            self._save(self.pos[code])

        return alerts


KIND_TEXT = {
    "stop_loss": "🛑 止损",
    "next_day_stop": "🛑 跨日止损",
    "take_profit": "🎯 达到目标",
    "trail": "📉 移动止盈",
}
