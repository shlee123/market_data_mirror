#!/usr/bin/env python3
# Direct GitHub Actions -> yfinance backend. Cloudflare Worker is not required.
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STOCK_DIR = DATA_DIR / "stocks"

US_INDICES = {
    "sp500": ("S&P 500", "^GSPC"),
    "nasdaq": ("Nasdaq Composite", "^IXIC"),
    "dow": ("Dow Jones Industrial Average", "^DJI"),
}
US_WATCHLIST = {
    "ARM": "Arm Holdings",
    "SNDK": "SanDisk",
    "MRVL": "Marvell Technology",
    "MU": "Micron Technology",
    "NVDA": "NVIDIA",
    "AVGO": "Broadcom",
    "NASA": "Tema Space Innovators ETF",
    "AMKR": "Amkor Technology",
    "AAOI": "Applied Optoelectronics",
    "COHR": "Coherent",
    "AMD": "Advanced Micro Devices",
    "INTC": "Intel",
    "NFLX": "Netflix",
}
TW_INDEX = ("加權指數", "^TWII")
TW_WATCHLIST = {
    "2002": ("中鋼", "2002.TW"),
    "2330": ("台積電", "2330.TW"),
    "2337": ("旺宏", "2337.TW"),
    "2449": ("京元電子", "2449.TW"),
    "2515": ("中工", "2515.TW"),
    "2615": ("萬海", "2615.TW"),
    "3363": ("上詮", "3363.TWO"),
    "6176": ("瑞儀", "6176.TW"),
}

CORPORATE_ACTIONS = {
    "6176": {
        "type": "capital_reduction",
        "description": "減資換股整理期間",
        "last_trading_date": "2026-08-12",
        "last_trading_close": 81.3,
        "suspension_start_date": "2026-08-13",
        "suspension_end_date": "2026-08-21",
        "expected_resume_date": "2026-08-24",
        "price_basis_reset": True,
        "verified_after_resume": False,
        "source_note": (
            "Official schedule override: old shares last traded 2026-08-12 at TWD 81.3; "
            "suspended from 2026-08-13; new shares expected to resume 2026-08-24. "
            "Verify TWSE/MOPS before changing this override."
        ),
    }
}


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def rnd(value, digits=4):
    value = finite(value)
    return None if value is None else round(value, digits)


def pct(a, b):
    a, b = finite(a), finite(b)
    if a is None or b in (None, 0):
        return None
    return rnd((a / b - 1.0) * 100.0, 4)


def iso_date(index_value):
    return pd.Timestamp(index_value).date().isoformat()


def fetch_history(symbol, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            df = yf.Ticker(symbol).history(
                period="1y",
                interval="1d",
                auto_adjust=False,
                actions=True,
                repair=True,
            )
            if df is None or df.empty:
                raise RuntimeError("empty history")
            df = df.copy()
            df = df[df["Close"].notna()]
            if len(df) < 2:
                raise RuntimeError(f"only {len(df)} valid rows")
            return df
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{symbol}: yfinance failed after {retries} attempts: {last_error}")


def detect_split(df):
    if "Stock Splits" in df.columns:
        events = df[df["Stock Splits"].fillna(0) != 0]["Stock Splits"]
        if not events.empty:
            idx = events.index[-1]
            return True, iso_date(idx), rnd(events.iloc[-1], 6), "yfinance_action"

    close = df["Close"].astype(float)
    common = [2, 3, 4, 5, 10, 0.5, 1 / 3, 0.25, 0.2, 0.1]
    for i in range(1, len(close)):
        prev, cur = close.iloc[i - 1], close.iloc[i]
        if prev == 0:
            continue
        ratio = cur / prev
        for target in common:
            if abs(ratio - target) / abs(target) <= 0.12:
                return True, iso_date(close.index[i]), rnd(ratio, 6), "heuristic"
    return False, None, None, None


def largest_gap(df):
    close = df["Close"].astype(float)
    changes = close.pct_change().abs() * 100.0
    changes = changes.dropna()
    if changes.empty:
        return False, None, None
    idx = changes.idxmax()
    value = float(changes.loc[idx])
    return value >= 35.0, rnd(value), iso_date(idx)


def adjusted_divergence(df):
    if "Adj Close" not in df.columns:
        return False
    for _, row in df.tail(60).iterrows():
        close = finite(row.get("Close"))
        adjusted = finite(row.get("Adj Close"))
        if close not in (None, 0) and adjusted is not None:
            if abs(adjusted / close - 1.0) >= 0.05:
                return True
    return False


def corporate_state(code, market_date):
    action = CORPORATE_ACTIONS.get(code)
    if not action:
        return {
            "status": "normal",
            "trading": True,
            "corporate_action_active": False,
            "reason": None,
            "last_trading_date": None,
            "last_trading_close": None,
            "suspension_start_date": None,
            "suspension_end_date": None,
            "expected_resume_date": None,
            "price_basis_reset": False,
            "price_basis_reset_pending": False,
            "technical_analysis_valid": True,
        }

    if market_date < action["expected_resume_date"]:
        return {
            "status": "corporate_action",
            "trading": False,
            "corporate_action_active": True,
            "reason": action["description"],
            "action_type": action["type"],
            "last_trading_date": action["last_trading_date"],
            "last_trading_close": action["last_trading_close"],
            "suspension_start_date": action["suspension_start_date"],
            "suspension_end_date": action["suspension_end_date"],
            "expected_resume_date": action["expected_resume_date"],
            "price_basis_reset": action["price_basis_reset"],
            "price_basis_reset_pending": action["price_basis_reset"],
            "technical_analysis_valid": False,
            "source_note": action["source_note"],
        }

    verified = action["verified_after_resume"]
    return {
        "status": "normal" if verified else "post_corporate_action_verification",
        "trading": True,
        "corporate_action_active": not verified,
        "reason": None if verified else "Post-action price basis requires verification.",
        "action_type": action["type"],
        "last_trading_date": action["last_trading_date"],
        "last_trading_close": action["last_trading_close"],
        "suspension_start_date": action["suspension_start_date"],
        "suspension_end_date": action["suspension_end_date"],
        "expected_resume_date": action["expected_resume_date"],
        "price_basis_reset": action["price_basis_reset"],
        "price_basis_reset_pending": action["price_basis_reset"] and not verified,
        "technical_analysis_valid": verified,
        "source_note": action["source_note"],
    }


def build_record(symbol, name, df, market=None, tw_code=None, market_date=None, is_index=False):
    latest = df.iloc[-1]
    previous = df.iloc[-2]
    close = finite(latest["Close"])
    previous_close = finite(previous["Close"])
    current_volume = finite(latest.get("Volume"))
    previous_volume = finite(previous.get("Volume"))

    moving_average = {}
    for period in (20, 50, 100, 200):
        moving_average[f"sma{period}"] = (
            rnd(df["Close"].tail(period).mean()) if len(df) >= period else None
        )

    average_volume_20 = (
        finite(df["Volume"].tail(20).mean())
        if "Volume" in df.columns and len(df) >= 20
        else None
    )

    split_detected, split_date, split_ratio, split_source = detect_split(df)
    gap_detected, gap_percent, gap_date = largest_gap(df)
    adj_divergence = adjusted_divergence(df)

    warning = "green"
    notes = []
    if split_detected or (gap_percent is not None and gap_percent >= 50):
        warning = "red"
    elif gap_detected or len(df) < 200 or adj_divergence:
        warning = "yellow"

    if len(df) < 200:
        notes.append("Less than 200 trading days available; SMA200 may be unavailable.")
    if split_detected:
        notes.append(
            f"Split/corporate-action signal detected near {split_date}; verify historical continuity."
        )
    if gap_detected:
        notes.append(f"Large single-day Close gap detected ({gap_percent}%) near {gap_date}.")
    if adj_divergence:
        notes.append(
            "Adjusted Close differs materially from regular Close; verify dividend/corporate-action effects."
        )
    if not notes:
        notes.append("No major data-quality anomaly detected.")

    high_series = df["High"].dropna()
    low_series = df["Low"].dropna()
    high_52w = finite(high_series.max()) if not high_series.empty else None
    low_52w = finite(low_series.min()) if not low_series.empty else None

    asof = iso_date(df.index[-1])
    previous_date = iso_date(df.index[-2])

    record = {
        "symbol": symbol,
        "name": name,
        "currency": None,
        "exchange": None,
        "asof": asof,
        "previous_date": previous_date,
        "price": {
            "close": rnd(close),
            "previous_close": rnd(previous_close),
            "change": rnd(
                close - previous_close
                if close is not None and previous_close is not None
                else None
            ),
            "change_percent": pct(close, previous_close),
        },
        "volume": {
            "current": int(current_volume) if current_volume is not None else None,
            "previous": int(previous_volume) if previous_volume is not None else None,
            "change_percent": pct(current_volume, previous_volume),
            "average_20d": rnd(average_volume_20, 2),
            "vs_average_20d_percent": pct(current_volume, average_volume_20),
        },
        "moving_average": moving_average,
        "distance_to_moving_average_percent": {
            key: pct(close, value) for key, value in moving_average.items()
        },
        "range_52week": {
            "high": rnd(high_52w),
            "low": rnd(low_52w),
            "distance_from_high_percent": pct(close, high_52w),
            "distance_from_low_percent": pct(close, low_52w),
        },
        "data_quality": {
            "history_days": len(df),
            "sma20_valid": len(df) >= 20,
            "sma50_valid": len(df) >= 50,
            "sma100_valid": len(df) >= 100,
            "sma200_valid": len(df) >= 200,
            "possible_split_detected": split_detected,
            "possible_split_date": split_date,
            "possible_split_ratio": split_ratio,
            "possible_split_source": split_source,
            "large_price_gap_detected": gap_detected,
            "largest_gap_percent": gap_percent,
            "largest_gap_date": gap_date,
            "adjusted_close_divergence_detected": adj_divergence,
            "warning_level": warning,
            "notes": notes,
        },
        "summary": {
            "daily_trend": (
                "up" if close > previous_close else "down" if close < previous_close else "flat"
            ),
            "versus_sma50": (
                "unknown"
                if moving_average["sma50"] is None
                else "above"
                if close > moving_average["sma50"]
                else "below"
                if close < moving_average["sma50"]
                else "at"
            ),
            "volume_signal": (
                "unknown"
                if average_volume_20 in (None, 0) or current_volume is None
                else "high volume"
                if current_volume >= average_volume_20 * 1.2
                else "low volume"
                if current_volume <= average_volume_20 * 0.8
                else "normal volume"
            ),
        },
        "source": "yfinance / Yahoo Finance",
        "calculation": "SMA uses daily Close with auto_adjust=False; 52-week range uses daily High/Low.",
    }

    try:
        fast_info = yf.Ticker(symbol).fast_info
        record["currency"] = getattr(fast_info, "currency", None)
        record["exchange"] = getattr(fast_info, "exchange", None)
    except Exception:
        pass

    if market:
        record["market"] = market
    if tw_code:
        record["tw_code"] = tw_code

    if is_index:
        record["volume"] = {
            "current": None,
            "previous": None,
            "change_percent": None,
            "average_20d": None,
            "vs_average_20d_percent": None,
            "note": "Index volume from Yahoo/yfinance is not used as Taiwan market turnover.",
        }
        record["summary"]["volume_signal"] = "not_applicable"

    if tw_code and market_date:
        state = corporate_state(tw_code, market_date)
        record["market_status"] = state

        stale = asof != market_date
        if state["corporate_action_active"] and state["trading"] is False:
            stale = False

        record["data_quality"]["stale_data"] = stale
        record["data_quality"]["market_date"] = market_date
        record["data_quality"]["symbol_asof"] = asof
        record["data_quality"]["corporate_action_active"] = state["corporate_action_active"]

        record["technical_analysis"] = {
            "valid": (not stale) and state["technical_analysis_valid"],
            "status": (
                "suspended"
                if state["price_basis_reset_pending"]
                else "stale_data"
                if stale
                else "valid"
            ),
            "reason": (
                "corporate_action_price_basis_reset_requires_verification"
                if state["price_basis_reset_pending"]
                else "latest_price_not_aligned_with_market_date"
                if stale
                else None
            ),
        }

        if state["corporate_action_active"] and state["trading"] is False:
            record["summary"]["volume_signal"] = "not_applicable"
            record["corporate_action_reference"] = {
                "official_last_trading_date": state["last_trading_date"],
                "official_last_trading_close": state["last_trading_close"],
                "currency": "TWD",
                "suspension_start_date": state["suspension_start_date"],
                "suspension_end_date": state["suspension_end_date"],
                "expected_resume_date": state["expected_resume_date"],
                "reporting_rule": (
                    "Use official_last_trading_close/date in reports. Do not describe vendor-adjusted "
                    "snapshots as actual trades during suspension."
                ),
            }
            record["vendor_adjusted_snapshot"] = {
                "source": "yfinance / Yahoo Finance",
                "asof": asof,
                "close": rnd(close),
                "note": (
                    "Vendor history may be corporate-action adjusted; not treated as an exchange trade "
                    "during suspension."
                ),
            }

    return record


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    failures = {}

    us_indices = {}
    for key, (name, symbol) in US_INDICES.items():
        try:
            us_indices[key] = build_record(symbol, name, fetch_history(symbol))
        except Exception as exc:
            failures[f"US_INDEX:{symbol}"] = str(exc)

    us_watchlist = {}
    for symbol, name in US_WATCHLIST.items():
        try:
            us_watchlist[symbol] = build_record(symbol, name, fetch_history(symbol))
        except Exception as exc:
            failures[f"US:{symbol}"] = str(exc)

    tw_name, tw_symbol = TW_INDEX
    tw_df = fetch_history(tw_symbol)
    tw_index = build_record(
        tw_symbol,
        tw_name,
        tw_df,
        market="Taiwan",
        is_index=True,
    )
    market_date = tw_index["asof"]

    tw_watchlist = {}
    for code, (name, symbol) in TW_WATCHLIST.items():
        try:
            tw_watchlist[code] = build_record(
                symbol,
                name,
                fetch_history(symbol),
                market="Taiwan",
                tw_code=code,
                market_date=market_date,
            )
        except Exception as exc:
            failures[f"TW:{code}:{symbol}"] = str(exc)

    if not us_watchlist:
        raise RuntimeError("all US watchlist downloads failed")
    if not tw_watchlist:
        raise RuntimeError("all Taiwan watchlist downloads failed")

    us_report = {
        "generated_at": generated_at,
        "market": "US",
        "indices": us_indices,
        "watchlist": us_watchlist,
        "source": "yfinance / Yahoo Finance",
        "version": "github-yfinance-1.0",
    }
    tw_report = {
        "generated_at": generated_at,
        "market": "Taiwan",
        "market_date": market_date,
        "indices": {"taiex": tw_index},
        "watchlist": tw_watchlist,
        "source": "yfinance / Yahoo Finance",
        "version": "github-yfinance-1.0",
        "note": (
            "TAIEX volume is not used as market turnover. Corporate-action overrides take precedence "
            "for reporting."
        ),
    }

    status = {
        "sync_status": "success" if not failures else "partial_success",
        "synced_at": generated_at,
        "source": "yfinance / Yahoo Finance",
        "backend": "GitHub Actions",
        "worker_dependency": False,
        "us_watchlist_count": len(US_WATCHLIST),
        "us_success_count": len(us_watchlist),
        "tw_watchlist_count": len(TW_WATCHLIST),
        "tw_success_count": len(tw_watchlist),
        "failures": failures,
        "market_generated_at": generated_at,
        "tw_market_generated_at": generated_at,
    }

    write_json(DATA_DIR / "market_report.json", us_report)
    write_json(DATA_DIR / "tw_market_report.json", tw_report)
    write_json(DATA_DIR / "status.json", status)

    STOCK_DIR.mkdir(parents=True, exist_ok=True)
    for symbol, record in us_watchlist.items():
        write_json(STOCK_DIR / f"{symbol}.json", record)

    expected = {f"{symbol}.json" for symbol in US_WATCHLIST}
    for path in STOCK_DIR.glob("*.json"):
        if path.name not in expected:
            path.unlink()

    print(json.dumps(status, ensure_ascii=False, indent=2))
    if failures:
        print("WARNING: partial failures present", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
