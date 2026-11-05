# -*- coding: utf-8 -*-
"""SQLite 落库：候选池 / 推送记录 / 日 K / 结果收益。

表设计要点：
  candidates  每轮扫描的**全部**候选都存（不只是推出去的），这样才能算覆盖率
  pushes      真正推送出去的（去重与审计）
  daily       个股日 K，用来算 T+N 收益
  outcomes    每个 (信号日, 代码) 的 T+1/T+3/T+5 收益，由 review.py 回填
"""
import json
import os
import sqlite3
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "intraday.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date  TEXT NOT NULL,
  ts          REAL NOT NULL,
  clock       TEXT NOT NULL,
  code        TEXT NOT NULL,
  name        TEXT,
  sector      TEXT,
  price       REAL,
  pct         REAL,
  score       REAL,
  primary_rule TEXT,
  rules       TEXT,
  tags        TEXT,
  reason      TEXT,
  pushed      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cand_date ON candidates(trade_date, code);
CREATE INDEX IF NOT EXISTS idx_cand_rule ON candidates(trade_date, primary_rule);

CREATE TABLE IF NOT EXISTS pushes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date  TEXT NOT NULL,
  ts          REAL NOT NULL,
  clock       TEXT NOT NULL,
  code        TEXT NOT NULL,
  name        TEXT,
  score       REAL,
  primary_rule TEXT,
  price       REAL,
  pct         REAL
);
CREATE INDEX IF NOT EXISTS idx_push_date ON pushes(trade_date, code);

CREATE TABLE IF NOT EXISTS daily (
  code        TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  open        REAL, high REAL, low REAL, close REAL,
  pct         REAL, amount REAL,
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(trade_date);

CREATE TABLE IF NOT EXISTS outcomes (
  trade_date  TEXT NOT NULL,
  code        TEXT NOT NULL,
  base_price  REAL,
  close_pct   REAL,        -- 信号日收盘相对基准
  ret_1d      REAL,        -- T+1 收盘相对基准
  ret_3d      REAL,
  ret_5d      REAL,
  max_1d      REAL,        -- T+1 最高价相对基准（最大浮盈）
  updated_at  REAL,
  PRIMARY KEY (trade_date, code)
);
"""


SCHEMA_EXT = """
CREATE TABLE IF NOT EXISTS positions (
  trade_date   TEXT NOT NULL,
  code         TEXT NOT NULL,
  name         TEXT,
  signal_price REAL,
  peak         REAL,
  state        TEXT,
  note         TEXT,
  updated_at   REAL,
  PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_pos_state ON positions(state);
"""


def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        c.executescript(SCHEMA_EXT)
    return DB_PATH


def save_candidates(trade_date, clock, rows, pushed_codes=None):
    """一轮扫描的全部候选落库。rows 为 run.scan() 的输出。"""
    if not rows:
        return 0
    pushed_codes = pushed_codes or set()
    now = time.time()
    with conn() as c:
        c.executemany(
            """INSERT INTO candidates
               (trade_date, ts, clock, code, name, sector, price, pct, score,
                primary_rule, rules, tags, reason, pushed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                trade_date, now, clock, r["code"], r["name"], r["sector"],
                r["price"], r["pct"], r["score"], r.get("primary", ""),
                json.dumps([h.rule_id for h in r["hits"]], ensure_ascii=False),
                json.dumps(r["tags"], ensure_ascii=False),
                r["reason"], 1 if r["code"] in pushed_codes else 0,
            ) for r in rows],
        )
    return len(rows)


def save_push(trade_date, clock, rows):
    if not rows:
        return
    now = time.time()
    with conn() as c:
        c.executemany(
            """INSERT INTO pushes (trade_date, ts, clock, code, name, score,
                                   primary_rule, price, pct)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(trade_date, now, clock, r["code"], r["name"], r["score"],
              r.get("primary", ""), r["price"], r["pct"]) for r in rows],
        )
        # 同步标记候选
        c.executemany(
            "UPDATE candidates SET pushed=1 WHERE trade_date=? AND code=?",
            [(trade_date, r["code"]) for r in rows],
        )


def upsert_daily(rows):
    """rows: [(code, trade_date, open, high, low, close, pct, amount), ...]"""
    if not rows:
        return
    with conn() as c:
        c.executemany(
            """INSERT INTO daily (code, trade_date, open, high, low, close, pct, amount)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(code, trade_date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, pct=excluded.pct, amount=excluded.amount""",
            rows,
        )


def save_outcome(row: dict):
    with conn() as c:
        c.execute(
            """INSERT INTO outcomes
               (trade_date, code, base_price, close_pct, ret_1d, ret_3d, ret_5d,
                max_1d, updated_at)
               VALUES (:trade_date,:code,:base_price,:close_pct,:ret_1d,:ret_3d,
                       :ret_5d,:max_1d,:updated_at)
               ON CONFLICT(trade_date, code) DO UPDATE SET
                 close_pct=excluded.close_pct, ret_1d=excluded.ret_1d,
                 ret_3d=excluded.ret_3d, ret_5d=excluded.ret_5d,
                 max_1d=excluded.max_1d, updated_at=excluded.updated_at""",
            {**row, "updated_at": time.time()},
        )


def stats_overview():
    with conn() as c:
        return {
            "候选行数": c.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "推送行数": c.execute("SELECT COUNT(*) FROM pushes").fetchone()[0],
            "日K行数": c.execute("SELECT COUNT(*) FROM daily").fetchone()[0],
            "结果行数": c.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0],
            "持仓行数": c.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
            "覆盖交易日": c.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM candidates").fetchone()[0],
        }


# ---------------- 持仓 / 止损 ----------------
def upsert_position(trade_date, code, name, signal_price, peak, state, note=""):
    with conn() as c:
        c.execute(
            """INSERT INTO positions
               (trade_date, code, name, signal_price, peak, state, note, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, code) DO UPDATE SET
                 peak=excluded.peak, state=excluded.state,
                 note=excluded.note, updated_at=excluded.updated_at""",
            (trade_date, code, name, signal_price, peak, state, note, time.time()),
        )


def load_watching(since_date):
    """加载 since_date 之后仍在 watching/trailing 的持仓。"""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM positions WHERE trade_date>=? AND state IN ('watching','trailing')",
            (since_date,))]
