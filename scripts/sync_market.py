#!/usr/bin/env python3
# This script is intentionally dependency-free so GitHub Actions can run it on stock Python.
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.environ.get(
    "WORKER_BASE_URL",
    "https://lively-tooth-f895.shlee123.workers.dev",
).rstrip("/")
RETRIES = int(os.environ.get("FETCH_RETRIES", "3"))
TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STOCK_DIR = DATA_DIR / "stocks"


def fetch_json(path: str):
    url = f"{BASE_URL}{path}"
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": "market-data-mirror/1.0"})
            with urlopen(req, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                raw = resp.read().decode("utf-8")
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError(f"Top-level JSON is not an object for {url}")
                return obj
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Failed after {RETRIES} attempts: {url}: {last_error}")


def validate_market(report: dict):
    for key in ("generated_at", "market", "indices", "watchlist", "source", "version"):
        if key not in report:
            raise ValueError(f"market/report missing required field: {key}")
    if report.get("market") != "US":
        raise ValueError(f"Unexpected market: {report.get('market')}")
    if not isinstance(report["indices"], dict) or not isinstance(report["watchlist"], dict):
        raise ValueError("indices/watchlist must be JSON objects")
    if not report["watchlist"]:
        raise ValueError("watchlist is empty")


def validate_stock(ticker: str, stock: dict):
    required = ("symbol", "asof", "price", "moving_average", "data_quality")
    for key in required:
        if key not in stock:
            raise ValueError(f"{ticker}: missing required field: {key}")
    if str(stock.get("symbol", "")).upper() != ticker:
        raise ValueError(f"{ticker}: symbol mismatch: {stock.get('symbol')}")
    price = stock.get("price")
    if not isinstance(price, dict) or price.get("close") is None:
        raise ValueError(f"{ticker}: price.close missing")


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def main():
    report = fetch_json("/market/report")
    validate_market(report)

    tickers = sorted(str(x).upper() for x in report["watchlist"].keys())
    for ticker in tickers:
        if not re.fullmatch(r"[A-Z0-9.\-]{1,15}", ticker):
            raise ValueError(f"Unsafe ticker in watchlist: {ticker}")

    fetched = {}
    for ticker in tickers:
        stock = fetch_json(f"/stock/{ticker}")
        validate_stock(ticker, stock)
        fetched[ticker] = stock

    status = {
        "sync_status": "success",
        "synced_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "worker_base_url": BASE_URL,
        "worker_version": report.get("version"),
        "market_generated_at": report.get("generated_at"),
        "source": report.get("source"),
        "stock_count": len(fetched),
        "stocks": tickers,
    }

    # Atomic mirror update: all remote fetches and validations must succeed first.
    with tempfile.TemporaryDirectory(prefix="market-mirror-") as tmp:
        tmp_root = Path(tmp)
        write_json(tmp_root / "market_report.json", report)
        write_json(tmp_root / "status.json", status)
        for ticker, stock in fetched.items():
            write_json(tmp_root / "stocks" / f"{ticker}.json", stock)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STOCK_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_root / "market_report.json", DATA_DIR / "market_report.json")
        shutil.copy2(tmp_root / "status.json", DATA_DIR / "status.json")
        for ticker in tickers:
            shutil.copy2(tmp_root / "stocks" / f"{ticker}.json", STOCK_DIR / f"{ticker}.json")

        # Remove stale stock files no longer present in the Worker watchlist.
        expected = {f"{ticker}.json" for ticker in tickers}
        for path in STOCK_DIR.glob("*.json"):
            if path.name not in expected:
                path.unlink()

    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
