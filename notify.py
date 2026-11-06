# -*- coding: utf-8 -*-
"""多渠道推送：飞书 / 钉钉 / 企业微信 / Telegram / Bark / 自定义 Webhook / 控制台。

设计：业务层只产出**渠道无关**的 Card，各渠道自己渲染。
加一个新渠道 = 写一个 Channel 子类 + 在 config.CHANNELS 里加一行，业务代码不动。

各渠道限制（2026-11 实测文档口径）：
  飞书自定义机器人   100 条/分钟、5 条/秒
  钉钉自定义机器人   20 条/分钟
  企业微信机器人     20 条/分钟
  Telegram Bot       30 条/秒
所以推送节奏统一由 config.PUSH_INTERVAL 控制，各渠道共享同一份消息。
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import config


# ---------------- 渠道无关的消息模型 ----------------
@dataclass
class Section:
    head: str                 # "1. 科森科技 603626"
    body: str                 # "🟢 +6.38%　现价 27.52　评分 100"
    tags: list[str] = field(default_factory=list)
    note: str = ""            # "主力净流入2.05亿 占成交16.9%"
    color: str = ""           # red / green，用于支持的渠道


@dataclass
class Card:
    kind: str                 # signal | alert
    title: str
    color: str = "grey"       # red / orange / green / grey / blue
    sections: list[Section] = field(default_factory=list)
    footer: str = ""
    at_all: bool = False


# ---------------- 渠道基类 ----------------
class Channel:
    type = "base"
    max_bytes = 20000

    def __init__(self, **kw):
        self.cfg = kw
        self.name = kw.get("name") or self.type

    @property
    def enabled(self) -> bool:
        raise NotImplementedError

    def render(self, card: Card) -> dict:
        raise NotImplementedError

    def post(self, payload, url=None) -> tuple[bool, str]:
        raise NotImplementedError

    def send(self, card: Card, dry_run=False) -> tuple[bool, str]:
        if dry_run:
            print(f"[dry-run] {self.name} 渲染结果：")
            print(json.dumps(self.render(card), ensure_ascii=False, indent=2)[:2000])
            return False, "dry_run"
        try:
            return self.post(self.render(card))
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


def _http_post(url: str, payload: dict, timeout=10) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = (r.read() or b"{}").decode("utf-8", "replace")
        return True, text[:300]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _strip_md(s: str) -> str:
    """去掉 markdown 标记，给纯文本渠道用。"""
    return s.replace("**", "").replace("`", "").replace("###", "")


def _plain(card: Card) -> str:
    """纯文本渲染，给 Bark / 控制台 / 自定义 Webhook 用。"""
    out = [_strip_md(card.title), ""]
    for s in card.sections:
        out.append(_strip_md(f"{s.head} {s.body}"))
        if s.tags:
            out.append("  " + " · ".join(s.tags))
        if s.note:
            out.append("  " + _strip_md(s.note))
        out.append("")
    if card.footer:
        out.append(_strip_md(card.footer))
    return "\n".join(out)


def _md(card: Card, bold="**", bullet="") -> str:
    """Markdown 渲染，钉钉/企业微信/Telegram 共用。"""
    out = [f"### {card.title}", ""]
    for s in card.sections:
        out.append(f"{bold}{s.head}{bold}　{s.body}")
        if s.tags:
            out.append(f"> {' · '.join(s.tags)}")
        if s.note:
            out.append(f"> {s.note}")
        out.append("")
    if card.footer:
        out.append(f"{card.footer}")
    text = "\n".join(out)
    # 部分渠道对 --- 支持不一致，这里不加分隔线
    return text


def _clip(text: str, max_bytes: int) -> str:
    b = text.encode("utf-8")
    if len(b) <= max_bytes:
        return text
    return b[: max_bytes - 20].decode("utf-8", "ignore") + "\n…（已截断）"


# ---------------- 飞书 ----------------
class FeishuChannel(Channel):
    type = "feishu"
    COLOR_MAP = {"red": "red", "orange": "orange", "green": "green",
                 "grey": "grey", "blue": "blue"}

    @property
    def enabled(self):
        return bool(self.cfg.get("webhook"))

    def render(self, card: Card) -> dict:
        elements = []
        if card.at_all:
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md", "content": "<at id=all></at>"}})
        if not card.sections:
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md", "content": "本轮无符合条件的标的。"}})
        for i, s in enumerate(card.sections, 1):
            parts = [f"**{s.head}**　{s.body}"]
            if s.tags:
                parts.append(" ".join(f"`{t}`" for t in s.tags[:5]))
            if s.note:
                parts.append(f"<font color='grey'>{s.note}</font>")
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md", "content": "\n".join(parts)}})
            if i < len(card.sections):
                elements.append({"tag": "hr"})
        if card.footer:
            elements.append({"tag": "note",
                             "elements": [{"tag": "plain_text", "content": card.footer}]})

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": self.COLOR_MAP.get(card.color, "grey"),
                    "title": {"tag": "plain_text", "content": card.title},
                },
                "elements": elements,
            },
        }
        secret = self.cfg.get("secret")
        if secret:
            ts = int(time.time())
            string_to_sign = f"{ts}\n{secret}"
            digest = hmac.new(string_to_sign.encode("utf-8"), b"",
                              digestmod=hashlib.sha256).digest()
            payload["timestamp"] = str(ts)
            payload["sign"] = base64.b64encode(digest).decode("utf-8")
        return payload

    def post(self, payload, url=None):
        ok, text = _http_post(self.cfg["webhook"], payload)
        try:
            d = json.loads(text)
            ok = d.get("code") == 0 or d.get("StatusCode") == 0
        except Exception:
            pass
        return ok, text


# ---------------- 钉钉 ----------------
class DingTalkChannel(Channel):
    type = "dingtalk"
    max_bytes = 18000

    @property
    def enabled(self):
        return bool(self.cfg.get("webhook"))

    def _url(self):
        url = self.cfg["webhook"]
        secret = self.cfg.get("secret")
        if not secret:
            return url
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                          digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def render(self, card: Card) -> dict:
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": card.title,
                "text": _clip(_md(card), self.max_bytes),
            },
            "at": {"isAtAll": bool(card.at_all)},
        }

    def post(self, payload, url=None):
        ok, text = _http_post(self._url(), payload)
        try:
            ok = json.loads(text).get("errcode") == 0
        except Exception:
            pass
        return ok, text


# ---------------- 企业微信 ----------------
class WeComChannel(Channel):
    type = "wecom"
    max_bytes = 3800

    @property
    def enabled(self):
        return bool(self.cfg.get("webhook"))

    def render(self, card: Card) -> dict:
        return {"msgtype": "markdown",
                "markdown": {"content": _clip(_md(card), self.max_bytes)}}

    def post(self, payload, url=None):
        ok, text = _http_post(self.cfg["webhook"], payload)
        try:
            ok = json.loads(text).get("errcode") == 0
        except Exception:
            pass
        return ok, text


# ---------------- Telegram ----------------
class TelegramChannel(Channel):
    type = "telegram"
    max_bytes = 3800

    @property
    def enabled(self):
        return bool(self.cfg.get("token") and self.cfg.get("chat_id"))

    def render(self, card: Card) -> dict:
        return {
            "chat_id": self.cfg["chat_id"],
            "text": _clip(_md(card, bold="*"), self.max_bytes),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

    def post(self, payload, url=None):
        u = f"https://api.telegram.org/bot{self.cfg['token']}/sendMessage"
        ok, text = _http_post(u, payload)
        try:
            ok = json.loads(text).get("ok") is True
        except Exception:
            pass
        return ok, text


# ---------------- Bark（iOS 推送） ----------------
class BarkChannel(Channel):
    type = "bark"

    @property
    def enabled(self):
        return bool(self.cfg.get("url"))

    def render(self, card: Card) -> dict:
        base = self.cfg["url"].rstrip("/")
        title = urllib.parse.quote(card.title)
        body = urllib.parse.quote(_plain(card)[:1000])
        return {"url": f"{base}/{title}/{body}?group=stock&level=timeSensitive"}

    def post(self, payload, url=None):
        req = urllib.request.Request(payload["url"])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                text = (r.read() or b"{}").decode("utf-8", "replace")
            return True, text[:300]
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


# ---------------- 自定义 Webhook ----------------
class WebhookChannel(Channel):
    """POST 一份结构化 JSON，方便接自建服务 / n8n / 企业微信中转。"""
    type = "webhook"

    @property
    def enabled(self):
        return bool(self.cfg.get("url"))

    def render(self, card: Card) -> dict:
        return {
            "kind": card.kind,
            "title": card.title,
            "color": card.color,
            "text": _plain(card),
            "sections": [
                {"head": s.head, "body": s.body, "tags": s.tags,
                 "note": s.note, "color": s.color}
                for s in card.sections
            ],
            "footer": card.footer,
            "ts": time.time(),
        }

    def post(self, payload, url=None):
        return _http_post(self.cfg["url"], payload)


# ---------------- 控制台 ----------------
class ConsoleChannel(Channel):
    type = "console"

    @property
    def enabled(self):
        return True

    def render(self, card: Card) -> dict:
        return {"text": _plain(card)}

    def post(self, payload, url=None):
        print("─" * 60)
        print(payload["text"])
        print("─" * 60)
        return True, "console"

    def send(self, card: Card, dry_run=False) -> tuple[bool, str]:
        """控制台渠道始终直接打印文本，dry-run 时也一样，便于人眼看效果。"""
        return self.post(self.render(card))


CHANNEL_CLASSES = {
    "feishu": FeishuChannel,
    "dingtalk": DingTalkChannel,
    "wecom": WeComChannel,
    "telegram": TelegramChannel,
    "bark": BarkChannel,
    "webhook": WebhookChannel,
    "console": ConsoleChannel,
}


# ---------------- 分发 ----------------
_CHANNELS: list[Channel] | None = None


def load_channels(force=False) -> list[Channel]:
    """按 config.CHANNELS 实例化，跳过未配置的渠道。"""
    global _CHANNELS
    if _CHANNELS is not None and not force:
        return _CHANNELS

    out = []
    for item in config.CHANNELS:
        t = item.get("type")
        cls = CHANNEL_CLASSES.get(t)
        if not cls:
            print(f"[推送] 未知渠道类型：{t}，已跳过")
            continue
        ch = cls(**item)
        if ch.enabled:
            out.append(ch)

    if not out:
        print("[推送] 没有配置任何渠道，自动启用控制台输出")
        out.append(ConsoleChannel())
    else:
        print(f"[推送] 已启用渠道：{', '.join(c.name for c in out)}")
    _CHANNELS = out
    return out


def dispatch(card: Card, dry_run=False) -> dict[str, tuple[bool, str]]:
    """发到所有已启用渠道。单渠道失败不影响其他渠道。"""
    results = {}
    for ch in load_channels():
        ok, detail = ch.send(card, dry_run=dry_run)
        results[ch.name] = (ok, detail)
        if not dry_run and not ok:
            print(f"[推送] {ch.name} 失败：{detail}")
    return results


# ---------------- 消息构造 ----------------
KIND_TEXT = {
    "stop_loss": "🛑 止损",
    "next_day_stop": "🛑 跨日止损",
    "take_profit": "🎯 达到目标",
    "trail": "📉 移动止盈",
}


def build_signal_card(rows: list[dict], breadth: dict, ts: float,
                      regime: dict | None = None) -> Card:
    """盘中候选卡片。rows 为 run.scan() 的输出。"""
    clock = time.strftime("%H:%M", time.localtime(ts))
    n = len(rows)
    up, down = breadth.get("up", 0), breadth.get("down", 0)
    zt, dt_ = breadth.get("limit_up", 0), breadth.get("limit_down", 0)

    if zt >= 60 and up > down * 1.5:
        color, mood = "red", "情绪偏强"
    elif zt >= 30 and up >= down:
        color, mood = "orange", "情绪中性偏多"
    elif down > up * 1.5:
        color, mood = "green", "情绪偏弱"
    else:
        color, mood = "grey", "情绪中性"

    # 市场环境优先于宽度判断
    if regime:
        r = regime.get("state")
        if r == "risk_on":
            color, mood = "red", f"进攻 · {mood}"
        elif r == "weak":
            color, mood = "orange", f"谨慎 · {mood}"
        elif r == "risk_off":
            color, mood = "grey", f"防守 · {mood}"

    footer_text = (f"大盘 {mood}　上涨{up} / 下跌{down}　涨停{zt} / 跌停{dt_}　"
                   f"扫描 {breadth.get('total', 0)} 只 · {clock}")

    sections = []
    for i, r in enumerate(rows, 1):
        sign = "🔴" if r["pct"] < 0 else "🟢"
        tags = list(dict.fromkeys(r["tags"]))[:3]
        # 执行方式来自回测口径，直接影响盈亏，必须显示
        tags.append("⏰尾盘可买" if r.get("entry") == "close" else "⏰次日开盘买")
        # 基本面 + 资金面：让「为什么选它」可复核
        extra = []
        if r.get("pe"):
            extra.append(f"PE {r['pe']:.0f}")
        if r.get("mf_pct"):
            extra.append(f"主力净占 {r['mf_pct']:+.0f}%")
        note = f"{r['reason']}　|　{r['sector']}"
        if extra:
            note += "　|　" + "　".join(extra)
        sections.append(Section(
            head=f"{i}. {r['name']} {r['code']}",
            body=f"{sign} **{r['pct']:+.2f}%**　现价 {r['price']:.2f}　评分 **{r['score']:.0f}**",
            tags=tags,
            note=note,
            color="green" if r["pct"] >= 0 else "red",
        ))

    # 标题随市场状态变化：防守日必须一眼看出「这不是平时的推送」
    r_state = (regime or {}).get("state", "neutral")
    if r_state == "risk_off":
        title = f"⚠️ 大盘弱势 · 候选 {n} 只 · {clock}"
        footer = ("⚠️ 防守模式：上证/创业板跌破MA20 或 涨跌家数失衡。"
                  "信号有效性在此环境未经验证，仅供观察。\n" + footer_text)
    elif r_state == "weak":
        title = f"🟠 大盘谨慎 · 候选 {n} 只 · {clock}"
        footer = "🟠 谨慎模式：已减量推送。\n" + footer_text
    else:
        title = f"🚀 盘中精选 {n} 只 · {clock}"
        footer = footer_text

    return Card(
        kind="signal",
        title=title,
        color=color,
        sections=sections,
        footer=footer,
        at_all=config.FEISHU_AT_ALL,
    )


def build_alert_card(alerts: list) -> Card:
    """止损 / 止盈告警卡片。alerts 为 watch.Alert 列表。"""
    clock = time.strftime("%H:%M:%S")
    n = len(alerts)
    stops = sum(1 for a in alerts if a.kind in ("stop_loss", "next_day_stop"))

    sections = []
    for a in alerts:
        sections.append(Section(
            head=f"{KIND_TEXT.get(a.kind, a.kind)}　{a.name} {a.code}",
            body=f"现价 {a.price:.2f}　相对信号价 **{a.pnl:+.2f}%**",
            tags=[],
            note=f"信号价 {a.signal_price:.2f}　峰值 {a.peak_pnl:+.2f}%　{a.reason}",
            color="red" if a.kind in ("stop_loss", "next_day_stop") else "green",
        ))

    return Card(
        kind="alert",
        title=f"⚠️ 持仓预警 {n} 条 · {clock}",
        color="red" if stops else "orange",
        sections=sections,
        footer=f"持仓监控 · {n} 条告警 · {clock}",
        at_all=config.FEISHU_AT_ALL and stops > 0,
    )


def build_watchlist_card(rows: list[dict], trade_date: str) -> Card:
    """收盘观察名单：今日推送但未确认买入的票，收盘表现如何。

    这类消息是「降级」的——不打扰盘中，只在收盘后给一份战绩，
    用来验证策略有没有效；要真正纳入监控得先 mark.py 确认买入。
    """
    n = len(rows)
    hit = sum(1 for r in rows if r["pnl"] > 0)
    rate = hit / n * 100 if n else 0

    sections = []
    for i, r in enumerate(rows, 1):
        sign = "🔴" if r["pnl"] < 0 else "🟢"
        sections.append(Section(
            head=f"{i}. {r['name']} {r['code']}",
            body=(f"{sign} **{r['pnl']:+.2f}%**　"
                  f"信号价 {r['signal_price']:.2f} → 收盘 {r['close']:.2f}"),
            tags=[],
            note=(f"日内最低 {r['low_pnl']:+.2f}%　最高 {r['peak_pnl']:+.2f}%　"
                  f"若按 -{abs(config.STOP_LOSS_PCT):.0f}% 止损："
                  f"{'已止损' if r['low_pnl'] <= config.STOP_LOSS_PCT else '未触发'}"),
            color="green" if r["pnl"] >= 0 else "red",
        ))

    return Card(
        kind="watchlist",
        title=f"📋 收盘观察名单 {n} 只 · {trade_date}",
        color="blue" if rate >= 50 else "grey",
        sections=sections,
        footer=(f"未确认买入，只记录不告警　命中 {hit}/{n} = {rate:.0f}%　"
                f"要纳入监控：python3 mark.py <代码>"),
        at_all=False,
    )


def build_health_card(kind: str, info: dict) -> Card:
    """系统健康通知：启动就绪 / 长时间静默 / 推送失败。

    目的：让「系统挂了」这件事变得可见。之前完全静默的设计，
    用户没法区分「今天不该推」和「程序早就死了」。
    """
    clock = time.strftime("%H:%M:%S")

    if kind == "startup":
        sec = [
            Section(head="版本", body=str(info.get("version", "")), tags=[], note=""),
            Section(head="大盘", body=str(info.get("env", "")), tags=[], note=""),
            Section(head="市场环境", body=str(info.get("regime", "")), tags=[], note=""),
            Section(head="数据就绪",
                    body=(f"概念板块 {info.get('boards', 0)} 个　"
                          f"日线缓存 {info.get('stocks', 0)} 只　"
                          f"策略 {info.get('rules', 0)} 条　"
                          f"业绩预告 {info.get('forecasts', 0)} 只"),
                    tags=[], note=""),
        ]
        return Card(kind="health", title=f"✅ 系统就绪 · {clock}", color="blue",
                    sections=sec, footer="这条消息说明盯盘已启动，接下来按信号推送。")

    if kind == "silence":
        mins = info.get("minutes", 0)
        sec = [Section(
            head="⚠️ 长时间没有推送",
            body=f"已 {mins} 分钟没有任何成功推送",
            tags=[],
            note=(f"最近错误：{info.get('last_error', '无')}　"
                  f"本轮候选：{info.get('candidates', 0)} 只"),
            color="red")]
        return Card(kind="health", title=f"⚠️ 静默告警 · {clock}", color="orange",
                    sections=sec,
                    footer="可能是：市场防守模式 / 上游接口故障 / 推送渠道异常。请检查日志。")

    # error
    sec = [Section(head=f"推送失败 {info.get('count', 0)} 次",
                   body=str(info.get("detail", ""))[:300],
                   tags=[], note="", color="red")]
    return Card(kind="health", title=f"❌ 推送异常 · {clock}", color="red",
                sections=sec, footer="检查 .env 里的渠道配置与网络。")


def build_push_summary_card(rows: list[dict], trade_date: str, total: int) -> Card:
    """收盘后：今日推送的个股与次数。

    rows 来自 store.push_summary()，已按推送次数降序。
    """
    n_stock = len(rows)
    if n_stock == 0:
        return Card(kind="summary",
                    title=f"📊 今日推送统计 · {trade_date}",
                    color="grey",
                    sections=[Section(head="今日无推送",
                                      body="没有标的通过全部筛选条件",
                                      tags=[], note="")],
                    footer="可能是市场防守、候选不足，或推送渠道异常。")

    # 次数分布
    dist: dict[int, int] = {}
    for r in rows:
        dist[r["n"]] = dist.get(r["n"], 0) + 1
    dist_txt = "　".join(f"{k}次×{v}只" for k, v in sorted(dist.items()))

    sections = []
    show = rows[:15]
    for i, r in enumerate(show, 1):
        chg = (r["last_p"] / r["first_p"] - 1) * 100 if r["first_p"] else 0.0
        mark = "🔴" if chg < 0 else "🟢"
        span = r["first_t"] if r["first_t"] == r["last_t"] else \
            f"{r['first_t']} → {r['last_t']}"
        tags = list(r["rules"])[:3]
        if r["n"] > 1:
            tags.insert(0, f"{r['n']}次")
        sections.append(Section(
            head=f"{i}. {r['name']} {r['code']}",
            body=(f"**{r['n']} 次**　{span}　"
                  f"{r['first_p']:.2f} → {r['last_p']:.2f}　{mark} **{chg:+.2f}%**"),
            tags=tags,
            note="",
            color="green" if chg >= 0 else "red",
        ))

    if n_stock > len(show):
        sections.append(Section(
            head=f"…另有 {n_stock - len(show)} 只只推过 1 次",
            body="", tags=[], note=""))

    avg = total / n_stock
    top = rows[0]
    return Card(
        kind="summary",
        title=f"📊 今日推送统计 · {trade_date}",
        color="blue",
        sections=sections,
        footer=(f"共 {total} 次 / {n_stock} 只　平均 {avg:.1f} 次/只　"
                f"最多 {top['name']} {top['n']} 次\n"
                f"分布：{dist_txt}"),
    )


def test_push(dry_run=False):
    """发一条测试消息到所有已启用渠道，用来验证配置。"""
    card = Card(
        kind="test",
        title="🧪 推送通道测试",
        color="blue",
        sections=[
            Section(head="1. 测试标的 000000",
                    body="🟢 **+3.21%**　现价 12.34　评分 **88**",
                    tags=["放量突破", "测试标签"],
                    note="这条消息用来确认渠道配置是否正确"),
        ],
        footer=f"推送测试 · {time.strftime('%Y-%m-%d %H:%M:%S')}",
    )
    res = dispatch(card, dry_run=dry_run)
    print("\n渠道结果：")
    for name, (ok, detail) in res.items():
        print(f"  {'✅' if ok else '❌'} {name:<10} {detail[:120]}")
    return res
