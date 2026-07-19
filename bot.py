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
    position_check_interval_seconds: int = 15
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
            position_check_interval_seconds=get_int_env("POSITION_CHECK_INTERVAL_SECONDS", 15),
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

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "professional-signal-bot/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


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

    def signals_today_count(self, symbol: str) -> int:
        """Rolling 24h count of signals sent for a symbol, regardless of side."""
        cutoff = int(time.time()) - 24 * 60 * 60
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE symbol = ? AND created_at >= ?",
                (symbol, cutoff),
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
        if len(candles) <= period + 1:
            return 0.0
        plus_dm = []
        minus_dm = []
        true_ranges = []
        recent = candles[-period - 1 :]
        for current, previous in zip(recent[1:], recent[:-1]):
            up_move = current.high - previous.high
            down_move = previous.low - current.low
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
            true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
        atr_value = sum(true_ranges) or 1
        plus_di = 100 * sum(plus_dm) / atr_value
        minus_di = 100 * sum(minus_dm) / atr_value
        if plus_di + minus_di == 0:
            return 0.0
        return 100 * abs(plus_di - minus_di) / (plus_di + minus_di)


class ProfessionalSignalEngine:
    def __init__(self, binance: BinanceFuturesClient, config: Config) -> None:
        self.binance = binance
        self.config = config
        self.last_scan_summary = "Henüz tarama yapılmadı."

    def scan(self) -> list[Signal]:
        started = time.time()
        candidates = self._fast_filter_symbols()
        btc_health = self._analyze_btc()
        signals = []
        rejected = 0

        for market_symbol in candidates:
            if market_symbol.symbol == "BTCUSDT":
                continue
            try:
                signal = self._analyze_symbol(market_symbol.symbol, btc_health)
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
        self.last_scan_summary = (
            f"Son tarama: {local_now_text()}\n"
            f"BTC: {btc_health.status} ({btc_health.direction}, skor {btc_health.score:.0f})\n"
            f"Analiz edilen: {len(candidates)}\n"
            f"Reddedilen: {rejected}\n"
            f"Sinyal adayı: {len(signals)}\n"
            f"Süre: {elapsed} sn"
        )
        return signals

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
        score = 50
        details = []

        if price > ema50 > ema200:
            score += 22
            direction = "Bullish"
            details.append("BTC EMA yapısı pozitif")
        elif price < ema50 < ema200:
            score += 22
            direction = "Bearish"
            details.append("BTC EMA yapısı negatif")
        else:
            direction = "Mixed"
            details.append("BTC trendi karışık")

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
        pct_change_24h = (closes[-1] - closes[-25]) / closes[-25] if len(closes) >= 25 and closes[-25] > 0 else 0.0
        return BtcHealth(status=status, direction=direction, score=score, volatility=volatility, details=details, pct_change_24h=pct_change_24h)

    def _analyze_symbol(self, symbol: str, btc: BtcHealth) -> Signal | None:
        candles_15m = self.binance.klines(symbol, "15m", 210)
        candles_1h = self.binance.klines(symbol, "1h", 210)
        candles_4h = self.binance.klines(symbol, "4h", 210)
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

        long_gate_ok = self._passes_hard_filters(
            True, price, ema20[-1], ema50[-1], ema200_1h[-1], adx, structure, htf_trend, btc, extension_pct, relative_strength_vs_btc
        )
        short_gate_ok = self._passes_hard_filters(
            False, price, ema20[-1], ema50[-1], ema200_1h[-1], adx, structure, htf_trend, btc, extension_pct, relative_strength_vs_btc
        )

        long_score, long_reasons = (
            self._score_direction(
                "LONG", price, ema20[-1], ema50[-1], ema200_1h[-1], rsi, macd_line, macd_signal, macd_hist,
                adx, rel_volume, funding, open_interest, structure, volatility, btc, htf_trend,
            )
            if long_gate_ok else (0.0, [])
        )
        short_score, short_reasons = (
            self._score_direction(
                "SHORT", price, ema20[-1], ema50[-1], ema200_1h[-1], rsi, macd_line, macd_signal, macd_hist,
                adx, rel_volume, funding, open_interest, structure, volatility, btc, htf_trend,
            )
            if short_gate_ok else (0.0, [])
        )

        if max(long_score, short_score) < self.config.min_confidence:
            return None
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
    ) -> bool:
        """A direction must clear ALL of these before its weighted score even counts.
        This used to all be soft, additive scoring (lose some points here, make it up
        there) — which let setups through where only some things lined up. These are
        the conditions that mattered most for actually reading the market correctly,
        so now they gate the direction out entirely rather than just costing points."""
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
        # BTC's own trend directly opposing the trade direction used to only cost
        # points (soft penalty), not block the trade. Alts overwhelmingly follow BTC's
        # dominant move — a short fired while BTC is trending up (or a long while BTC
        # is trending down) is fighting the tape, which is exactly the whipsaw pattern
        # described: shorts run over as BTC ripped up, then longs given right as BTC
        # rolled over. "Mixed" BTC direction still allows either side.
        if btc.direction == ("Bearish" if bullish else "Bullish"):
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
        structure: str,
        volatility: float,
        btc: BtcHealth,
        htf_trend: str,
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
        reward_1 = risk_distance * 1.45
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
                time.sleep(2)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logging.exception("Main loop error")
                self.telegram.send_message(f"⚠️ Bot hata yakaladı ve çalışmaya devam ediyor:\n{html_escape(str(exc))}")
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
        tp2_rows = [r for r in rows if r["status"] == "TP2"]
        sl_rows = [r for r in rows if r["status"] == "SL"]
        be_rows = [r for r in rows if r["status"] == "BE"]
        open_rows = [r for r in rows if r["status"] == "OPEN"]
        closed_rows = tp2_rows + sl_rows + be_rows

        lines = [title, ""]
        lines.append(f"📡 Gönderilen sinyal: <b>{total}</b> (LONG {long_count} / SHORT {short_count})")
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
        Does not account for US market holidays."""
        ny_time = now_local.astimezone(cls.US_MARKET_OPEN_TZ)
        if ny_time.weekday() >= 5:  # Saturday/Sunday -> US market isn't open anyway
            return False
        window_start = ny_time.replace(hour=9, minute=15, second=0, microsecond=0)
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
        for signal in signals[:5]:
            if self.db.has_open_position(signal.symbol):
                continue
            if self.db.recently_sent(signal.symbol, signal.side, self.config.signal_cooldown_minutes):
                continue
            if self.db.signals_today_count(signal.symbol) >= self.config.max_signals_per_symbol_per_day:
                continue
            self.telegram.send_message(format_signal_message(signal))
            self.db.save_signal(signal)
            sent += 1
        if sent == 0 and send_empty_report:
            self.telegram.send_message(
                "📊 Tarama tamamlandı, yeni sinyal yok.\n\n"
                f"{self.engine.last_scan_summary}\n\n"
                "Zayıf veya tekrarlı fırsatlar elendi."
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
    allowed = {
        "Trend confirmed": "✅ Trend Confirmed",
        "Momentum confirmed": "✅ Momentum Confirmed",
        "Market structure confirmed": "✅ Structure Confirmed",
        "Higher timeframe (4h) trend aligned": "✅ 4H Trend Confirmed",
    }
    lines = [allowed[reason] for reason in reasons if reason in allowed]
    if any(reason.startswith("Volume confirmed") for reason in reasons):
        lines.append("✅ Volume Confirmed")
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

