from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import statistics
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


BINANCE_BASE_URL = "https://fapi.binance.com"
TELEGRAM_BASE_URL = "https://api.telegram.org"
DB_PATH = "signals.db"
LOCAL_TZ = ZoneInfo("Europe/Istanbul")
TURKISH_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    scan_interval_seconds: int = 300
    max_symbols_to_analyze: int = 0  # 0 = sinirsiz, tum uygun pariteleri tara
    min_quote_volume_usdt: float = 50_000_000
    min_confidence: float = 86
    signal_cooldown_minutes: int = 240
    max_signals_per_symbol_per_day: int = 2
    max_same_direction_signals: int = 5
    same_direction_window_minutes: int = 180
    position_check_interval_seconds: int = 8
    announce_empty_scans: bool = False
    binance_timeout_seconds: int = 12
    log_level: str = "INFO"

    @staticmethod
    def load() -> "Config":
        load_dotenv(".env")
        return Config(
            telegram_bot_token=get_required_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get_required_env("TELEGRAM_CHAT_ID"),
            scan_interval_seconds=get_int_env("SCAN_INTERVAL_SECONDS", 300),
            max_symbols_to_analyze=get_int_env("MAX_SYMBOLS_TO_ANALYZE", 0),
            min_quote_volume_usdt=get_float_env("MIN_QUOTE_VOLUME_USDT", 50_000_000),
            min_confidence=get_float_env("MIN_CONFIDENCE", 86),
            signal_cooldown_minutes=get_int_env("SIGNAL_COOLDOWN_MINUTES", 240),
            max_signals_per_symbol_per_day=get_int_env("MAX_SIGNALS_PER_SYMBOL_PER_DAY", 2),
            max_same_direction_signals=get_int_env("MAX_SAME_DIRECTION_SIGNALS", 5),
            same_direction_window_minutes=get_int_env("SAME_DIRECTION_WINDOW_MINUTES", 180),
            position_check_interval_seconds=get_int_env("POSITION_CHECK_INTERVAL_SECONDS", 8),
            announce_empty_scans=get_bool_env("ANNOUNCE_EMPTY_SCANS", False),
            binance_timeout_seconds=get_int_env("BINANCE_TIMEOUT_SECONDS", 12),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float


@dataclass
class MarketSymbol:
    symbol: str
    quote_volume: float
    price_change_percent: float
    last_price: float


@dataclass
class BtcHealth:
    status: str
    direction: str
    score: float
    volatility: float
    details: list[str]
    pct_change_24h: float = 0.0


@dataclass
class Signal:
    symbol: str
    side: str
    confidence: float
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    leverage: int
    risk_reward: float
    btc_status: str
    reasons: list[str]


class HttpClient:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers or {"User-Agent": "professional-signal-bot/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


class MacroClient:
    """Fetches the S&P 500 and NASDAQ Composite intraday direction as a proxy for
    "is there a broad risk-on/risk-off macro flow happening right now" — ETF-driven
    Bitcoin moves are correlated with this, which is the exact whipsaw pattern
    reported (crypto reversing hard when US equities open and move).

    IMPORTANT: this talks to a third-party endpoint (Yahoo Finance's public chart API,
    no key required) that could not be exercised from the development environment this
    bot was written in — that sandbox has no network access, so unlike every other
    piece of this file, this one specific integration could not be tested end-to-end
    before being shipped. It is written defensively so that ANY failure (blocked,
    timed out, response format changed, anything) degrades to "Unknown" and never
    blocks signal generation — worst case, this filter silently does nothing.
    Check /status after deploying to see what it's actually reading.
    """

    def __init__(self, timeout_seconds: int) -> None:
        self.http = HttpClient(timeout_seconds)

    def _fetch_index_pct_change(self, yahoo_ticker: str) -> float | None:
        """Returns the intraday % change for one index, or None if the fetch/parse
        failed for any reason. Never raises."""
        try:
            data = self.http.get_json(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}",
                params={"interval": "5m", "range": "1d"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            meta = data["chart"]["result"][0]["meta"]
            current = meta.get("regularMarketPrice")
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            if not current or not previous:
                return None
            return (current - previous) / previous
        except Exception:
            logging.exception("%s fetch failed (non-fatal, ignored)", yahoo_ticker)
            return None

    @staticmethod
    def _classify_pct_change(change: float | None) -> str:
        if change is None:
            return "Unknown"
        if change > 0.003:
            return "Bullish"
        if change < -0.003:
            return "Bearish"
        return "Mixed"

    def us_equities_detail(self) -> dict[str, str]:
        """Fetches S&P 500 (^GSPC) and NASDAQ Composite (^IXIC) ONCE and returns
        each one's own trend plus the combined verdict, as
        {'spx': ..., 'nasdaq': ..., 'combined': ...} - each value is 'Bullish',
        'Bearish', 'Mixed', or 'Unknown'. Added because the report asked to SEE
        S&P 500 and NASDAQ separately in the status message, not just the merged
        read that us_equities_trend() already used for scoring - this does one
        fetch and serves both needs instead of fetching twice per scan.

        Same weekend-staleness guard as before: skip the fetch entirely on Sat/Sun
        (US market closed) and return 'Unknown' for all three."""
        ny_time = datetime.now(ZoneInfo("America/New_York"))
        if ny_time.weekday() >= 5:  # Saturday=5, Sunday=6 -> US market is closed
            return {"spx": "Unknown", "nasdaq": "Unknown", "combined": "Unknown"}
        spx_change = self._fetch_index_pct_change("%5EGSPC")
        nasdaq_change = self._fetch_index_pct_change("%5EIXIC")
        changes = [c for c in (spx_change, nasdaq_change) if c is not None]
        combined = self._classify_pct_change(sum(changes) / len(changes)) if changes else "Unknown"
        return {
            "spx": self._classify_pct_change(spx_change),
            "nasdaq": self._classify_pct_change(nasdaq_change),
            "combined": combined,
        }

    def us_equities_trend(self) -> str:
        """Returns just the combined verdict from us_equities_detail() - kept as its
        own method since this is what the scoring code calls and it reads clearer
        at each call site than us_equities_detail()['combined']."""
        return self.us_equities_detail()["combined"]

    def crypto_dominance(self) -> dict[str, float] | None:
        """BTC and USDT dominance (each coin's share of total crypto market cap, as
        a percentage) via CoinGecko's free public /global endpoint (no key
        required). Requested explicitly: 'BTC dominansı, USDT dominansı' as two of
        the four things the status message should show.

        Returns {'btc': pct, 'usdt': pct} (e.g. {'btc': 52.3, 'usdt': 4.1}), or None
        if the fetch/parse fails for any reason - never raises. This endpoint gives
        a current SNAPSHOT only, no historical series, so rising/falling is judged
        by comparing consecutive scans against each other (see
        last_btc_dominance_pct / last_usdt_dominance_pct on the engine), the same
        way any other scan-over-scan trend read in this file works - not by this
        method itself.

        Same untestable-from-this-sandbox caveat as the rest of MacroClient: this
        talks to a real third-party API that this development environment has no
        network access to reach, so this specific integration could not be
        exercised end-to-end before shipping. Check /status after deploying."""
        try:
            data = self.http.get_json("https://api.coingecko.com/api/v3/global")
            pct = data["data"]["market_cap_percentage"]
            btc = pct.get("btc")
            usdt = pct.get("usdt")
            if btc is None or usdt is None:
                return None
            return {"btc": float(btc), "usdt": float(usdt)}
        except Exception:
            logging.exception("CoinGecko dominance fetch failed (non-fatal, ignored)")
            return None

    def dxy_trend(self) -> str:
        """US Dollar Index (DXY) trend, read on the DAILY timeframe rather than
        intraday noise — per report, DXY tends to move inversely to risk assets
        (crypto, gold, US equities): DXY breaking below support has historically
        coincided with those breaking up, and the reverse when DXY breaks above
        resistance. Rather than hard-coding specific support/resistance price levels
        (which go stale), this compares the latest daily close against its own 20-day
        and 50-day averages — the same kind of trend-structure read _higher_timeframe_
        trend already does with EMA50/EMA200, just applied to DXY. Returns 'Bearish'
        (DXY falling — supportive for LONG on risk assets), 'Bullish' (DXY rising —
        supportive for SHORT), 'Mixed', or 'Unknown' if the fetch fails. Same
        defensive never-raises, never-blocks-a-trade design as us_equities_trend."""
        try:
            data = self.http.get_json(
                "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
                params={"interval": "1d", "range": "3mo"},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 50:
                return "Unknown"
            price = closes[-1]
            sma20 = statistics.fmean(closes[-20:])
            sma50 = statistics.fmean(closes[-50:])
            if price < sma20 < sma50:
                return "Bearish"
            if price > sma20 > sma50:
                return "Bullish"
            return "Mixed"
        except Exception:
            logging.exception("DXY fetch failed (non-fatal, ignored)")
            return "Unknown"


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout_seconds: int) -> None:
        self.token = token
        self.chat_id = chat_id
        self.http = HttpClient(timeout_seconds)
        self.offset = 0

    def send_message(self, text: str) -> None:
        url = f"{TELEGRAM_BASE_URL}/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except Exception:
            logging.exception("Telegram message failed")

    def delete_webhook(self) -> None:
        """Clears any webhook left configured on this bot token.

        getUpdates (long polling) and a webhook cannot be active at the same time —
        Telegram answers getUpdates with HTTP 409 Conflict for as long as a webhook
        is set, even if nothing else is actually polling right now. Calling this once
        on startup makes sure polling always works regardless of what was configured
        on this token before.
        """
        url = f"{TELEGRAM_BASE_URL}/bot{self.token}/deleteWebhook"
        data = urllib.parse.urlencode({"drop_pending_updates": "false"}).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except Exception:
            logging.exception("Telegram deleteWebhook failed")

    def get_updates(self) -> list[dict[str, Any]]:
        url = f"{TELEGRAM_BASE_URL}/bot{self.token}/getUpdates"
        params = {"timeout": 1, "offset": self.offset}
        try:
            updates = self.http.get_json(url, params)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # Another consumer (a webhook, or a second running instance) is using
                # this token's getUpdates right now. Log one short line instead of a
                # full traceback every 2s, and back off a bit so we don't hammer
                # Telegram while the conflict persists.
                logging.warning("Telegram getUpdates 409 Conflict - baska bir yerde ayni token kullaniliyor olabilir.")
                time.sleep(5)
            else:
                logging.exception("Telegram update polling failed (HTTP %s)", exc.code)
            return []
        except Exception:
            logging.exception("Telegram update polling failed")
            return []
        if not updates.get("ok"):
            return []
        result = updates.get("result", [])
        if result:
            self.offset = max(update["update_id"] for update in result) + 1
        return result


class BinanceFuturesClient:
    def __init__(self, timeout_seconds: int) -> None:
        self.http = HttpClient(timeout_seconds)

    def exchange_symbols(self) -> set[str]:
        data = self.http.get_json(f"{BINANCE_BASE_URL}/fapi/v1/exchangeInfo")
        symbols = set()
        for item in data.get("symbols", []):
            if (
                item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
            ):
                symbols.add(item["symbol"])
        return symbols

    def tickers_24h(self) -> list[MarketSymbol]:
        live_symbols = self.exchange_symbols()
        data = self.http.get_json(f"{BINANCE_BASE_URL}/fapi/v1/ticker/24hr")
        symbols = []
        for item in data:
            symbol = item.get("symbol", "")
            if symbol not in live_symbols:
                continue
            try:
                symbols.append(
                    MarketSymbol(
                        symbol=symbol,
                        quote_volume=float(item["quoteVolume"]),
                        price_change_percent=float(item["priceChangePercent"]),
                        last_price=float(item["lastPrice"]),
                    )
                )
            except (KeyError, ValueError):
                continue
        return symbols

    def klines(self, symbol: str, interval: str, limit: int = 210) -> list[Candle]:
        data = self.http.get_json(
            f"{BINANCE_BASE_URL}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        candles = []
        for row in data:
            candles.append(
                Candle(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=int(row[6]),
                    quote_volume=float(row[7]),
                )
            )
        return candles

    def all_mark_prices(self) -> dict[str, float]:
        """Single lightweight call that returns the latest price for every symbol."""
        data = self.http.get_json(f"{BINANCE_BASE_URL}/fapi/v1/ticker/price")
        prices: dict[str, float] = {}
        for item in data:
            try:
                prices[item["symbol"]] = float(item["price"])
            except (KeyError, ValueError, TypeError):
                continue
        return prices

    def funding_rate(self, symbol: str) -> float | None:
        try:
            data = self.http.get_json(f"{BINANCE_BASE_URL}/fapi/v1/premiumIndex", {"symbol": symbol})
            return float(data["lastFundingRate"])
        except Exception:
            logging.info("Funding unavailable for %s", symbol)
            return None

    def open_interest(self, symbol: str) -> float | None:
        try:
            data = self.http.get_json(f"{BINANCE_BASE_URL}/fapi/v1/openInterest", {"symbol": symbol})
            return float(data["openInterest"])
        except Exception:
            logging.info("Open interest unavailable for %s", symbol)
            return None

    def long_short_ratio(self, symbol: str) -> float | None:
        """Global long/short ACCOUNT ratio (how many accounts are net long vs net
        short) — a very lopsided ratio (crowded long or crowded short) is a
        contrarian, squeeze-risk signal: it's exactly the kind of imbalance that
        precedes a liquidity-sweep reversal. Same Binance API family as
        funding_rate/open_interest above (fapi.binance.com), not a new/different
        service — same reliability profile as those already-working calls."""
        try:
            data = self.http.get_json(
                f"{BINANCE_BASE_URL}/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": "15m", "limit": 1},
            )
            return float(data[0]["longShortRatio"])
        except Exception:
            logging.info("Long/short ratio unavailable for %s", symbol)
            return None


class SignalDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            # Migration: add columns needed for TP/SL tracking to existing databases.
            existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
            migrations = {
                "leverage": "INTEGER NOT NULL DEFAULT 1",
                "status": "TEXT NOT NULL DEFAULT 'OPEN'",
                "tp1_hit": "INTEGER NOT NULL DEFAULT 0",
                "tp2_hit": "INTEGER NOT NULL DEFAULT 0",
                "sl_hit": "INTEGER NOT NULL DEFAULT 0",
                "closed_at": "INTEGER",
                "exit_price": "REAL",
            }
            for column, declaration in migrations.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_side_time ON signals(symbol, side, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    sent_at INTEGER NOT NULL,
                    UNIQUE(report_type, period_key)
                )
                """
            )

    def recently_sent(self, symbol: str, side: str, cooldown_minutes: int) -> bool:
        cutoff = int(time.time()) - cooldown_minutes * 60
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM signals WHERE symbol = ? AND side = ? AND created_at >= ? LIMIT 1",
                (symbol, side, cutoff),
            ).fetchone()
        return row is not None

    def recently_closed(self, symbol: str, cooldown_minutes: int) -> bool:
        """True if this symbol had a position CLOSE (TP2/SL/breakeven) within the
        cooldown window, on either side. The open-time cooldown alone isn't enough:
        if a position stays open longer than the cooldown itself, closing it no
        longer blocks anything, and a new signal can fire on the same coin within
        minutes of the last one resolving. This adds a cooldown from CLOSE time too."""
        cutoff = int(time.time()) - cooldown_minutes * 60
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM signals WHERE symbol = ? AND closed_at IS NOT NULL AND closed_at >= ? LIMIT 1",
                (symbol, cutoff),
            ).fetchone()
        return row is not None

    def signals_today_count(self, symbol: str) -> int:
        """Rolling 24h count of signals sent for a symbol, regardless of side."""
        cutoff = int(time.time()) - 24 * 60 * 60
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE symbol = ? AND created_at >= ?",
                (symbol, cutoff),
            ).fetchone()
        return int(row["n"])

    def same_direction_count(self, side: str, window_minutes: int) -> int:
        """How many signals (across ALL symbols) were sent in this SAME direction
        within the rolling window. A per-cycle cap alone isn't enough: if the same
        macro read (e.g. BTC bearish) holds across several consecutive scans, a fresh
        batch of same-side signals keeps going out each cycle — 12 signals that are
        really just the same directional bet repeated 12 times, so when that one call
        is wrong, all 12 fail together. This caps aggregate exposure to one direction."""
        cutoff = int(time.time()) - window_minutes * 60
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE side = ? AND created_at >= ?",
                (side, cutoff),
            ).fetchone()
        return int(row["n"])

    def save_signal(self, signal: Signal) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signals(symbol, side, confidence, entry, stop_loss, tp1, tp2, leverage, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.symbol,
                    signal.side,
                    signal.confidence,
                    signal.entry,
                    signal.stop_loss,
                    signal.tp1,
                    signal.tp2,
                    signal.leverage,
                    int(time.time()),
                ),
            )
            return int(cursor.lastrowid)

    def total_signals(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
        return int(row["n"])

    def open_positions(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM signals WHERE status = 'OPEN'").fetchall()

    def has_open_position(self, symbol: str) -> bool:
        """True if this symbol already has an unresolved (OPEN) signal — used to stop
        the bot from sending a new signal for a coin until the current one closes via
        TP2, SL, or breakeven."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM signals WHERE symbol = ? AND status = 'OPEN' LIMIT 1",
                (symbol,),
            ).fetchone()
        return row is not None

    def mark_tp1_hit(self, signal_id: int) -> None:
        # Move the stop to breakeven (entry price) once TP1 is hit. If price later
        # reverses, the position closes near entry instead of at the original SL —
        # so a trade that already banked TP1 profit can no longer turn into a full loss.
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET tp1_hit = 1, stop_loss = entry WHERE id = ?",
                (signal_id,),
            )

    def mark_tp2_hit(self, signal_id: int, exit_price: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET tp2_hit = 1, status = 'TP2', closed_at = ?, exit_price = ? WHERE id = ?",
                (int(time.time()), exit_price, signal_id),
            )

    def mark_sl_hit(self, signal_id: int, exit_price: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET sl_hit = 1, status = 'SL', closed_at = ?, exit_price = ? WHERE id = ?",
                (int(time.time()), exit_price, signal_id),
            )

    def mark_breakeven_hit(self, signal_id: int, exit_price: float) -> None:
        """Closed via the post-TP1 breakeven stop — not a real loss, TP1 profit stands."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET sl_hit = 1, status = 'BE', closed_at = ?, exit_price = ? WHERE id = ?",
                (int(time.time()), exit_price, signal_id),
            )

    def performance_summary(self) -> tuple[int, int, int]:
        """Returns (tp2_closed, sl_closed, still_open) counts for basic win-rate tracking."""
        with self._connect() as conn:
            tp2 = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status = 'TP2'").fetchone()["n"]
            sl = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status = 'SL'").fetchone()["n"]
            open_count = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status = 'OPEN'").fetchone()["n"]
        return int(tp2), int(sl), int(open_count)

    def signals_between(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM signals WHERE created_at >= ? AND created_at <= ? ORDER BY created_at",
                (start_ts, end_ts),
            ).fetchall()

    def report_already_sent(self, report_type: str, period_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM report_log WHERE report_type = ? AND period_key = ? LIMIT 1",
                (report_type, period_key),
            ).fetchone()
        return row is not None

    def mark_report_sent(self, report_type: str, period_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO report_log(report_type, period_key, sent_at) VALUES (?, ?, ?)",
                (report_type, period_key, int(time.time())),
            )


class IndicatorEngine:
    @staticmethod
    def ema(values: list[float], period: int) -> list[float]:
        if not values:
            return []
        alpha = 2 / (period + 1)
        result = [values[0]]
        for value in values[1:]:
            result.append(value * alpha + result[-1] * (1 - alpha))
        return result

    @staticmethod
    def rsi(values: list[float], period: int = 14) -> float:
        if len(values) <= period:
            return 50.0
        gains = []
        losses = []
        for current, previous in zip(values[-period:], values[-period - 1 : -1]):
            change = current - previous
            gains.append(max(change, 0))
            losses.append(abs(min(change, 0)))
        avg_gain = statistics.fmean(gains) if gains else 0
        avg_loss = statistics.fmean(losses) if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(values: list[float]) -> tuple[float, float, float]:
        if len(values) < 35:
            return 0.0, 0.0, 0.0
        ema12 = IndicatorEngine.ema(values, 12)
        ema26 = IndicatorEngine.ema(values, 26)
        macd_line = [a - b for a, b in zip(ema12[-len(ema26) :], ema26)]
        signal_line = IndicatorEngine.ema(macd_line, 9)
        histogram = macd_line[-1] - signal_line[-1]
        return macd_line[-1], signal_line[-1], histogram

    @staticmethod
    def atr(candles: list[Candle], period: int = 14) -> float:
        if len(candles) <= period:
            return 0.0
        true_ranges = []
        for candle, previous in zip(candles[-period:], candles[-period - 1 : -1]):
            true_ranges.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - previous.close),
                    abs(candle.low - previous.close),
                )
            )
        return statistics.fmean(true_ranges)

    @staticmethod
    def adx(candles: list[Candle], period: int = 14) -> float:
        """ADX FIX: this used to compute a single DX (directional index) snapshot
        from one period-length window and return that directly AS 'adx' — but real
        ADX is specifically an AVERAGE of DX over `period`, which is what makes it
        smooth/stable instead of swinging hard candle to candle. The thresholds used
        elsewhere in this file (adx < 20 = not trending, 16-42 = acceptable range)
        are standard ADX conventions, calibrated for that smoothed behavior — applying
        them to a raw single-period DX reading is noisier than intended, and could
        let a market gate open/closed on noise rather than sustained trend strength.
        This now computes DX at each of the last `period` points and averages them."""
        needed = period * 2 + 1
        if len(candles) < needed:
            return 0.0
        recent = candles[-needed:]
        plus_dm = []
        minus_dm = []
        true_ranges = []
        for current, previous in zip(recent[1:], recent[:-1]):
            up_move = current.high - previous.high
            down_move = previous.low - current.low
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
            true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))

        dx_values = []
        for i in range(period, len(plus_dm) + 1):
            window_tr = sum(true_ranges[i - period : i]) or 1
            plus_di = 100 * sum(plus_dm[i - period : i]) / window_tr
            minus_di = 100 * sum(minus_dm[i - period : i]) / window_tr
            di_sum = plus_di + minus_di
            dx_values.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

        if not dx_values:
            return 0.0
        return statistics.fmean(dx_values[-period:])


class ProfessionalSignalEngine:
    def __init__(self, binance: BinanceFuturesClient, config: Config) -> None:
        self.binance = binance
        self.config = config
        self.macro = MacroClient(config.binance_timeout_seconds)
        self.last_scan_summary = "Henüz tarama yapılmadı."
        self.last_us_trend = "Unknown"
        self.last_spx_trend = "Unknown"
        self.last_nasdaq_trend = "Unknown"
        self.last_dxy_trend = "Unknown"
        self.last_btc_dominance_trend = "Unknown"
        self.last_usdt_dominance_trend = "Unknown"
        self.last_btc_dominance_pct: float | None = None
        self.last_usdt_dominance_pct: float | None = None
        self.last_market_condition_summary = "Henüz tarama yapılmadı."

    @staticmethod
    def _dominance_trend(previous: float | None, current: float | None, dead_zone: float = 0.10) -> str:
        """CoinGecko's free /global endpoint is a snapshot, not a historical series,
        so unlike DXY/SPX (daily SMA20 vs SMA50) there's no way to compute a real
        trend from a single call. Instead this compares the current scan's reading
        against the PREVIOUS scan's (stored on the engine) - a dead_zone of 0.10
        percentage points avoids flip-flopping on noise between two back-to-back
        scans. Returns 'Unknown' until there are at least two readings to compare."""
        if previous is None or current is None:
            return "Unknown"
        delta = current - previous
        if delta > dead_zone:
            return "Rising"
        if delta < -dead_zone:
            return "Falling"
        return "Flat"

    def scan(self) -> list[Signal]:
        started = time.time()
        try:
            candidates = self._fast_filter_symbols()
            btc_health = self._analyze_btc()
        except Exception:
            # This was the actual cause of the "Connection reset by peer" crashes: a
            # transient network hiccup on the ticker list or BTC klines fetch used to
            # propagate all the way up and abort the ENTIRE scan (every candidate,
            # not just one symbol) — which meant a single momentary blip could mean
            # zero signals for that cycle, or even the rest of the day if it kept
            # recurring. Per-symbol analysis below was already protected the same way;
            # this brings the setup phase in line with it. Skip this cycle cleanly and
            # let the next one (in SCAN_INTERVAL_SECONDS) retry normally.
            logging.exception("Scan setup failed (ticker list or BTC data) — skipping this cycle")
            self.last_scan_summary = (
                f"Son tarama: {local_now_text()}\n"
                "Piyasa verisi çekilemedi (geçici ağ hatası), bu tarama atlandı — bir sonraki taramada tekrar denenecek."
            )
            self.last_market_condition_summary = "Piyasa verisi şu an çekilemedi (geçici ağ hatası) — bir sonraki taramada tekrar denenecek."
            return []
        us_detail = self.macro.us_equities_detail()
        us_trend = us_detail["combined"]
        self.last_us_trend = us_trend
        self.last_spx_trend = us_detail["spx"]
        self.last_nasdaq_trend = us_detail["nasdaq"]
        dxy_trend = self.macro.dxy_trend()
        self.last_dxy_trend = dxy_trend

        dominance = self.macro.crypto_dominance()
        btc_dom_pct = dominance["btc"] if dominance else None
        usdt_dom_pct = dominance["usdt"] if dominance else None
        btc_dom_trend = self._dominance_trend(self.last_btc_dominance_pct, btc_dom_pct)
        usdt_dom_trend = self._dominance_trend(self.last_usdt_dominance_pct, usdt_dom_pct)
        self.last_btc_dominance_trend = btc_dom_trend
        self.last_usdt_dominance_trend = usdt_dom_trend
        self.last_btc_dominance_pct = btc_dom_pct if btc_dom_pct is not None else self.last_btc_dominance_pct
        self.last_usdt_dominance_pct = usdt_dom_pct if usdt_dom_pct is not None else self.last_usdt_dominance_pct

        signals = []
        rejected = 0

        for market_symbol in candidates:
            try:
                signal = self._analyze_symbol(
                    market_symbol.symbol, btc_health, us_trend, dxy_trend, btc_dom_trend, usdt_dom_trend,
                )
            except Exception:
                rejected += 1
                logging.exception("Symbol analysis failed: %s", market_symbol.symbol)
                continue
            if signal:
                signals.append(signal)
            else:
                rejected += 1

        signals.sort(key=lambda item: item.confidence, reverse=True)
        elapsed = round(time.time() - started, 1)

        # Requested explicitly: "bunlar hangileri destekliyor? Piyasayı?" (which of
        # these support the market?) - SPX/NASDAQ Bullish, DXY Bearish (falling dollar
        # helps risk assets), and falling USDT dominance (money leaving stablecoins
        # into risk assets) all read as market-supportive; only known (non-Unknown)
        # readings are counted. BTC dominance is left out of this specific count since
        # it means something different for BTC itself vs for altcoins (falling BTC.D
        # is an "alt season" signal, not necessarily bullish for BTC's own price) -
        # it's still shown on its own line just below.
        supportive_checks = [
            ("S&P 500", us_detail["spx"] == "Bullish", us_detail["spx"] != "Unknown"),
            ("NASDAQ", us_detail["nasdaq"] == "Bullish", us_detail["nasdaq"] != "Unknown"),
            ("DXY", dxy_trend == "Bearish", dxy_trend != "Unknown"),
            ("USDT Dominansı", usdt_dom_trend == "Falling", usdt_dom_trend != "Unknown"),
        ]
        known = [(name, ok) for name, ok, is_known in supportive_checks if is_known]
        supportive_names = [name for name, ok in known if ok]
        supportive_line = (
            f"{', '.join(supportive_names) if supportive_names else 'Hiçbiri'} ({len(supportive_names)}/{len(known)} bilinen)"
            if known else "Bilinmiyor (veri çekilemedi)"
        )

        btc_dom_text = f"{btc_dom_trend}" + (f" ({btc_dom_pct:.1f}%)" if btc_dom_pct is not None else "")
        usdt_dom_text = f"{usdt_dom_trend}" + (f" ({usdt_dom_pct:.1f}%)" if usdt_dom_pct is not None else "")

        self.last_scan_summary = (
            f"Son tarama: {local_now_text()}\n"
            f"BTC: {btc_health.status} ({btc_health.direction}, skor {btc_health.score:.0f})\n"
            f"S&P 500: {us_detail['spx']}\n"
            f"NASDAQ: {us_detail['nasdaq']}\n"
            f"Dolar Endeksi (DXY): {dxy_trend}\n"
            f"BTC Dominansı: {btc_dom_text}\n"
            f"USDT Dominansı: {usdt_dom_text}\n"
            f"Piyasayı destekleyen: {supportive_line}\n"
            f"Analiz edilen: {len(candidates)}\n"
            f"Reddedilen: {rejected}\n"
            f"Sinyal adayı: {len(signals)}\n"
            f"Süre: {elapsed} sn"
        )
        self.last_market_condition_summary = self._market_condition_summary(btc_health, us_trend, len(signals))
        return signals

    @staticmethod
    def _market_condition_summary(btc: BtcHealth, us_trend: str, signal_count: int) -> str:
        """Plain-language read of current conditions, for the 'no new signal' report —
        instead of a labeled stats dump (BTC: Healthy (Mixed, skor 82) etc.), say in
        ordinary words what the market is doing and, when nothing fired, why."""
        btc_direction_text = {
            "Bullish": "BTC net yükseliş eğiliminde",
            "Bearish": "BTC net düşüş eğiliminde",
            "Mixed": "BTC şu anda net bir yön vermiyor, kararsız seyrediyor",
        }.get(btc.direction, "BTC yönü belirsiz")

        btc_status_text = {
            "Healthy": "genel piyasa görünümü sağlıklı",
            "Cautious": "piyasada temkinli olunması gereken bir hava var",
            "Dangerous": "piyasa şu anda riskli görünüyor",
        }.get(btc.status, "")

        us_text = {
            "Bullish": "ABD borsaları (S&P 500 + NASDAQ) yükselişte",
            "Bearish": "ABD borsaları düşüşte",
            "Mixed": "ABD borsaları kararsız seyrediyor",
            "Unknown": "ABD borsası verisi şu an alınamadı",
        }.get(us_trend, "")

        summary = f"{btc_direction_text}, {btc_status_text}. {us_text}."

        if signal_count == 0:
            if btc.direction == "Mixed":
                reason = (
                    "BTC net bir yön vermediği için bu taramada hiçbir coin için LONG ya da "
                    "SHORT aranmadı — piyasa netleşince tekrar değerlendirilecek."
                )
            else:
                reason = (
                    "Piyasanın yönü belliydi ama taranan coinlerin hiçbiri trend, destek/direnç, "
                    "likidite avı gibi kalite filtrelerinin hepsini aynı anda geçemedi."
                )
            summary += f"\n\n{reason}"

        return summary

    def _fast_filter_symbols(self) -> list[MarketSymbol]:
        tickers = self.binance.tickers_24h()
        filtered = [
            item
            for item in tickers
            if item.quote_volume >= self.config.min_quote_volume_usdt
            and item.last_price > 0
            and not item.symbol.endswith("USDC")
        ]
        filtered.sort(key=lambda item: item.quote_volume, reverse=True)
        if self.config.max_symbols_to_analyze and self.config.max_symbols_to_analyze > 0:
            return filtered[: self.config.max_symbols_to_analyze]
        return filtered

    def _analyze_btc(self) -> BtcHealth:
        candles_1h = self.binance.klines("BTCUSDT", "1h", 210)
        closes = [c.close for c in candles_1h]
        ema50 = IndicatorEngine.ema(closes, 50)[-1]
        ema200 = IndicatorEngine.ema(closes, 200)[-1]
        rsi = IndicatorEngine.rsi(closes)
        atr = IndicatorEngine.atr(candles_1h)
        adx = IndicatorEngine.adx(candles_1h)
        price = closes[-1]
        volatility = atr / price if price else 0
        pct_change_24h = (closes[-1] - closes[-25]) / closes[-25] if len(closes) >= 25 and closes[-25] > 0 else 0.0
        score = 50
        details = []

        # EMA50-vs-EMA200 structure confirms an ESTABLISHED trend well, but it's a
        # lagging signal — the (very slow-moving) 200-period average can take days to
        # catch up, so a genuinely sharp single-day move can still show up as "Mixed"
        # here even though price already moved a lot. pct_change_24h is a faster,
        # more responsive fallback for exactly that case.
        if price > ema50 > ema200:
            score += 22
            direction = "Bullish"
            details.append("BTC EMA yapısı pozitif")
        elif price < ema50 < ema200:
            score += 22
            direction = "Bearish"
            details.append("BTC EMA yapısı negatif")
        elif pct_change_24h <= -0.03:
            direction = "Bearish"
            details.append("BTC 24 saatte sert düştü")
        elif pct_change_24h >= 0.03:
            direction = "Bullish"
            details.append("BTC 24 saatte sert yükseldi")
        else:
            direction = "Mixed"
            details.append("BTC trendi karışık")

        # Recovery override: both the EMA structure AND the 24h-change fallback above
        # are still looking BACKWARD (what already happened). If BTC's own most recent
        # candles show the move already stalling/reversing — a higher low right after
        # a "Bearish" read, or a lower high right after "Bullish" — keep calling it
        # Bearish/Bullish is stale and dangerous: it's exactly how a routine dip inside
        # an uptrend gets misread as a fresh downtrend right as it turns back up,
        # which is what let a wave of SHORT signals fire straight into a rally.
        if len(candles_1h) >= 12:
            recent = candles_1h[-6:]
            prior = candles_1h[-12:-6]
            if direction == "Bearish":
                recent_low = min(c.low for c in recent)
                prior_low = min(c.low for c in prior)
                if recent_low > prior_low:
                    direction = "Mixed"
                    details.append("BTC toparlanma belirtisi gösteriyor, Bearish okuma iptal edildi")
            elif direction == "Bullish":
                recent_high = max(c.high for c in recent)
                prior_high = max(c.high for c in prior)
                if recent_high < prior_high:
                    direction = "Mixed"
                    details.append("BTC zayıflama belirtisi gösteriyor, Bullish okuma iptal edildi")

        if 45 <= rsi <= 68:
            score += 10
            details.append("BTC momentumu sağlıklı")
        elif rsi > 76 or rsi < 24:
            score -= 12
            details.append("BTC aşırı bölgede")

        if 15 <= adx <= 45:
            score += 8
            details.append("BTC trend gücü kabul edilebilir")
        if volatility > 0.035:
            score -= 18
            details.append("BTC volatilitesi yüksek")

        score = clamp(score, 0, 100)
        if score >= 72:
            status = "Healthy"
        elif score >= 55:
            status = "Cautious"
        else:
            status = "Dangerous"
        return BtcHealth(status=status, direction=direction, score=score, volatility=volatility, details=details, pct_change_24h=pct_change_24h)

    def _analyze_symbol(
        self, symbol: str, btc: BtcHealth, us_trend: str, dxy_trend: str, btc_dom_trend: str, usdt_dom_trend: str,
    ) -> Signal | None:
        candles_15m = self.binance.klines(symbol, "15m", 210)
        candles_1h = self.binance.klines(symbol, "1h", 210)
        candles_4h = self.binance.klines(symbol, "4h", 210)
        # Long-term liquidity zones: an old high/low from months (up to ~a year) back
        # that price never came back to since - "iki ay önceki fitilde bir likidasyon
        # var, oraya inip temizleyebilir." Fetched separately, daily candles, since
        # nothing else here looks back further than ~9 days (candles_1h at 210x1h).
        candles_1d = self.binance.klines(symbol, "1d", 400)
        if len(candles_15m) < 100 or len(candles_1h) < 100:
            return None

        closes_15m = [c.close for c in candles_15m]
        closes_1h = [c.close for c in candles_1h]
        closes_4h = [c.close for c in candles_4h]
        price = closes_15m[-1]
        atr = IndicatorEngine.atr(candles_15m)
        atr_1h = IndicatorEngine.atr(candles_1h)
        if atr <= 0 or price <= 0:
            return None

        ema20 = IndicatorEngine.ema(closes_15m, 20)
        ema50 = IndicatorEngine.ema(closes_15m, 50)
        ema200_1h = IndicatorEngine.ema(closes_1h, 200)
        rsi = IndicatorEngine.rsi(closes_15m)
        macd_line, macd_signal, macd_hist = IndicatorEngine.macd(closes_15m)
        adx = IndicatorEngine.adx(candles_15m)
        rel_volume = self._relative_volume(candles_15m)
        funding = self.binance.funding_rate(symbol)
        open_interest = self.binance.open_interest(symbol)
        long_short_ratio = self.binance.long_short_ratio(symbol)
        structure = self._market_structure(candles_15m)
        htf_trend = self._higher_timeframe_trend(closes_4h)
        volatility = atr / price

        # How far price has already run in the last 6 hourly candles. Chasing a coin
        # that already moved a lot very recently (a pump already in progress) is a
        # well-known high-risk entry — it's much more likely to be the coin's brief top
        # than the start of a fresh trend. This is a hard reject, not a scoring nudge.
        extension_pct = 0.0
        if len(closes_1h) >= 7 and closes_1h[-7] > 0:
            extension_pct = (closes_1h[-1] - closes_1h[-7]) / closes_1h[-7]

        # Proxy for BTC-dominance-style rotation: is capital moving into this specific
        # coin relative to BTC, or is BTC eating its relative strength? Binance doesn't
        # publish an actual BTC.D/USDT.D index (that's a cross-exchange aggregate only
        # data providers like CoinGecko compute, and this environment has no network
        # access to fetch that) — this compares the coin's own 24h move against BTC's
        # 24h move using data already being pulled, as a workable substitute.
        alt_pct_change_24h = 0.0
        if len(closes_1h) >= 25 and closes_1h[-25] > 0:
            alt_pct_change_24h = (closes_1h[-1] - closes_1h[-25]) / closes_1h[-25]
        relative_strength_vs_btc = alt_pct_change_24h - btc.pct_change_24h

        liquidity_sweep = self._detect_liquidity_sweep(candles_15m)
        structural_bias = self._structural_sweep_bias(candles_15m)
        sr_zone = self._nearest_sr_zone(candles_1h)
        long_term_zone = self._long_term_liquidity_zone(candles_1d)

        long_gate_ok = self._passes_hard_filters(
            True, price, ema20[-1], ema50[-1], ema200_1h[-1], adx, structure, htf_trend, btc, extension_pct, relative_strength_vs_btc, liquidity_sweep, sr_zone, structural_bias, long_term_zone
        )
        short_gate_ok = self._passes_hard_filters(
            False, price, ema20[-1], ema50[-1], ema200_1h[-1], adx, structure, htf_trend, btc, extension_pct, relative_strength_vs_btc, liquidity_sweep, sr_zone, structural_bias, long_term_zone
        )

        # BTC dominance (rising = capital rotating INTO BTC, typically OUT of alts) is
        # only meaningful for ALTCOIN trades - it says nothing clean about BTC's own
        # USD price direction (BTC.D can rise while BTC itself is flat or even
        # falling, just falling less than everything else). Pass "Unknown" (neutral,
        # no score effect) when the symbol being analyzed IS BTC itself.
        effective_btc_dom_trend = "Unknown" if symbol == "BTCUSDT" else btc_dom_trend

        long_score, long_reasons = (
            self._score_direction(
                "LONG", price, ema20[-1], ema50[-1], ema200_1h[-1], rsi, macd_line, macd_signal, macd_hist,
                adx, rel_volume, funding, open_interest, long_short_ratio, structure, volatility, btc, htf_trend, us_trend, dxy_trend,
                liquidity_sweep, sr_zone, effective_btc_dom_trend, usdt_dom_trend, structural_bias, long_term_zone,
            )
            if long_gate_ok else (0.0, [])
        )
        short_score, short_reasons = (
            self._score_direction(
                "SHORT", price, ema20[-1], ema50[-1], ema200_1h[-1], rsi, macd_line, macd_signal, macd_hist,
                adx, rel_volume, funding, open_interest, long_short_ratio, structure, volatility, btc, htf_trend, us_trend, dxy_trend,
                liquidity_sweep, sr_zone, effective_btc_dom_trend, usdt_dom_trend, structural_bias, long_term_zone,
            )
            if short_gate_ok else (0.0, [])
        )

        long_qualifies = long_score >= self.config.min_confidence
        short_qualifies = short_score >= (self.config.min_confidence + self.SHORT_EXTRA_CONFIRMATION)

        if not long_qualifies and not short_qualifies:
            return None
        if long_qualifies and not short_qualifies:
            return self._build_signal(symbol, "LONG", long_score, price, atr, atr_1h, long_reasons, btc.status)
        if short_qualifies and not long_qualifies:
            return self._build_signal(symbol, "SHORT", short_score, price, atr, atr_1h, short_reasons, btc.status)
        # Both qualify -- same tie-break as before, pick whichever scored higher.
        if long_score >= short_score:
            return self._build_signal(symbol, "LONG", long_score, price, atr, atr_1h, long_reasons, btc.status)
        return self._build_signal(symbol, "SHORT", short_score, price, atr, atr_1h, short_reasons, btc.status)

    @staticmethod
    def _passes_hard_filters(
        bullish: bool,
        price: float,
        ema20: float,
        ema50: float,
        ema200_1h: float,
        adx: float,
        structure: str,
        htf_trend: str,
        btc: BtcHealth,
        extension_pct: float,
        relative_strength_vs_btc: float,
        liquidity_sweep: str,
        sr_zone: str,
        structural_bias: str,
        long_term_zone: str,
    ) -> bool:
        """A direction must clear ALL of these before its weighted score even counts.
        This used to all be soft, additive scoring (lose some points here, make it up
        there) — which let setups through where only some things lined up. These are
        the conditions that mattered most for actually reading the market correctly,
        so now they gate the direction out entirely rather than just costing points.

        NOTE: US equities (S&P/NASDAQ) direction is deliberately NOT a hard gate here
        — crypto and US equities are correlated most of the time (ETF flows) but not
        reliably enough to let it single-handedly veto an otherwise well-confirmed
        crypto-native setup (they do decouple — US up while crypto drops, and vice
        versa). It's used as a soft scoring input in _score_direction instead."""
        trend_ok = (price > ema20 > ema50 and price > ema200_1h) if bullish else (price < ema20 < ema50 and price < ema200_1h)
        if not trend_ok:
            return False
        if structure != ("Bullish" if bullish else "Bearish"):
            return False
        if adx < 20:
            return False  # market isn't actually trending, too choppy to trust a directional call
        if htf_trend == ("Bearish" if bullish else "Bullish"):
            return False  # 4h chart disagrees with the trade direction
        if btc.status == "Dangerous":
            return False
        # BTC's own trend directly opposing (or failing to clearly support) the trade
        # direction used to only cost points (soft penalty), not block the trade.
        # Alts overwhelmingly follow BTC's dominant move.
        #
        # This requires STRICT alignment on BOTH sides now — "Mixed" BTC direction no
        # longer passes for either LONG or SHORT. It was briefly asymmetric (Mixed
        # allowed for LONG, blocked for SHORT) after a day where 14/14 SHORT signals
        # failed while LONG did better. But a later day showed 23/23 signals were LONG
        # and still failed badly (13 SL, 1 TP2) — proving the real problem wasn't
        # "LONG is safer than SHORT", it's that "Mixed" BTC gives no real edge for
        # EITHER direction, and whichever side the bot happened to lean on that day
        # was essentially a coin flip. Now neither direction fires unless BTC's own
        # trend is unambiguous — during genuinely uncertain (Mixed) periods, expect
        # fewer or zero signals rather than a guess in either direction.
        if bullish:
            if btc.direction != "Bullish":
                return False
        else:
            if btc.direction != "Bearish":
                return False
        # Already-extended move in the same direction (a pump already in progress) —
        # chasing it here is much riskier than catching it early.
        if bullish and extension_pct > 0.12:
            return False
        if not bullish and extension_pct < -0.12:
            return False
        # BTC-dominance-style check: is this coin meaningfully lagging BTC's own
        # strength (capital isn't rotating in) when going LONG, or meaningfully
        # beating BTC (real buying interest working against the thesis) when
        # going SHORT? Either is a headwind for the trade.
        if bullish and relative_strength_vs_btc < -0.08:
            return False
        if not bullish and relative_strength_vs_btc > 0.08:
            return False
        # Liquidity sweep / fakeout: the entry candle itself just poked to a new local
        # high (or low) and got rejected back inside the prior range with a long wick
        # — a classic stop-hunt, not real continuation. This is exactly the reported
        # pattern: a spike to a new high with a long upper wick, read as a bullish
        # breakout, immediately followed by a hard reversal down.
        if bullish and liquidity_sweep == "BearishSweep":
            return False
        if not bullish and liquidity_sweep == "BullishSweep":
            return False
        # Same idea as the point-in-time sweep above, but for a sweep that happened
        # further back and is STILL being respected (price never traded back through
        # it since) - reported gap: a sweep low followed by a multi-hour rally, where
        # later SHORT signals fired 1-3 hours after the sweep candle itself, too late
        # for the narrow 3-candle window above but still very much the same "don't
        # fight this reversal" situation.
        if bullish and structural_bias == "BearishBias":
            return False
        if not bullish and structural_bias == "BullishBias":
            return False
        # Support/resistance: don't go LONG right into a level that's already
        # rejected price multiple times (resistance), and don't go SHORT right into
        # a level that's already held multiple times (support) — this is exactly
        # backwards from how these zones tend to behave and was the core complaint:
        # "resistance'tan long, support'tan short veriyor, tam tersi olmalı."
        if bullish and sr_zone == "NearResistance":
            return False
        if not bullish and sr_zone == "NearSupport":
            return False
        # Same idea, but for an OLD (up to ~a year back) unswept high/low - "iki ay
        # önceki fitilde bir likidasyon var, oraya inip temizleyebilir." Don't go
        # LONG right into an old unswept high, don't go SHORT right into an old
        # unswept low - same backwards-zone logic as the recent SR check above.
        if bullish and long_term_zone == "NearOldResistance":
            return False
        if not bullish and long_term_zone == "NearOldSupport":
            return False
        return True

    def _score_direction(
        self,
        side: str,
        price: float,
        ema20: float,
        ema50: float,
        ema200_1h: float,
        rsi: float,
        macd_line: float,
        macd_signal: float,
        macd_hist: float,
        adx: float,
        rel_volume: float,
        funding: float | None,
        open_interest: float | None,
        long_short_ratio: float | None,
        structure: str,
        volatility: float,
        btc: BtcHealth,
        htf_trend: str,
        us_trend: str,
        dxy_trend: str,
        liquidity_sweep: str,
        sr_zone: str,
        btc_dom_trend: str,
        usdt_dom_trend: str,
        structural_bias: str,
        long_term_zone: str,
    ) -> tuple[float, list[str]]:
        score = 0.0
        reasons = []
        bullish = side == "LONG"

        trend_ok = price > ema20 > ema50 and price > ema200_1h if bullish else price < ema20 < ema50 and price < ema200_1h
        if trend_ok:
            score += 22
            reasons.append("Trend confirmed")

        btc_ok = btc.status != "Dangerous" and (btc.direction in ("Mixed", "Bullish") if bullish else btc.direction in ("Mixed", "Bearish"))
        if btc_ok:
            score += 14
            reasons.append(f"BTC status supportive: {btc.status}")
        elif btc.status == "Dangerous":
            score -= 18
            reasons.append("BTC dangerous, trade filtered conservatively")

        momentum_ok = (rsi > 52 and macd_line > macd_signal and macd_hist > 0) if bullish else (rsi < 48 and macd_line < macd_signal and macd_hist < 0)
        if momentum_ok:
            score += 18
            reasons.append("Momentum confirmed")

        structure_ok = structure == ("Bullish" if bullish else "Bearish")
        if structure_ok:
            score += 14
            reasons.append("Market structure confirmed")

        htf_ok = htf_trend == ("Bullish" if bullish else "Bearish")
        htf_conflict = htf_trend == ("Bearish" if bullish else "Bullish")
        if htf_ok:
            score += 12
            reasons.append("Higher timeframe (4h) trend aligned")
        elif htf_conflict:
            score -= 14
            reasons.append("Higher timeframe (4h) trend conflicting")

        # Soft input, not a gate: crypto tracks US equities (ETF flows) MOST of the
        # time but genuinely decouples sometimes — US up while crypto drops, or the
        # reverse. A small nudge here can't by itself block a setup where the
        # crypto-native signals (trend/structure/momentum/BTC) are all strongly
        # confirmed; it only matters at the margin.
        us_ok = us_trend == ("Bullish" if bullish else "Bearish")
        us_conflict = us_trend == ("Bearish" if bullish else "Bullish")
        if us_ok:
            score += 6
            reasons.append("US equities (S&P/NASDAQ) aligned")
        elif us_conflict:
            score -= 6
            reasons.append("US equities (S&P/NASDAQ) conflicting")

        # Same soft-nudge treatment as us_trend above, but DXY moves OPPOSITE to risk
        # assets: a falling dollar (Bearish DXY) supports LONG, a rising dollar
        # (Bullish DXY) supports SHORT — so the mapping is inverted relative to us_ok.
        dxy_ok = dxy_trend == ("Bearish" if bullish else "Bullish")
        dxy_conflict = dxy_trend == ("Bullish" if bullish else "Bearish")
        if dxy_ok:
            score += 6
            reasons.append("Dolar endeksi (DXY) destekleyici")
        elif dxy_conflict:
            score -= 6
            reasons.append("Dolar endeksi (DXY) ters yönde")

        # liquidity_sweep and sr_zone were ONLY ever used as hard blocks (in
        # _passes_hard_filters) on the WRONG direction - a bearish sweep stopped LONG,
        # but never actively helped SHORT qualify. That's exactly the reported gap:
        # "temizliğini gördükten sonra short vermem gerekiyor" (after seeing the
        # sweep/cleanup, I need a SHORT) - a sweep should be a reason FOR the reversal
        # trade, not just a veto on the wrong one. Same idea for a tested support/
        # resistance touch: bouncing off a well-tested support is itself a reason to
        # go LONG from there specifically, not just "not blocked in the middle of a
        # range." Both get a real weight here since they're the most specific,
        # chart-level confirmation available - similar in spirit to trend/structure.
        if bullish and liquidity_sweep == "BullishSweep":
            score += 10
            reasons.append("Likidite avı (sweep) bu yönü destekliyor")
        if not bullish and liquidity_sweep == "BearishSweep":
            score += 10
            reasons.append("Likidite avı (sweep) bu yönü destekliyor")

        # Same as the point-in-time sweep just above, but for a still-holding sweep
        # from further back (see _structural_sweep_bias) - a rally that's been
        # respecting a sweep low for hours is exactly as valid a reason to favor LONG
        # as a sweep that just happened on the last candle.
        if bullish and structural_bias == "BullishBias":
            score += 10
            reasons.append("Sürmekte olan likidite avı yapısı bu yönü destekliyor")
        if not bullish and structural_bias == "BearishBias":
            score += 10
            reasons.append("Sürmekte olan likidite avı yapısı bu yönü destekliyor")

        if bullish and sr_zone == "NearSupport":
            score += 8
            reasons.append("Test edilmiş destek bölgesinden dönüş")
        if not bullish and sr_zone == "NearResistance":
            score += 8
            reasons.append("Test edilmiş direnç bölgesinden dönüş")

        # Old (up to ~a year back), never-revisited high/low - same weight as the
        # recent SR zone above, since an untouched old level can pull just as hard.
        if bullish and long_term_zone == "NearOldSupport":
            score += 8
            reasons.append("Eski (uzun vadeli) likidite bölgesinden dönüş")
        if not bullish and long_term_zone == "NearOldResistance":
            score += 8
            reasons.append("Eski (uzun vadeli) likidite bölgesinden dönüş")

        # BTC dominance: RISING means capital rotating INTO BTC and OUT of altcoins -
        # bearish for an altcoin LONG, supportive of an altcoin SHORT. Already passed
        # as "Unknown" (no effect) when the symbol being analyzed IS BTC itself, since
        # this reads rotation between BTC and alts, not BTC's own direction.
        if bullish and btc_dom_trend == "Falling":
            score += 5
            reasons.append("BTC dominansı düşüyor (alt rotasyonu destekleyici)")
        elif not bullish and btc_dom_trend == "Rising":
            score += 5
            reasons.append("BTC dominansı yükseliyor (alt rotasyonu ters yönde)")

        # USDT dominance: RISING means capital moving INTO stablecoins - broad
        # risk-off, bearish for crypto generally (any symbol, BTC included).
        # FALLING means capital moving OUT of stablecoins into risk assets.
        if bullish and usdt_dom_trend == "Falling":
            score += 5
            reasons.append("USDT dominansı düşüyor (risk iştahı destekleyici)")
        elif not bullish and usdt_dom_trend == "Rising":
            score += 5
            reasons.append("USDT dominansı yükseliyor (risk iştahı azalıyor)")

        if rel_volume >= 1.15:
            score += 12
            reasons.append(f"Volume confirmed ({rel_volume:.2f}x)")
        elif rel_volume < 0.75:
            score -= 8
            reasons.append("Weak participation")

        if 16 <= adx <= 42:
            score += 8
            reasons.append("Trend strength acceptable")
        elif adx < 12:
            score -= 8
            reasons.append("Trend strength weak")

        if volatility <= 0.022:
            score += 7
            reasons.append("Volatility controlled")
        elif volatility > 0.04:
            score -= 12
            reasons.append("Volatility too high")

        if funding is not None:
            if bullish and funding < 0.0008:
                score += 3
                reasons.append("Funding acceptable")
            elif not bullish and funding > -0.0008:
                score += 3
                reasons.append("Funding acceptable")
            else:
                score -= 5
                reasons.append("Funding crowded")

        if open_interest is not None and open_interest > 0:
            score += 2
            reasons.append("Open interest available")

        # Contrarian: a heavily lopsided long/short account ratio means one side of
        # the market is crowded -- exactly the kind of imbalance that precedes a
        # squeeze / liquidity-sweep reversal against the crowded side.
        if long_short_ratio is not None:
            if bullish:
                if long_short_ratio > 2.5:
                    score -= 6
                    reasons.append("Long/short oranı çok kalabalık long (sıkışma riski)")
                elif long_short_ratio < 0.7:
                    score += 4
                    reasons.append("Long/short oranı LONG lehine destekleyici")
            else:
                if long_short_ratio < 0.4:
                    score -= 6
                    reasons.append("Long/short oranı çok kalabalık short (sıkışma riski)")
                elif long_short_ratio > 1.5:
                    score += 4
                    reasons.append("Long/short oranı SHORT lehine destekleyici")

        return clamp(score, 0, 100), reasons

    @staticmethod
    def _higher_timeframe_trend(closes_4h: list[float]) -> str:
        """Reads the 4h chart's structure (EMA50 vs EMA200) as an extra, higher-timeframe
        confirmation layer, so the bot doesn't just react to noisy 15m/1h moves."""
        if len(closes_4h) < 210:
            return "Mixed"
        price = closes_4h[-1]
        ema50 = IndicatorEngine.ema(closes_4h, 50)[-1]
        ema200 = IndicatorEngine.ema(closes_4h, 200)[-1]
        if price > ema50 > ema200:
            return "Bullish"
        if price < ema50 < ema200:
            return "Bearish"
        return "Mixed"

    # If the stop distance itself is this wide (as % of price), the coin is too
    # volatile to risk-manage sanely even with reduced leverage — skip it entirely
    # rather than send a trade whose worst case is still a huge loss.
    MAX_STOP_DISTANCE_PCT = 0.08

    # REMOVED (was 6): this used to force SHORT to clear a higher confidence bar than
    # LONG, on the theory that crypto's structural upward bias makes shorts riskier.
    # That was added after a day where 14/14 SHORT signals hit stop — but the BTC
    # 'Mixed' fix above (now blocking BOTH directions when BTC is ambiguous, not just
    # SHORT) targets that same root cause more precisely, and predates this constant.
    # Left in place, this handicap was producing a severe imbalance (39 LONG vs 5
    # SHORT in one week) while LONG itself was not winning either (14% win rate that
    # same week, 25 SL vs 4 TP2) — so it wasn't protecting the account, just
    # concentrating losses into LONG instead of screening trades on their own merit.
    # Both directions now clear the same confidence bar.
    SHORT_EXTRA_CONFIRMATION = 0

    def _build_signal(
        self, symbol: str, side: str, confidence: float, entry: float, atr: float, atr_1h: float,
        reasons: list[str], btc_status: str,
    ) -> Signal | None:
        # Risk distance used to be 1.35x the 15m ATR — a single 15-minute candle's
        # average range. That's small enough that ordinary price noise (not an actual
        # reversal) was hitting the stop before the trade thesis had time to play out,
        # which is why SL was firing so much more often than TP. The 1h ATR is a far
        # less noisy measure of how much this symbol actually moves, so basing the
        # stop on it gives the trade real room to work before getting stopped out.
        risk_unit = atr_1h if atr_1h > 0 else atr * 4
        risk_distance = risk_unit * 1.6
        stop_distance_pct = risk_distance / entry
        if stop_distance_pct > self.MAX_STOP_DISTANCE_PCT:
            return None  # too volatile to risk-manage — e.g. this is what let a -36% leveraged loss through on 1000XECUSDT
        # TP1 used to sit at 1.45x the risk distance. Restated preference: TP1 alone
        # (not TP2) is what counts as "this trade worked" — TP2 is a bonus if price
        # keeps going, not the target being optimized for. A closer TP1 is reached far
        # more often for the same entry quality, at the cost of a smaller win when it
        # does. TP2 is unchanged — still the "let it run" target for the rest.
        reward_1 = risk_distance * 1.0
        reward_2 = risk_distance * 2.25
        if side == "LONG":
            stop_loss = entry - risk_distance
            tp1 = entry + reward_1
            tp2 = entry + reward_2
        else:
            stop_loss = entry + risk_distance
            tp1 = entry - reward_1
            tp2 = entry - reward_2
        risk_reward = abs(tp2 - entry) / abs(entry - stop_loss)
        leverage = self._suggest_leverage(stop_distance_pct)
        return Signal(
            symbol=symbol,
            side=side,
            confidence=confidence,
            entry=entry,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            leverage=leverage,
            risk_reward=risk_reward,
            btc_status=btc_status,
            reasons=reasons,
        )

    @staticmethod
    def _relative_volume(candles: list[Candle]) -> float:
        if len(candles) < 30:
            return 0.0
        current = candles[-1].quote_volume
        baseline = statistics.fmean(c.quote_volume for c in candles[-21:-1])
        return current / baseline if baseline else 0.0

    @staticmethod
    def _market_structure(candles: list[Candle]) -> str:
        recent = candles[-24:]
        highs = [c.high for c in recent]
        lows = [c.low for c in recent]
        first_high = max(highs[:12])
        second_high = max(highs[12:])
        first_low = min(lows[:12])
        second_low = min(lows[12:])
        if second_high > first_high and second_low > first_low:
            return "Bullish"
        if second_high < first_high and second_low < first_low:
            return "Bearish"
        return "Mixed"

    @staticmethod
    def _detect_liquidity_sweep(candles: list[Candle], recency_window: int = 3) -> str:
        """Classic 'liquidity sweep' / stop-hunt / fakeout (Smart Money Concepts /
        Wyckoff 'upthrust' and 'spring'): price pokes past a recent swing high or low
        — often triggering breakout buyers and resting stop orders — then FAILS to
        hold and closes back inside the prior range, typically leaving a long wick.
        This is the exact pattern reported: a sharp spike to a new local high with a
        long upper wick, immediately followed by a hard reversal down — the bot read
        that spike as bullish breakout confirmation instead of recognizing it as a
        fakeout top. Returns 'BearishSweep' (fake breakout up, favors NOT going long
        here), 'BullishSweep' (fake breakdown, favors NOT going short here), or
        'None'.

        RECENCY WINDOW FIX: this used to only check candles[-1] — literally just the
        single most recent candle. Worse, klines() returns Binance's still-forming
        candle as the last row, so that candle's wick can still change shape until it
        actually closes. A sweep-and-reject doesn't stop mattering the instant that
        one candle closes — the reported miss (LONG given when a sweep had just
        rejected a high 1-2 candles earlier) is exactly what a single-candle-only
        check misses. Now: the still-forming candle is excluded, and the last
        `recency_window` CLOSED candles are each checked against the 20-candle range
        before them — a sweep on any of them still counts."""
        closed = candles[:-1]  # exclude the still-forming candle (last row from klines())
        if len(closed) < 20 + recency_window:
            return "None"
        for i in range(len(closed) - recency_window, len(closed)):
            lookback = closed[i - 20 : i]
            candidate = closed[i]
            if len(lookback) < 20:
                continue
            prior_high = max(c.high for c in lookback)
            prior_low = min(c.low for c in lookback)
            candle_range = candidate.high - candidate.low
            if candle_range <= 0:
                continue
            upper_wick = candidate.high - max(candidate.open, candidate.close)
            lower_wick = min(candidate.open, candidate.close) - candidate.low

            swept_high = candidate.high > prior_high and candidate.close < prior_high
            if swept_high and upper_wick > candle_range * 0.4:
                return "BearishSweep"

            swept_low = candidate.low < prior_low and candidate.close > prior_low
            if swept_low and lower_wick > candle_range * 0.4:
                return "BullishSweep"

        return "None"

    @staticmethod
    def _structural_sweep_bias(candles: list[Candle], lookback: int = 60) -> str:
        """_detect_liquidity_sweep only looks at the last `recency_window` (3)
        candles - it correctly catches a FRESH sweep, but its protective effect
        expires 45 minutes later even if the market is still clearly respecting
        that reversal. This is exactly the reported gap: a sweep low followed by a
        multi-HOUR rally, where later altcoin SHORT signals fired 1-3 hours after
        the sweep candle itself - too late for the narrow window to still see it,
        even though price never came close to revisiting that low the whole time.

        Scans back through `lookback` candles (60 x 15m = 15h) for the MOST RECENT
        candle that qualifies as a sweep (same wick/rejection test as
        _detect_liquidity_sweep), then checks whether every candle SINCE has
        respected it (never closed back through the swept level). If yes, that
        sweep's bias is still structurally active - the reversal hasn't been
        invalidated, no matter how many candles ago it happened. Returns
        'BullishBias' (a bullish sweep low is still holding - don't fight it with a
        SHORT), 'BearishBias' (mirror case for a resistance sweep), or 'None' (no
        sweep found in range, or the most recent one already got invalidated by
        price trading back through it)."""
        closed = candles[:-1]
        if len(closed) < lookback + 20:
            return "None"
        window = closed[-lookback:]
        for i in range(len(window) - 1, 19, -1):  # scan backward: most recent first
            lookback_slice = window[i - 20 : i]
            candidate = window[i]
            prior_high = max(c.high for c in lookback_slice)
            prior_low = min(c.low for c in lookback_slice)
            candle_range = candidate.high - candidate.low
            if candle_range <= 0:
                continue
            upper_wick = candidate.high - max(candidate.open, candidate.close)
            lower_wick = min(candidate.open, candidate.close) - candidate.low

            if candidate.low < prior_low and candidate.close > prior_low and lower_wick > candle_range * 0.4:
                still_holding = all(c.close > candidate.low for c in window[i + 1 :])
                return "BullishBias" if still_holding else "None"

            if candidate.high > prior_high and candidate.close < prior_high and upper_wick > candle_range * 0.4:
                still_holding = all(c.close < candidate.high for c in window[i + 1 :])
                return "BearishBias" if still_holding else "None"

        return "None"

    @staticmethod
    def _nearest_sr_zone(candles: list[Candle]) -> str:
        """Support/resistance: is current price sitting right at a level that's been
        tested (touched and reversed) multiple times recently — a double-top/double-
        bottom style zone, exactly like the one circled on the reported chart. This
        was completely missing: a pure trend-following read (EMAs, momentum) keeps
        saying "bullish" right up until price hits a well-tested ceiling and rejects,
        or "bearish" right up until it hits a well-tested floor and bounces — which is
        backwards from how this level actually tends to behave. Returns
        'NearResistance', 'NearSupport', or 'None'."""
        if len(candles) < 60:
            return "None"
        lookback = candles[-120:] if len(candles) >= 120 else candles
        current_price = lookback[-1].close
        if current_price <= 0:
            return "None"

        window = 3
        pivot_highs = []
        pivot_lows = []
        for i in range(window, len(lookback) - window):
            segment = lookback[i - window : i + window + 1]
            if lookback[i].high == max(c.high for c in segment):
                pivot_highs.append(lookback[i].high)
            if lookback[i].low == min(c.low for c in segment):
                pivot_lows.append(lookback[i].low)

        tolerance = current_price * 0.006  # ~0.6% band around the current price
        resistance_touches = sum(1 for h in pivot_highs if abs(h - current_price) <= tolerance)
        support_touches = sum(1 for l in pivot_lows if abs(l - current_price) <= tolerance)

        if resistance_touches >= 2 and resistance_touches > support_touches:
            return "NearResistance"
        if support_touches >= 2 and support_touches > resistance_touches:
            return "NearSupport"
        return "None"

    @staticmethod
    def _long_term_liquidity_zone(candles_1d: list[Candle]) -> str:
        """Different concept from _nearest_sr_zone (which wants a level TESTED 2+
        times within the last ~9 days). This is the reported idea: an old,
        significant high or low from months back (up to ~a year, using DAILY
        candles) that price swept ONCE and never came back to since - resting
        liquidity/stops from that move are still untouched, and price often gets
        drawn back to it eventually. Only needs ONE occurrence, not repeated
        touches - the fact that it was never revisited since is what still makes it
        'live'. Deliberately skips the most recent 14 days (that range is already
        covered by _nearest_sr_zone and the sweep detectors above) so this is
        specifically about the OLDER, longer-forgotten levels.
        Returns 'NearOldResistance', 'NearOldSupport', or 'None'."""
        closed = candles_1d[:-1]
        if len(closed) < 30:
            return "None"
        current_price = candles_1d[-1].close  # latest candle (may still be forming) - reflects price NOW
        if current_price <= 0:
            return "None"

        window = 5
        cutoff = max(window, len(closed) - 14)
        old_highs = []
        old_lows = []
        for i in range(window, cutoff - window):
            segment = closed[i - window : i + window + 1]
            candidate = closed[i]
            if candidate.high == max(c.high for c in segment):
                if all(c.high < candidate.high for c in closed[i + 1 :]):
                    old_highs.append(candidate.high)
            if candidate.low == min(c.low for c in segment):
                if all(c.low > candidate.low for c in closed[i + 1 :]):
                    old_lows.append(candidate.low)

        tolerance = current_price * 0.02  # 2% - daily-candle levels are coarser than the hourly SR check
        near_resistance = any(abs(h - current_price) <= tolerance for h in old_highs)
        near_support = any(abs(l - current_price) <= tolerance for l in old_lows)

        if near_resistance and not near_support:
            return "NearOldResistance"
        if near_support and not near_resistance:
            return "NearOldSupport"
        return "None"

    @staticmethod
    def _suggest_leverage(stop_distance_pct: float) -> int:
        # A trade like 1000XECUSDT (stop 7.28% away, 5x leverage -> a ~36% leveraged
        # loss on a single stop-out) is way too much risk for one trade. Leverage now
        # scales so the WORST CASE (stop hit) lands near a fixed target loss (~12% of
        # margin) regardless of how wide the stop is — a wider stop gets proportionally
        # lower leverage instead of a coarse bucket that could still multiply out huge.
        target_loss_pct = 0.12
        if stop_distance_pct <= 0:
            return 2
        raw_leverage = target_loss_pct / stop_distance_pct
        return int(clamp(round(raw_leverage), 2, 10))


class SignalBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramClient(config.telegram_bot_token, config.telegram_chat_id, config.binance_timeout_seconds)
        self.db = SignalDatabase(DB_PATH)
        self.engine = ProfessionalSignalEngine(BinanceFuturesClient(config.binance_timeout_seconds), config)
        self.next_scan_at = 0.0
        self._last_error_text: str | None = None

    def run(self) -> None:
        self.telegram.delete_webhook()
        self.telegram.send_message("✅ Binance Futures signal bot aktif.\nKomutlar: /scan /status /rapor /help")
        threading.Thread(target=self._position_monitor_loop, daemon=True).start()
        threading.Thread(target=self._report_scheduler_loop, daemon=True).start()
        while True:
            try:
                self._handle_updates()
                if time.time() >= self.next_scan_at:
                    self._run_scan(send_empty_report=self.config.announce_empty_scans)
                    self.next_scan_at = time.time() + self.config.scan_interval_seconds
                self._last_error_text = None  # clear once any iteration succeeds cleanly
                time.sleep(2)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logging.exception("Main loop error")
                error_text = str(exc)
                # Only notify on a NEW error, not a repeat of the same one every ~12s —
                # this is what was causing the repeated identical "Connection reset by
                # peer" messages. It still logs every time (visible in deploy logs),
                # just doesn't re-send the same Telegram message on every retry.
                if error_text != self._last_error_text:
                    self.telegram.send_message(f"⚠️ Bot hata yakaladı ve çalışmaya devam ediyor:\n{html_escape(error_text)}")
                self._last_error_text = error_text
                # Back off to the normal scan cadence instead of retrying every ~12s —
                # a persistent issue shouldn't turn into a hammering retry loop.
                self.next_scan_at = time.time() + self.config.scan_interval_seconds
                time.sleep(10)

    def _position_monitor_loop(self) -> None:
        # Runs independently of the scan loop above. A full-universe market scan can
        # take a while (many symbols), and TP/SL checks must never wait behind it —
        # that delay was exactly why TP/SL messages were arriving late.
        while True:
            try:
                self._check_open_positions()
            except Exception:
                logging.exception("Position monitor loop error")
            time.sleep(self.config.position_check_interval_seconds)

    def _check_open_positions(self) -> None:
        open_positions = self.db.open_positions()
        if not open_positions:
            return
        try:
            prices = self.engine.binance.all_mark_prices()
        except Exception:
            logging.exception("Position price fetch failed")
            return
        for row in open_positions:
            price = prices.get(row["symbol"])
            if price is None:
                continue
            self._evaluate_position(row, price)

    def _evaluate_position(self, row: sqlite3.Row, price: float) -> None:
        side = row["side"]
        hit_stop = price <= row["stop_loss"] if side == "LONG" else price >= row["stop_loss"]
        hit_tp2 = price >= row["tp2"] if side == "LONG" else price <= row["tp2"]
        hit_tp1 = price >= row["tp1"] if side == "LONG" else price <= row["tp1"]

        # Stop takes priority: protecting capital is checked before targets. Once TP1
        # has already been hit, row["stop_loss"] has already been moved to breakeven
        # by mark_tp1_hit, so this same check naturally becomes a breakeven-close
        # instead of a loss once that has happened.
        if hit_stop and not row["sl_hit"]:
            if row["tp1_hit"]:
                self.telegram.send_message(format_breakeven_hit_message(row, price))
                self.db.mark_breakeven_hit(row["id"], price)
            else:
                self.telegram.send_message(format_sl_hit_message(row, price))
                self.db.mark_sl_hit(row["id"], price)
            return

        if hit_tp2 and not row["tp2_hit"]:
            self.telegram.send_message(format_tp_hit_message(row, price, level=2))
            self.db.mark_tp2_hit(row["id"], price)
            return

        if hit_tp1 and not row["tp1_hit"]:
            self.telegram.send_message(format_tp_hit_message(row, price, level=1))
            self.db.mark_tp1_hit(row["id"])
            # Position stays OPEN after TP1 so we keep watching for TP2 or the
            # (now breakeven) stop.

    def _report_scheduler_loop(self) -> None:
        # Independent thread, same reasoning as the position monitor: report timing
        # must not depend on how long a scan happens to take.
        while True:
            try:
                self._maybe_send_scheduled_reports()
            except Exception:
                logging.exception("Report scheduler loop error")
            time.sleep(30)

    def _maybe_send_scheduled_reports(self) -> None:
        now = datetime.now(LOCAL_TZ)
        if now.hour != 23:
            return  # reports only fire during the 23:00 TR hour

        day_key = now.strftime("%Y-%m-%d")
        if not self.db.report_already_sent("daily", day_key):
            self._send_period_report("daily", now)
            self.db.mark_report_sent("daily", day_key)

        if now.weekday() == 6:  # Monday=0 ... Sunday=6 -> last day of the week
            week_key = now.strftime("%G-W%V")
            if not self.db.report_already_sent("weekly", week_key):
                self._send_period_report("weekly", now)
                self.db.mark_report_sent("weekly", week_key)

        if self._is_last_day_of_month(now):
            month_key = now.strftime("%Y-%m")
            if not self.db.report_already_sent("monthly", month_key):
                self._send_period_report("monthly", now)
                self.db.mark_report_sent("monthly", month_key)

    def _send_period_report(self, period: str, now: datetime) -> None:
        if period == "daily":
            start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
            title = f"📅 <b>GÜNLÜK ÖZET</b> — {now.strftime('%d.%m.%Y')}"
        elif period == "weekly":
            start_local = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
            title = f"🗓️ <b>HAFTALIK ÖZET</b> — {start_local.strftime('%d.%m')} / {now.strftime('%d.%m.%Y')}"
        else:
            start_local = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            title = f"📆 <b>AYLIK ÖZET</b> — {TURKISH_MONTHS[now.month]} {now.year}"

        rows = self.db.signals_between(int(start_local.timestamp()), int(now.timestamp()))
        self.telegram.send_message(self._build_report_text(title, rows))

    @staticmethod
    def _is_last_day_of_month(moment: datetime) -> bool:
        return (moment + timedelta(days=1)).day == 1

    @staticmethod
    def _build_report_text(title: str, rows: list[sqlite3.Row]) -> str:
        def pct(row: sqlite3.Row) -> float | None:
            entry = row["entry"]
            exit_price = row["exit_price"]
            if exit_price is None:
                if row["status"] == "TP2":
                    exit_price = row["tp2"]
                elif row["status"] == "SL":
                    exit_price = row["stop_loss"]
                else:
                    return None
            if row["side"] == "LONG":
                return (exit_price - entry) / entry * 100
            return (entry - exit_price) / entry * 100

        total = len(rows)
        long_count = sum(1 for r in rows if r["side"] == "LONG")
        short_count = sum(1 for r in rows if r["side"] == "SHORT")
        tp1_reached_count = sum(1 for r in rows if r["tp1_hit"])
        tp2_rows = [r for r in rows if r["status"] == "TP2"]
        sl_rows = [r for r in rows if r["status"] == "SL"]
        be_rows = [r for r in rows if r["status"] == "BE"]
        open_rows = [r for r in rows if r["status"] == "OPEN"]
        closed_rows = tp2_rows + sl_rows + be_rows

        lines = [title, ""]
        lines.append(f"📡 Gönderilen sinyal: <b>{total}</b> (LONG {long_count} / SHORT {short_count})")
        lines.append(f"🎯 TP1'e ulaştı (en az bir kez): <b>{tp1_reached_count}</b>")
        lines.append(f"🎯 TP2 (kazandı): <b>{len(tp2_rows)}</b>")
        lines.append(f"⚪️ Başabaş (TP1 sonrası): <b>{len(be_rows)}</b>")
        lines.append(f"🛑 SL (kaybetti): <b>{len(sl_rows)}</b>")
        lines.append(f"⏳ Hâlâ açık: <b>{len(open_rows)}</b>")

        if tp2_rows or sl_rows:
            win_rate = len(tp2_rows) / (len(tp2_rows) + len(sl_rows)) * 100
            lines.append(f"🏆 Kazanma oranı: <b>{win_rate:.0f}%</b> (TP2 vs gerçek SL, başabaş hariç)")

        if rows:
            avg_conf = statistics.mean(r["confidence"] for r in rows)
            lines.append(f"⭐ Ortalama confidence: <b>{avg_conf:.0f}</b>")

        scored = [(r, pct(r)) for r in closed_rows]
        scored = [(r, p) for r, p in scored if p is not None]
        if scored:
            best_row, best_pct = max(scored, key=lambda item: item[1])
            worst_row, worst_pct = min(scored, key=lambda item: item[1])
            lines.append("")
            if best_pct > 0:
                lines.append(f"🥇 En iyi işlem: <b>{best_row['symbol']}</b> {best_row['side']} ({best_pct:+.2f}%)")
            else:
                lines.append("🥇 En iyi işlem: bu dönemde kâr eden işlem olmadı")
            lines.append(f"🥈 En kötü işlem: <b>{worst_row['symbol']}</b> {worst_row['side']} ({worst_pct:+.2f}%)")

        if total == 0:
            lines.append("")
            lines.append("Bu dönemde sinyal gönderilmedi.")

        lines.append("")
        lines.append(f"🕒 {local_now_text()}")
        return "\n".join(lines)

    def _handle_updates(self) -> None:
        for update in self.telegram.get_updates():
            message = update.get("message") or update.get("channel_post") or {}
            text = (message.get("text") or "").strip().lower()
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat_id and chat_id != self.config.telegram_chat_id:
                continue
            if text.startswith("/start") or text.startswith("/help"):
                self.telegram.send_message(
                    "Komutlar:\n"
                    "/scan - hemen piyasa tara\n"
                    "/status - bot durumunu göster\n"
                    "/rapor - bugüne ait özet raporu şimdi gönder\n"
                    "/help - yardım"
                )
            elif text.startswith("/status"):
                self.telegram.send_message(self.status_text())
            elif text.startswith("/rapor"):
                self._send_period_report("daily", datetime.now(LOCAL_TZ))
            elif text.startswith("/scan"):
                self.telegram.send_message("🔎 Manuel tarama başladı.")
                self._run_scan(send_empty_report=True)
                self.next_scan_at = time.time() + self.config.scan_interval_seconds

    US_MARKET_OPEN_TZ = ZoneInfo("America/New_York")

    @classmethod
    def _in_us_market_open_window(cls, now_local: datetime) -> bool:
        """NYSE/NASDAQ open (9:30 AM ET) reliably brings a burst of volume and, per
        observation, can sharply reverse whatever direction crypto was drifting in all
        day — likely tied to US equity / spot-BTC-ETF flows. We can't fetch live
        NASDAQ/SPX data here (no network access in this environment to build or test
        that against), so instead of guessing at an untested integration, this avoids
        opening NEW positions in a caution window around that known, fixed time.
        Existing open positions are unaffected — TP/SL monitoring keeps running as normal.

        WINDOW WIDENED: was 9:15-10:30 AM ET. Reported pattern: a fake pump roughly
        30-60 minutes BEFORE the open (so as early as ~8:30 AM ET) that the bot read
        as bullish and longed, which then reversed — the old window started too late
        (15 min before open) to cover that. Now starts at 8:30 AM ET.
        Does not account for US market holidays."""
        ny_time = now_local.astimezone(cls.US_MARKET_OPEN_TZ)
        if ny_time.weekday() >= 5:  # Saturday/Sunday -> US market isn't open anyway
            return False
        window_start = ny_time.replace(hour=8, minute=30, second=0, microsecond=0)
        window_end = ny_time.replace(hour=10, minute=30, second=0, microsecond=0)
        return window_start <= ny_time <= window_end

    def _run_scan(self, send_empty_report: bool) -> None:
        now_local = datetime.now(LOCAL_TZ)
        if self._in_us_market_open_window(now_local):
            if send_empty_report:
                self.telegram.send_message(
                    "⏸️ ABD borsası açılış penceresi (yüksek volatilite / ani yön değişimi riski). "
                    "Bu aralıkta yeni sinyal aranmıyor, açık pozisyonlar normal şekilde takip ediliyor."
                )
            return
        signals = self.engine.scan()
        sent = 0
        same_direction_counts = {
            "LONG": self.db.same_direction_count("LONG", self.config.same_direction_window_minutes),
            "SHORT": self.db.same_direction_count("SHORT", self.config.same_direction_window_minutes),
        }
        for signal in signals[:5]:
            if self.db.has_open_position(signal.symbol):
                continue
            if self.db.recently_closed(signal.symbol, self.config.signal_cooldown_minutes):
                continue
            if self.db.recently_sent(signal.symbol, signal.side, self.config.signal_cooldown_minutes):
                continue
            if self.db.signals_today_count(signal.symbol) >= self.config.max_signals_per_symbol_per_day:
                continue
            if same_direction_counts[signal.side] >= self.config.max_same_direction_signals:
                continue
            self.telegram.send_message(format_signal_message(signal))
            self.db.save_signal(signal)
            same_direction_counts[signal.side] += 1
            sent += 1
        if sent == 0 and send_empty_report:
            self.telegram.send_message(
                "📊 Tarama tamamlandı, yeni sinyal yok.\n\n"
                f"{self.engine.last_market_condition_summary}\n\n"
                "(Detaylı sayılar için /status)"
            )

    def status_text(self) -> str:
        tp2_count, sl_count, open_count = self.db.performance_summary()
        closed_total = tp2_count + sl_count
        win_rate_line = ""
        if closed_total > 0:
            win_rate = tp2_count / closed_total * 100
            win_rate_line = f"TP2/SL kapanan: {closed_total} (TP2: {tp2_count}, SL: {sl_count}) — kazanma oranı: {win_rate:.0f}%\n"
        return (
            "✅ Bot çalışıyor\n\n"
            f"{self.engine.last_scan_summary}\n\n"
            f"Toplam kayıtlı sinyal: {self.db.total_signals()}\n"
            f"Açık pozisyon: {open_count}\n"
            f"{win_rate_line}"
            f"Minimum confidence: {self.config.min_confidence:.0f}\n"
            f"Günlük coin limiti: {self.config.max_signals_per_symbol_per_day}\n"
            f"Tarama aralığı: {self.config.scan_interval_seconds} sn"
        )


def format_signal_message(signal: Signal) -> str:
    icon = "🟢" if signal.side == "LONG" else "🔴"
    return (
        "🚨 <b>NEW SIGNAL</b>\n\n"
        f"{icon} <b>{signal.side}</b>\n\n"
        f"🪙 Coin: <b>{signal.symbol}</b>\n"
        f"⭐ Confidence: <b>{signal.confidence:.0f}/100</b>\n"
        f"💰 Entry: <code>{format_price(signal.entry)}</code>\n"
        f"🛑 Stop Loss: <code>{format_price(signal.stop_loss)}</code>\n\n"
        f"🎯 TP1: <code>{format_price(signal.tp1)}</code>\n"
        f"🎯 TP2: <code>{format_price(signal.tp2)}</code>\n\n"
        f"⚡ Leverage: <b>{signal.leverage}x</b>\n"
        f"📊 Risk/Reward: <b>{signal.risk_reward:.2f}</b>\n"
        f"₿ BTC Status: <b>{html_escape(signal.btc_status)}</b>\n\n"
        f"{format_reasons(signal.reasons)}\n\n"
        f"🕒 Signal Time: {local_now_text()}"
    )


def format_tp_hit_message(row: sqlite3.Row, price: float, level: int) -> str:
    entry = row["entry"]
    side = row["side"]
    leverage = row["leverage"] or 1
    raw_pct = (price - entry) / entry * 100 if side == "LONG" else (entry - price) / entry * 100
    leveraged_pct = raw_pct * leverage
    final_note = "\n\n✅ Pozisyon tamamen kapandı." if level == 2 else "\n\nℹ️ TP2 ve Stop için takip devam ediyor."
    return (
        f"🎯 <b>TP{level} HIT</b>\n\n"
        f"🪙 Coin: <b>{row['symbol']}</b>\n"
        f"{'🟢' if side == 'LONG' else '🔴'} Yön: <b>{side}</b>\n"
        f"💰 Giriş: <code>{format_price(entry)}</code>\n"
        f"🎯 TP{level} Fiyatı: <code>{format_price(price)}</code>\n"
        f"📈 Kazanç: <b>+{raw_pct:.2f}%</b> (kaldıraçlı ~<b>+{leveraged_pct:.2f}%</b>, {leverage}x)"
        f"{final_note}\n\n"
        f"{format_position_timing(row)}"
    )


def format_sl_hit_message(row: sqlite3.Row, price: float) -> str:
    entry = row["entry"]
    side = row["side"]
    leverage = row["leverage"] or 1
    raw_pct = (price - entry) / entry * 100 if side == "LONG" else (entry - price) / entry * 100
    leveraged_pct = raw_pct * leverage
    return (
        "🛑 <b>STOP LOSS HIT</b>\n\n"
        f"🪙 Coin: <b>{row['symbol']}</b>\n"
        f"{'🟢' if side == 'LONG' else '🔴'} Yön: <b>{side}</b>\n"
        f"💰 Giriş: <code>{format_price(entry)}</code>\n"
        f"🛑 Stop Fiyatı: <code>{format_price(price)}</code>\n"
        f"📉 Kayıp: <b>{raw_pct:.2f}%</b> (kaldıraçlı ~<b>{leveraged_pct:.2f}%</b>, {leverage}x)\n\n"
        "✅ Pozisyon kapandı.\n\n"
        f"{format_position_timing(row)}"
    )


def format_breakeven_hit_message(row: sqlite3.Row, price: float) -> str:
    entry = row["entry"]
    side = row["side"]
    leverage = row["leverage"] or 1
    raw_pct = (price - entry) / entry * 100 if side == "LONG" else (entry - price) / entry * 100
    leveraged_pct = raw_pct * leverage
    return (
        "⚪️ <b>BREAKEVEN (TP1 sonrası)</b>\n\n"
        f"🪙 Coin: <b>{row['symbol']}</b>\n"
        f"{'🟢' if side == 'LONG' else '🔴'} Yön: <b>{side}</b>\n"
        f"💰 Giriş: <code>{format_price(entry)}</code>\n"
        f"⚪️ Kapanış Fiyatı: <code>{format_price(price)}</code>\n"
        f"📊 Net: <b>{raw_pct:+.2f}%</b> (kaldıraçlı ~<b>{leveraged_pct:+.2f}%</b>, {leverage}x)\n\n"
        "✅ TP1 kârı korundu, pozisyon başabaşta kapandı (tam SL değil).\n\n"
        f"{format_position_timing(row)}"
    )


def format_reasons(reasons: Iterable[str]) -> str:
    # This used to whitelist only 4 reason strings (Trend/Momentum/Structure/4H) -
    # every other signal the bot actually checks (BTC status, US equities, DXY,
    # liquidity sweep, support/resistance zone, funding, long/short ratio) was
    # silently dropped here and never reached the Telegram message, even though
    # _score_direction was already computing all of it. Expanded to show everything
    # that's a genuine positive confirmation; still excludes negative/conflict
    # reasons (those already reduced the score - showing them under a "Confirmed"
    # checklist would be misleading) and "Open interest available" (that's data
    # availability, not a directional confirmation).
    allowed = {
        "Trend confirmed": "✅ Trend Confirmed",
        "Momentum confirmed": "✅ Momentum Confirmed",
        "Market structure confirmed": "✅ Structure Confirmed",
        "Higher timeframe (4h) trend aligned": "✅ 4H Trend Confirmed",
        "US equities (S&P/NASDAQ) aligned": "✅ US Equities Confirmed",
        "Dolar endeksi (DXY) destekleyici": "✅ DXY Confirmed",
        "Likidite avı (sweep) bu yönü destekliyor": "✅ Liquidity Sweep Confirmed",
        "Sürmekte olan likidite avı yapısı bu yönü destekliyor": "✅ Liquidity Sweep Confirmed",
        "Test edilmiş destek bölgesinden dönüş": "✅ Support Zone Confirmed",
        "Test edilmiş direnç bölgesinden dönüş": "✅ Resistance Zone Confirmed",
        "Eski (uzun vadeli) likidite bölgesinden dönüş": "✅ Long-Term Liquidity Zone Confirmed",
        "BTC dominansı düşüyor (alt rotasyonu destekleyici)": "✅ BTC Dominance Confirmed",
        "BTC dominansı yükseliyor (alt rotasyonu ters yönde)": "✅ BTC Dominance Confirmed",
        "USDT dominansı düşüyor (risk iştahı destekleyici)": "✅ USDT Dominance Confirmed",
        "USDT dominansı yükseliyor (risk iştahı azalıyor)": "✅ USDT Dominance Confirmed",
        "Trend strength acceptable": "✅ Trend Strength Confirmed",
        "Volatility controlled": "✅ Volatility Confirmed",
        "Funding acceptable": "✅ Funding Confirmed",
        "Long/short oranı LONG lehine destekleyici": "✅ Long/Short Ratio Confirmed",
        "Long/short oranı SHORT lehine destekleyici": "✅ Long/Short Ratio Confirmed",
    }
    lines = [allowed[reason] for reason in reasons if reason in allowed]
    if any(reason.startswith("Volume confirmed") for reason in reasons):
        lines.append("✅ Volume Confirmed")
    if any(reason.startswith("BTC status supportive") for reason in reasons):
        lines.append("✅ BTC Confirmed")
    if not lines:
        lines.append("✅ Multi-layer validation passed")
    return "\n".join(lines)


def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} .env içinde doldurulmalı.")
    return value


def get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def format_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def local_now_text() -> str:
    # Telegram shows message-delivery time in the recipient's local timezone.
    # Displaying the signal time in that same local timezone (instead of UTC)
    # avoids the confusing "message says 16:29 but arrived at 19:29" mismatch —
    # both were always the same instant, just two different timezone labels.
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M (TR)")


def format_local_time(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, LOCAL_TZ).strftime("%Y-%m-%d %H:%M (TR)")


def format_position_timing(row: sqlite3.Row) -> str:
    """Açılış/kapanış saatleri ve pozisyonun ne kadar açık kaldığı — TP/SL/breakeven
    mesajlarında gösterilir."""
    opened_at = row["created_at"]
    closed_at = int(time.time())
    duration_seconds = max(0, closed_at - opened_at)
    hours, remainder = divmod(duration_seconds, 3600)
    minutes = remainder // 60
    duration_text = f"{hours} sa {minutes} dk" if hours else f"{minutes} dk"
    return (
        f"🕒 Açılış: {format_local_time(opened_at)}\n"
        f"🕒 Kapanış: {local_now_text()}\n"
        f"⏱️ Süre: {duration_text}"
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
    )


def main() -> None:
    try:
        config = Config.load()
        configure_logging(config.log_level)
        SignalBot(config).run()
    except Exception as exc:
        print("Bot başlatılamadı:", exc)
        print(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

