# -*- coding: utf-8 -*-
"""持仓监控：对已推送的股票做止损 / 移动止盈，状态迁移只告警一次。

状态机：
  watching  --跌破 -3%-->  stopped   （止损）
  watching  --涨到 +5%-->  trailing  （进入移动止盈）
  trailing  --从峰值回撤 2%-->  taken （止盈）
  watching/trailing --次日跌破 -5%-->  stopped

每轮用全市场快照直接查表，不额外发请求。
"""
from dataclasses import dataclass
from datetime import date

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
    pnl: float           # 相对信号价 %
    peak_pnl: float      # 相对信号价的峰值 %
    reason: str


class Watcher:
    def __init__(self):
        self.pos: dict[str, dict] = {}
        self.loaded = False

    # ---------- 持久化 ----------
    def load(self):
        """加载最近两个交易日内仍有效的持仓（支持跨日跟踪）。"""
        since = tc.prev_trading_day(date.today(), 1).isoformat()
        try:
            for r in store.load_watching(since):
                self.pos[r["code"]] = r
        except Exception as e:
            print(f"[监控] 加载持仓失败：{e}")
        self.loaded = True
        if self.pos:
            print(f"[监控] 恢复 {len(self.pos)} 只持仓跟踪：{list(self.pos)[:8]}")

    def register(self, rows, trade_date):
        """新推送的股票加入跟踪。"""
        for r in rows:
            if r["code"] in self.pos:
                continue
            rec = {
                "trade_date": trade_date, "code": r["code"], "name": r["name"],
                "signal_price": r["price"], "peak": r["price"],
                "state": "watching", "note": "",
            }
            self.pos[r["code"]] = rec
            store.upsert_position(trade_date, r["code"], r["name"],
                                  r["price"], r["price"], "watching", "")
        if rows:
            print(f"[监控] 新增跟踪 {len(rows)} 只")

    def _save(self, rec, note=""):
        store.upsert_position(rec["trade_date"], rec["code"], rec["name"],
                              rec["signal_price"], rec["peak"], rec["state"], note)

    # ---------- 主检查 ----------
    def check(self, snaps: dict) -> list[Alert]:
        if not config.WATCH_ENABLED or not self.pos:
            return []

        today = date.today().isoformat()
        alerts: list[Alert] = []
        dirty = set()

        for code, rec in list(self.pos.items()):
            s = snaps.get(code)
            if s is None or s.price <= 0:
                continue
            sp = rec["signal_price"] or 0
            if sp <= 0:
                continue

            pnl = (s.price / sp - 1) * 100
            if s.price > (rec["peak"] or 0):
                rec["peak"] = s.price
                dirty.add(code)
            peak_pnl = (rec["peak"] / sp - 1) * 100
            state = rec["state"]

            # 跨日：昨天的持仓今天跌破 -5% 直接判该走了
            cross_day = rec["trade_date"] < today

            if state == "watching":
                if cross_day and pnl <= config.NEXT_DAY_STOP_PCT:
                    rec["state"] = "stopped"
                    alerts.append(Alert(code, rec["name"], "next_day_stop", sp,
                                        s.price, pnl, peak_pnl,
                                        f"跨日跌破 {config.NEXT_DAY_STOP_PCT}%"))
                elif pnl <= config.STOP_LOSS_PCT:
                    rec["state"] = "stopped"
                    alerts.append(Alert(code, rec["name"], "stop_loss", sp,
                                        s.price, pnl, peak_pnl,
                                        f"跌破止损位 {config.STOP_LOSS_PCT}%"))
                elif pnl >= config.TAKE_PROFIT_PCT:
                    rec["state"] = "trailing"
                    alerts.append(Alert(code, rec["name"], "take_profit", sp,
                                        s.price, pnl, peak_pnl,
                                        f"达到目标 +{config.TAKE_PROFIT_PCT}%，转移动止盈"))
                else:
                    continue

            elif state == "trailing":
                drawdown = (rec["peak"] - s.price) / rec["peak"] * 100 if rec["peak"] else 0
                if drawdown >= config.TRAIL_DRAWDOWN:
                    rec["state"] = "taken"
                    alerts.append(Alert(code, rec["name"], "trail", sp,
                                        s.price, pnl, peak_pnl,
                                        f"从最高 {rec['peak']:.2f} 回撤 {drawdown:.1f}%"))
                else:
                    continue
            else:
                continue

            self._save(rec)

        # 只把峰值变化落库，避免每轮写全表
        for code in dirty - {a.code for a in alerts}:
            self._save(self.pos[code])

        return alerts


KIND_TEXT = {
    "stop_loss": "🛑 止损",
    "next_day_stop": "🛑 跨日止损",
    "take_profit": "🎯 达到目标",
    "trail": "📉 移动止盈",
}
