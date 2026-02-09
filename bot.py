import os
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

CNN_API_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_API_URL = "https://api.alternative.me/fng/?limit=1"
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я показываю Fear & Greed Index.\n"
        "Команды:\n"
        "/fg - stock (CNN)\n"
        "/crypto или /cfg - crypto"
    )


async def fg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        score, rating, updated_at = fetch_fear_and_greed()
        await update.message.reply_text(
            f"Fear & Greed Index: {score:.2f}\n"
            f"Состояние: {rating}\n"
            f"Обновлено: {updated_at}"
        )
    except Exception as exc:
        await update.message.reply_text(f"Не удалось получить данные: {exc}")


async def crypto_fg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        score, rating, updated_at = fetch_crypto_fear_and_greed()
        await update.message.reply_text(
            f"Crypto Fear & Greed Index: {score}\n"
            f"Состояние: {rating}\n"
            f"Обновлено: {updated_at}"
        )
    except Exception as exc:
        await update.message.reply_text(f"Не удалось получить crypto данные: {exc}")


def main() -> None:
    token = get_token()

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fg", fg))
    app.add_handler(CommandHandler(["crypto", "cfg"], crypto_fg))

    app.run_polling()


if __name__ == "__main__":
    main()
