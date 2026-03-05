#!/usr/bin/env python3
"""
monitor.py — QQQ/TQQQ Rotation Strategy Monitor

ENTRY signal (fires once when entering a trade):
  Condition A: VIX > 40    AND  TQQQ ≥ 50% below all-time high
  Condition B: 28 < VIX ≤ 40  AND  TQQQ ≥ 75% below all-time high

EXIT signal (fires once on crossover, only if currently in a trade):
  TQQQ crosses BELOW its 30-day moving average (above→below crossover).

State is persisted in state.json, which is auto-committed back to the repo
by the GitHub Actions workflow after each run.
"""

import json
import os
import sys
import time
import logging
from datetime import date
from pathlib import Path

import yfinance as yf
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Strategy parameters ────────────────────────────────────────────────────────
VIX_HIGH_THRESHOLD = 40.0   # VIX above this → 50% TQQQ drawdown threshold
VIX_MID_LOW        = 28.0   # Lower bound for mid-tier VIX entry
VIX_MID_HIGH       = 40.0   # Upper bound for mid-tier VIX entry
TQQQ_DD_HIGH_VIX   = 50.0   # % below ATH required when VIX > 40
TQQQ_DD_MID_VIX    = 75.0   # % below ATH required when 28 < VIX ≤ 40
MA_PERIOD          = 30     # Days for TQQQ moving average

# ── Paths & network ───────────────────────────────────────────────────────────
STATE_FILE  = Path(__file__).parent / "state.json"
RETRIES     = 3
RETRY_DELAY = 5   # seconds (multiplied by attempt number)


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_STATE: dict = {
    "in_trade":        False,   # True after ENTRY fires; False after EXIT fires
    "tqqq_above_30ma": None,    # None = first run, no prior data
    "entry_date":      None,
    "exit_date":       None,
    "last_run":        None,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return {**DEFAULT_STATE, **json.load(f)}
    logging.info("No state.json found — using defaults (first run).")
    return DEFAULT_STATE.copy()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
    logging.info("State saved: %s", state)


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _download(ticker: str, period: str, auto_adjust: bool = True) -> pd.Series:
    for attempt in range(1, RETRIES + 1):
        try:
            logging.info("Fetching %s period=%s (attempt %d)…", ticker, period, attempt)
            df = yf.download(ticker, period=period, progress=False, auto_adjust=auto_adjust)
            if df.empty:
                raise RuntimeError(f"No data returned for {ticker}")
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if close.empty:
                raise RuntimeError(f"'Close' column empty for {ticker}")
            return close
        except Exception as exc:
            logging.warning("Attempt %d failed for %s: %s", attempt, ticker, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


def fetch_vix() -> float:
    close = _download("^VIX", period="5d", auto_adjust=False)
    val = float(close.iloc[-1])
    logging.info("  VIX = %.2f  (date: %s)", val, close.index[-1].date())
    return val


def fetch_tqqq() -> dict:
    """
    Returns a dict with:
      current    – latest closing price
      ath        – all-time high (split-adjusted, full history)
      dd_pct     – drawdown from ATH as a negative % (e.g. -55.0)
      ma_30      – 30-day simple moving average of closing price
      above_ma   – True if current > ma_30
    """
    close = _download("TQQQ", period="max", auto_adjust=True)

    if len(close) < MA_PERIOD:
        raise RuntimeError(f"Insufficient TQQQ history ({len(close)} rows, need ≥ {MA_PERIOD})")

    current  = float(close.iloc[-1])
    ath      = float(close.max())
    dd_pct   = (current - ath) / ath * 100      # negative = below ATH
    ma_30    = float(close.iloc[-MA_PERIOD:].mean())
    above_ma = current > ma_30

    logging.info(
        "  TQQQ = $%.2f  |  ATH = $%.2f  |  DD = %.2f%%  |  30MA = $%.2f  |  Above MA: %s",
        current, ath, dd_pct, ma_30, above_ma,
    )
    return {
        "current":  current,
        "ath":      ath,
        "dd_pct":   dd_pct,
        "ma_30":    ma_30,
        "above_ma": above_ma,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Signal logic
# ─────────────────────────────────────────────────────────────────────────────

def check_entry(vix: float, dd_pct: float) -> tuple[bool, str]:
    """
    Returns (should_enter, reason_string).
    dd_pct is negative (e.g. -55.0 means 55% below ATH).
    """
    abs_dd = abs(dd_pct)

    if vix > VIX_HIGH_THRESHOLD and abs_dd >= TQQQ_DD_HIGH_VIX:
        return (
            True,
            f"VIX {vix:.1f} > {VIX_HIGH_THRESHOLD:.0f}  &  "
            f"TQQQ −{abs_dd:.1f}% from ATH  (≥ {TQQQ_DD_HIGH_VIX:.0f}%)"
        )

    if VIX_MID_LOW < vix <= VIX_MID_HIGH and abs_dd >= TQQQ_DD_MID_VIX:
        return (
            True,
            f"VIX {vix:.1f} in ({VIX_MID_LOW:.0f}–{VIX_MID_HIGH:.0f}]  &  "
            f"TQQQ −{abs_dd:.1f}% from ATH  (≥ {TQQQ_DD_MID_VIX:.0f}%)"
        )

    return False, ""


def check_exit(above_yesterday: bool | None, above_today: bool) -> bool:
    """True only on the crossover day: above MA yesterday, below MA today."""
    if above_yesterday is None:
        return False   # first run — no prior data, can't detect a crossover
    return above_yesterday and not above_today


# ─────────────────────────────────────────────────────────────────────────────
# Message formatting
# ─────────────────────────────────────────────────────────────────────────────

def build_entry_message(vix: float, tqqq: dict, reason: str) -> str:
    today_str = date.today().strftime("%A, %d %b %Y")
    abs_dd    = abs(tqqq["dd_pct"])
    lines = [
        "🟢 <b>QQQ/TQQQ Rotation — ENTRY SIGNAL</b>",
        f"📅 {today_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "✅ <b>ROTATE: QQQ → TQQQ</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"  VIX                : <b>{vix:.2f}</b>",
        f"  TQQQ Current Price : <b>${tqqq['current']:.2f}</b>",
        f"  TQQQ All-Time High : <b>${tqqq['ath']:.2f}</b>",
        f"  TQQQ Drawdown      : <b>−{abs_dd:.1f}%</b> from ATH  🔴",
        f"  TQQQ 30-day MA     : <b>${tqqq['ma_30']:.2f}</b>",
        "",
        f"📌 <b>Trigger:</b> {reason}",
        "",
        "📋 <b>Action:</b>",
        "  ✅ Switch allocation from QQQ → TQQQ",
        "  ✅ EXIT signal fires when TQQQ crosses",
        "     <b>below</b> its 30-day moving average",
    ]
    return "\n".join(lines)


def build_exit_message(vix: float, tqqq: dict, entry_date: str | None) -> str:
    today_str  = date.today().strftime("%A, %d %b %Y")
    entry_info = f"  Entry date         : {entry_date}" if entry_date else ""
    lines = [
        "🔴 <b>QQQ/TQQQ Rotation — EXIT SIGNAL</b>",
        f"📅 {today_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️  <b>ROTATE: TQQQ → QQQ</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"  VIX                : <b>{vix:.2f}</b>",
        f"  TQQQ Current Price : <b>${tqqq['current']:.2f}</b>",
        f"  TQQQ 30-day MA     : <b>${tqqq['ma_30']:.2f}</b>",
        f"  TQQQ All-Time High : <b>${tqqq['ath']:.2f}</b>",
    ]
    if entry_info:
        lines.append(entry_info)
    lines += [
        "",
        "📌 <b>Trigger:</b> TQQQ crossed <b>below</b> its 30-day MA",
        "   (was above yesterday, below today)",
        "",
        "📋 <b>Action:</b>",
        "  ✅ Close TQQQ position",
        "  ✅ Rotate back to QQQ",
        "  ✅ Monitor for next ENTRY signal",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram delivery
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, data=payload, timeout=15)
            r.raise_for_status()
            logging.info("Telegram: sent OK (HTTP %s)", r.status_code)
            return
        except Exception as exc:
            logging.warning("Attempt %d: Telegram send failed: %s", attempt, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logging.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(2)

    state = load_state()
    logging.info("Loaded state: %s", state)

    try:
        vix  = fetch_vix()
        tqqq = fetch_tqqq()

        above_today     = tqqq["above_ma"]
        above_yesterday = state["tqqq_above_30ma"]   # None on first run

        if not state["in_trade"]:
            # ── Watch for ENTRY ───────────────────────────────────────────────
            should_enter, reason = check_entry(vix, tqqq["dd_pct"])
            if should_enter:
                logging.info("ENTRY signal: %s", reason)
                send_telegram(bot_token, chat_id, build_entry_message(vix, tqqq, reason))
                state["in_trade"]   = True
                state["entry_date"] = str(date.today())
                state["exit_date"]  = None
            else:
                logging.info("No entry conditions met.")

        else:
            # ── Watch for EXIT crossover ──────────────────────────────────────
            if check_exit(above_yesterday, above_today):
                logging.info("EXIT signal — TQQQ crossed below 30MA.")
                send_telegram(
                    bot_token, chat_id,
                    build_exit_message(vix, tqqq, state.get("entry_date")),
                )
                state["in_trade"]  = False
                state["exit_date"] = str(date.today())
            else:
                logging.info(
                    "In trade — no exit crossover.  "
                    "TQQQ above 30MA: yesterday=%s, today=%s",
                    above_yesterday, above_today,
                )

        # Persist rolling state
        state["tqqq_above_30ma"] = above_today
        state["last_run"]        = str(date.today())
        save_state(state)

    except Exception as exc:
        logging.exception("Unhandled error in monitor")
        try:
            send_telegram(bot_token, chat_id, f"⚠️ QQQ/TQQQ monitor error:\n{exc}")
        except Exception:
            logging.exception("Also failed to send error notification to Telegram")
        sys.exit(1)


if __name__ == "__main__":
    main()
