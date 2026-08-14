"""Telegram Morning Brief push — the desk reports for duty before the
operator opens a laptop.

Formats the same build_brief() the UI serves (one source of truth; the
push can never disagree with the page) into compact Telegram HTML and
sends it through the existing bot. Scheduled daily; silent when the bot
is unconfigured rather than noisy about it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def format_brief(b: dict) -> str:
    lines = ["<b>JARVIS Morning Brief</b>"]

    pulse = b.get("market_pulse") or []
    if pulse:
        lines.append("\n<b>Pulse</b>")
        lines.append(" | ".join(
            f"{p['symbol'].replace('/USD', '').replace('=X', '').replace('=F', '')} "
            f"{_pct(p['change_pct'])}" for p in pulse))

    for a in b.get("analog_reads") or []:
        lines.append(
            f"\n<b>{a['symbol']}</b> analogs (n={a['n_analogs']}): "
            f"4h {_pct(a['fwd_4h_median_pct'])} ({a['fwd_4h_up_rate']}% up) · "
            f"1d {_pct(a['fwd_1d_median_pct'])} ({a['fwd_1d_up_rate']}% up) "
            f"<i>history, not prediction</i>")

    gate = b.get("gate_experiment") or {}
    fresh = gate.get("resolved_in_window") or {}
    if fresh:
        lines.append("\n<b>Gate</b> (resolved in window)")
        for arm in ("TRADE", "TENTATIVE", "NO_TRADE"):
            if arm in fresh:
                f = fresh[arm]
                lines.append(f"  {arm}: {f['resolved']} @ {f['win_rate']}% "
                             f"win, {_pct(f['avg_pnl_pct'])}")
    picks = gate.get("new_trade_picks") or []
    if picks:
        lines.append(f"  new TRADE picks: {', '.join(picks[:8])}")

    ext = b.get("positioning_extremes") or []
    if ext:
        lines.append("\n<b>Positioning extremes</b>")
        for e in ext[:4]:
            lines.append(f"  {e['instrument']} {e['spec_pctile_3y']} pctile")

    tw = b.get("threat_transmission") or []
    if tw:
        lines.append("\n<b>Threat pressure</b> (hypotheses)")
        for t in tw[:5]:
            lines.append(f"  {t['instrument']} {t['pressure']} — "
                         f"{t['rule']} [{t['severity']}]")

    book = b.get("book") or {}
    lines.append(f"\n<b>Book</b>: {book.get('open_positions', 0)} open · "
                 f"{book.get('closed_in_window', 0)} closed · "
                 f"realized {book.get('realized_pnl_window') or 0}")

    inc = (b.get("incubator") or {}).get("counts") or {}
    alerts = b.get("alerts") or {}
    lines.append(f"<b>Incubator</b>: {inc.get('incubating', 0)} coins "
                 f"building history · <b>Alerts</b>: "
                 f"{alerts.get('CRITICAL', 0)} crit / "
                 f"{alerts.get('ACTIONABLE', 0)} actionable")

    for r in b.get("releases_today") or []:
        lines.append(f"📅 {r}")
    return "\n".join(lines)


def run() -> dict:
    from jobs.telegram_bot import get_cfg, send
    from lib.morning_brief import build_brief

    token, chat_id = get_cfg()
    if not token or not chat_id:
        return {"skipped": "telegram not configured"}
    text = format_brief(build_brief(24))
    ok = send(token, chat_id, text)
    logger.info(f"[BriefPush] sent={bool(ok)} ({len(text)} chars)")
    return {"sent": bool(ok), "chars": len(text)}
