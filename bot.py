import os
import io
import re
import zipfile
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

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
FRANKFURTER_LATEST_URL = "https://api.frankfurter.app/latest"
OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/EUR"
WDD_RESERVOIRS_PAGE_URL = (
    "https://www.moa.gov.cy/moa/wdd/Wdd.nsf/page18_en/page18_en?opendocument"
)
BOT_VERSION = "v1.7.0"
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
SCHEDULER_STATUS = "not-initialized"


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
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except ValueError:
        # Example: 2026-02-09T20:08:11+00:00 or ...Z
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)


def format_cyprus_time(dt_utc: datetime) -> str:
    dt_cy = dt_utc.astimezone(CYPRUS_TZ)
    return f"{dt_cy.day} {dt_cy.strftime('%b %H:%M')}"


def format_cyprus_date(dt_utc: datetime) -> str:
    dt_cy = dt_utc.astimezone(CYPRUS_TZ)
    return f"{dt_cy.day} {dt_cy.strftime('%b')}"


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


def get_url_text(url: str) -> str:
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.exceptions.SSLError:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30, verify=False)
        response.raise_for_status()
        return response.text


def get_url_bytes(url: str) -> bytes:
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        return response.content
    except requests.exceptions.SSLError:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=30, verify=False)
        response.raise_for_status()
        return response.content


def col_from_ref(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def parse_wdd_report_date(text: str) -> str:
    # Usually in URL/file name: 18-FEB-2026
    match = re.search(r"(\\d{1,2})-([A-Z]{3})-(\\d{4})", text)
    if not match:
        return "n/a"
    day, mon, year = match.groups()
    dt = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y").replace(
        tzinfo=timezone.utc
    )
    return f"{dt.day} {dt.strftime('%b %Y')}"


def read_xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall(f"{ns}si"):
        parts = [t.text or "" for t in si.iter(f"{ns}t")]
        strings.append("".join(parts))
    return strings


def read_xlsx_first_sheet_rows(xlsx_bytes: bytes) -> dict[int, dict[str, object]]:
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        shared = read_xlsx_shared_strings(zf)
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        first_sheet = workbook.find(f"{ns_main}sheets/{ns_main}sheet")
        if first_sheet is None:
            raise ValueError("No worksheet in WDD xlsx")
        rel_id = first_sheet.attrib[f"{ns_rel}id"]
        sheet_target = rel_map[rel_id]
        sheet_xml = ET.fromstring(zf.read(f"xl/{sheet_target}"))

        rows_data: dict[int, dict[str, object]] = {}
        for row in sheet_xml.findall(f".//{ns_main}row"):
            row_num = int(row.attrib["r"])
            values: dict[str, object] = {}
            for cell in row.findall(f"{ns_main}c"):
                ref = cell.attrib.get("r")
                if not ref:
                    continue
                col = col_from_ref(ref)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{ns_main}v")
                inline_node = cell.find(f"{ns_main}is")
                if value_node is not None and value_node.text is not None:
                    raw = value_node.text
                    if cell_type == "s":
                        values[col] = shared[int(raw)]
                    else:
                        try:
                            values[col] = float(raw)
                        except ValueError:
                            values[col] = raw
                elif inline_node is not None:
                    text_node = inline_node.find(f".//{ns_main}t")
                    if text_node is not None and text_node.text is not None:
                        values[col] = text_node.text
            if values:
                rows_data[row_num] = values
        return rows_data


def fetch_cyprus_reservoirs_summary() -> str:
    html = get_url_text(WDD_RESERVOIRS_PAGE_URL)

    # WDD sometimes serves mixed href casing/quoting/URL styles.
    href_matches = re.findall(
        r"""href\s*=\s*(['"])(.*?)\1""",
        html,
        flags=re.IGNORECASE,
    )
    xlsx_candidates = []
    for _, href in href_matches:
        low = href.lower()
        if ".xlsx" not in low:
            continue
        if "uk.xlsx" in low and "graphs" not in low:
            xlsx_candidates.append(href)
    if not xlsx_candidates:
        raise ValueError("WDD latest UK.xlsx link not found")

    xlsx_url = urljoin(WDD_RESERVOIRS_PAGE_URL, xlsx_candidates[0])
    report_date = parse_wdd_report_date(xlsx_url)
    xlsx_bytes = get_url_bytes(xlsx_url)
    rows = read_xlsx_first_sheet_rows(xlsx_bytes)

    grand_total_row = None
    for row_num, row_values in rows.items():
        if str(row_values.get("B", "")).strip() == "GRAND TOTAL":
            grand_total_row = row_values
            break
    if grand_total_row is None:
        raise ValueError("GRAND TOTAL row not found in WDD xlsx")

    since_from = str(rows.get(15, {}).get("G", "n/a"))
    inflow_24h = float(grand_total_row["F"])
    inflow_since = float(grand_total_row["G"])
    current_mcm = float(grand_total_row["H"])
    current_pct = float(grand_total_row["I"])
    last_year_mcm = float(grand_total_row["J"])
    last_year_pct = float(grand_total_row["K"])

    return (
        "Cyprus reservoirs:\n"
        f"Inflow: +{inflow_24h:.3f} MCM (24h), +{inflow_since:.3f} MCM (since {since_from})\n"
        f"Now: {current_mcm:.3f} MCM ({current_pct:.2f}%)\n"
        f"Last year: {last_year_mcm:.3f} MCM ({last_year_pct:.2f}%)\n"
        f"Report date: {report_date}"
    )


def fetch_fear_and_greed() -> tuple[float, str, str]:
    response = requests.get(CNN_API_URL, headers=REQUEST_HEADERS, timeout=15)
    response.raise_for_status()
    data = response.json()

    score = data["fear_and_greed"]["score"]
    rating = data["fear_and_greed"]["rating"]
    timestamp_raw = data["fear_and_greed"]["timestamp"]
    dt_utc = parse_timestamp_utc(timestamp_raw)
    updated_at = format_cyprus_time(dt_utc)

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
    updated_at = format_cyprus_time(dt_utc)

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


def fetch_fx_rates() -> tuple[float, float, str]:
    """
    Returns:
        EUR/USD, EUR/RUB and source descriptor
    """
    # Primary source: Yahoo FX (more оперативный intraday feed).
    # We support both direct EURRUB and synthetic EURUSD * USDRUB paths.
    try:
        response = requests.get(
            YAHOO_QUOTE_URL,
            params={"symbols": "EURUSD=X,EURRUB=X,RUB=X,USDRUB=X"},
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        results = data["quoteResponse"]["result"]
        eur_usd = None
        eur_rub_direct = None
        usd_rub = None
        market_time = None
        for item in results:
            symbol = item.get("symbol")
            price = item.get("regularMarketPrice")
            if symbol == "EURUSD=X" and price is not None:
                eur_usd = float(price)
                market_time = item.get("regularMarketTime", market_time)
            if symbol == "EURRUB=X" and price is not None:
                eur_rub_direct = float(price)
                market_time = item.get("regularMarketTime", market_time)
            # Yahoo commonly returns RUB=X as USD/RUB (RUB per 1 USD).
            if symbol == "RUB=X" and price is not None:
                usd_rub = float(price)
                market_time = item.get("regularMarketTime", market_time)
            if symbol == "USDRUB=X" and price is not None:
                usd_rub = float(price)
                market_time = item.get("regularMarketTime", market_time)

        if eur_usd and eur_usd > 0:
            eur_rub = None
            source = "Yahoo FX"
            if eur_rub_direct and eur_rub_direct > 0:
                eur_rub = eur_rub_direct
                source = "Yahoo FX (direct EURRUB)"
            elif usd_rub and usd_rub > 0:
                eur_rub = eur_usd * usd_rub
                source = "Yahoo FX (EURUSD x USDRUB)"

            if eur_rub and eur_rub > 0:
                if market_time:
                    dt_utc = datetime.fromtimestamp(float(market_time), tz=timezone.utc)
                    source = f"{source}, {format_cyprus_time(dt_utc)}"
                return eur_usd, eur_rub, source
    except Exception:
        pass

    # Fallback 1: Frankfurter/ECB (reference, usually daily).
    try:
        response = requests.get(
            FRANKFURTER_LATEST_URL,
            params={"from": "EUR", "to": "USD,RUB"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        rates = data["rates"]
        eur_usd = float(rates["USD"])
        eur_rub = float(rates["RUB"])
        if eur_usd > 0 and eur_rub > 0:
            date_raw = str(data.get("date", "")).strip()
            source = "Frankfurter/ECB"
            if date_raw:
                try:
                    dt_utc = datetime.strptime(date_raw, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    source = f"{source}, {format_cyprus_date(dt_utc)}"
                except ValueError:
                    source = f"{source}, {date_raw}"
            return eur_usd, eur_rub, source
    except Exception:
        pass

    # Fallback 2: open.er-api (when other sources don't include RUB).
    response = requests.get(OPEN_ER_API_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    rates = data["rates"]
    eur_usd = float(rates["USD"])
    eur_rub = float(rates["RUB"])
    if eur_usd <= 0 or eur_rub <= 0:
        raise ValueError("invalid FX rates")

    updated_raw = data.get("time_last_update_utc")
    source = "open.er-api"
    if updated_raw:
        try:
            dt_utc = datetime.strptime(
                str(updated_raw), "%a, %d %b %Y %H:%M:%S %z"
            ).astimezone(timezone.utc)
            source = f"{source}, {format_cyprus_time(dt_utc)}"
        except ValueError:
            source = f"{source}, {updated_raw}"

    return eur_usd, eur_rub, source


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

    try:
        eur_usd, eur_rub, fx_source = fetch_fx_rates()
        fx_block = (
            f"EUR/USD: {eur_usd:.5f}\n"
            f"EUR/RUB: {eur_rub:.5f}\n"
            f"FX source: {fx_source}"
        )
    except Exception as exc:
        fx_block = f"FX курсы: временно недоступны ({exc})"

    try:
        reservoirs_block = fetch_cyprus_reservoirs_summary()
    except Exception as exc:
        reservoirs_block = f"Cyprus reservoirs: временно недоступно ({exc})"

    return with_version(
        f"{stock_block}\n\n{crypto_block}\n\n{prices_block}\n\n{fx_block}\n\n{reservoirs_block}"
    )


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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_chat_id = os.getenv("TELEGRAM_TARGET_CHAT_ID", "(not set)")
    has_job_queue = "yes" if context.application.job_queue is not None else "no"
    jobs = context.application.job_queue.jobs() if context.application.job_queue else []
    job_names = ", ".join(job.name for job in jobs) if jobs else "(none)"
    await update.effective_message.reply_text(
        with_version(
            "Статус бота:\n"
            f"- Scheduler: {SCHEDULER_STATUS}\n"
            f"- job_queue available: {has_job_queue}\n"
            f"- TELEGRAM_TARGET_CHAT_ID: {target_chat_id}\n"
            f"- jobs: {job_names}"
        )
    )


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    await context.bot.send_message(chat_id=chat_id, text=build_report_text())


async def on_startup(app) -> None:
    global SCHEDULER_STATUS
    await app.bot.set_my_commands(
        [
            BotCommand("start", "помощь"),
            BotCommand("fg", "stock + crypto Fear & Greed"),
            BotCommand("myid", "показать chat_id"),
            BotCommand("id", "показать chat_id (alias)"),
            BotCommand("status", "статус scheduler и env"),
        ]
    )
    target_chat_id = get_target_chat_id()
    if not target_chat_id:
        SCHEDULER_STATUS = "disabled: TELEGRAM_TARGET_CHAT_ID is not set"
        return
    if app.job_queue is None:
        SCHEDULER_STATUS = "disabled: job_queue unavailable (install ptb[job-queue])"
        return

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
    SCHEDULER_STATUS = "enabled: 08:00 and 20:00 Europe/Nicosia"


def main() -> None:
    token = get_token()

    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fg", fg))
    app.add_handler(CommandHandler(["myid", "id", "chatid"], myid))
    app.add_handler(CommandHandler("status", status))

    app.run_polling()


if __name__ == "__main__":
    main()
