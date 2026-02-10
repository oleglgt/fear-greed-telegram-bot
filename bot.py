import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

CNN_API_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_API_URL = "https://api.alternative.me/fng/?limit=1"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
COINGECKO_BTC_URL = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_BTC_URL = "https://api.coinbase.com/v2/prices/spot"
STOOQ_SPX_CSV_URL = "https://stooq.com/q/l/?s=%5Espx&i=d"
FRED_SPX_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
BOT_VERSION = "v1.5.4"
REQUEST_HEADERS = {
    # CNN often blocks non-browser default clients (python-requests).
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}
LAST_BTC_PRICE: float | None = None
LAST_SPX_PRICE: float | None = None
CYPRUS_TZ = ZoneInfo("Europe/Nicosia")


def with_version(text: str) -> str:
    return f"[{BOT_VERSION}]\n{text}"


def parse_timestamp_utc(timestamp_raw: object) -> datetime:
    """
    CNN may return timestamp as int/float/string.
    Supports unix seconds, unix milliseconds and ISO datetime strings.
    """
    raw = str(timestamp_raw).strip()
    try:
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.utcfromtimestamp(ts)
    except ValueError:
        # Example: 2026-02-09T20:08:11+00:00 or ...Z
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt


def get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return token

    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "TELEGRAM_BOT_TOKEN":
                    cleaned = value.strip().strip('"').strip("'")
                    if cleaned:
                        return cleaned

    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment or .env file.")


def fetch_fear_and_greed() -> tuple[float, str, str]:
    response = requests.get(CNN_API_URL, headers=REQUEST_HEADERS, timeout=15)
    response.raise_for_status()
    data = response.json()

    score = data["fear_and_greed"]["score"]
    rating = data["fear_and_greed"]["rating"]
    timestamp_raw = data["fear_and_greed"]["timestamp"]
    dt_utc = parse_timestamp_utc(timestamp_raw)
    updated_at = dt_utc.strftime("%Y-%m-%d %H:%M UTC")

    return float(score), str(rating), updated_at


def fetch_crypto_fear_and_greed() -> tuple[int, str, str]:
    response = requests.get(CRYPTO_API_URL, timeout=15)
    response.raise_for_status()
    data = response.json()

    latest = data["data"][0]
    score = int(latest["value"])
    rating = str(latest["value_classification"])
    timestamp_raw = latest["timestamp"]
    dt_utc = parse_timestamp_utc(timestamp_raw)
    updated_at = dt_utc.strftime("%Y-%m-%d %H:%M UTC")

    return score, rating, updated_at


def fetch_market_prices() -> tuple[float, float]:
    global LAST_BTC_PRICE, LAST_SPX_PRICE

    btc_price: float | None = None
    spx_price: float | None = None

    # Primary source: Yahoo Finance (both symbols in one call).
    try:
        params = {"symbols": "BTC-USD,^GSPC"}
        response = requests.get(
            YAHOO_QUOTE_URL, params=params, headers=REQUEST_HEADERS, timeout=15
        )
        response.raise_for_status()
        data = response.json()

        results = data["quoteResponse"]["result"]
        for item in results:
            symbol = item.get("symbol")
            price = item.get("regularMarketPrice")
            if symbol == "BTC-USD" and price is not None:
                btc_price = float(price)
            if symbol == "^GSPC" and price is not None:
                spx_price = float(price)
    except Exception:
        pass

    # BTC fallback 1: Coinbase spot API.
    if btc_price is None:
        try:
            btc_response = requests.get(
                COINBASE_BTC_URL, params={"currency": "USD"}, timeout=15
            )
            btc_response.raise_for_status()
            btc_data = btc_response.json()
            btc_price = float(btc_data["data"]["amount"])
        except Exception:
            pass

    # BTC fallback 2: CoinGecko.
    if btc_price is None:
        try:
            btc_response = requests.get(
                COINGECKO_BTC_URL,
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=15,
            )
            btc_response.raise_for_status()
            btc_data = btc_response.json()
            btc_price = float(btc_data["bitcoin"]["usd"])
        except Exception:
            pass

    # S&P fallback 1: Stooq (^SPX close price from CSV).
    if spx_price is None:
        try:
            spx_response = requests.get(STOOQ_SPX_CSV_URL, timeout=15)
            spx_response.raise_for_status()
            lines = [line.strip() for line in spx_response.text.splitlines() if line.strip()]
            if len(lines) >= 2:
                row = lines[1].split(",")
                # CSV columns: Symbol,Date,Time,Open,High,Low,Close,Volume
                if len(row) > 6 and row[6] not in {"", "N/D"}:
                    spx_price = float(row[6])
        except Exception:
            pass

    # S&P fallback 2: FRED daily S&P500 series (no API key).
    if spx_price is None:
        try:
            fred_response = requests.get(FRED_SPX_CSV_URL, timeout=15)
            fred_response.raise_for_status()
            # CSV columns: DATE,SP500
            rows = [line.strip() for line in fred_response.text.splitlines() if line.strip()]
            # Walk backwards and take latest non-empty numeric value.
            for line in reversed(rows[1:]):
                parts = line.split(",")
                if len(parts) >= 2 and parts[1] not in {"", "."}:
                    spx_price = float(parts[1])
                    break
        except Exception:
            pass

    # Last-resort fallback: last successful values in memory.
    if btc_price is None:
        btc_price = LAST_BTC_PRICE
    if spx_price is None:
        spx_price = LAST_SPX_PRICE

    if btc_price is None or spx_price is None:
        raise ValueError("не удалось получить цены BTC/S&P ни из одного источника")

    LAST_BTC_PRICE = btc_price
    LAST_SPX_PRICE = spx_price

    return btc_price, spx_price


def build_report_text() -> str:
    try:
        score, rating, updated_at = fetch_fear_and_greed()
        stock_block = f"Stock Fear & Greed (CNN): {score:.2f} {rating} {updated_at}"
    except Exception as exc:
        stock_block = f"Stock Fear & Greed (CNN): ошибка ({exc})"

    try:
        c_score, c_rating, c_updated_at = fetch_crypto_fear_and_greed()
        crypto_block = f"Crypto Fear & Greed: {c_score} {c_rating} {c_updated_at}"
    except Exception as exc:
        crypto_block = f"Crypto Fear & Greed: ошибка ({exc})"

    try:
        btc_price, spx_price = fetch_market_prices()
        prices_block = (
            f"Bitcoin (BTC-USD): ${btc_price:,.2f}\n"
            f"S&P 500 (^GSPC): {spx_price:,.2f}"
        )
    except Exception as exc:
        prices_block = f"Рыночные цены: временно недоступны ({exc})"

    return with_version(f"{stock_block}\n\n{crypto_block}\n\n{prices_block}")


def get_target_chat_id() -> int | None:
    raw = os.getenv("TELEGRAM_TARGET_CHAT_ID", "").strip()
    if not raw:
        return None
    return int(raw)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        with_version(
            "Привет! Я показываю Fear & Greed Index.\n"
            "Команды:\n"
            "/fg - stock + crypto в одном сообщении\n\n"
            "Авто-отправка в 08:00 и 20:00 (Кипр) работает, если в Render задана "
            "переменная TELEGRAM_TARGET_CHAT_ID."
        )
    )


async def fg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_report_text())


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    message = update.effective_message
    if message is not None:
        await message.reply_text(with_version(f"Твой chat_id: {chat_id}"))
        return
    # Fallback for rare update types without effective_message.
    if chat_id is not None:
        await context.bot.send_message(
            chat_id=chat_id, text=with_version(f"Твой chat_id: {chat_id}")
        )


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    await context.bot.send_message(chat_id=chat_id, text=build_report_text())


async def on_startup(app) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "помощь"),
            BotCommand("fg", "stock + crypto Fear & Greed"),
            BotCommand("myid", "показать chat_id"),
            BotCommand("id", "показать chat_id (alias)"),
        ]
    )
    target_chat_id = get_target_chat_id()
    if target_chat_id and app.job_queue is not None:
        app.job_queue.run_daily(
            scheduled_report,
            time=time(hour=8, minute=0, tzinfo=CYPRUS_TZ),
            data=target_chat_id,
            name="daily_report_0800_cyprus",
        )
        app.job_queue.run_daily(
            scheduled_report,
            time=time(hour=20, minute=0, tzinfo=CYPRUS_TZ),
            data=target_chat_id,
            name="daily_report_2000_cyprus",
        )


def main() -> None:
    token = get_token()

    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fg", fg))
    app.add_handler(CommandHandler(["myid", "id", "chatid"], myid))

    app.run_polling()


if __name__ == "__main__":
    main()
