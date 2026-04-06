import asyncio
import logging
import hashlib
import os
import io
import json
import re
import textwrap
import time as time_module
import zipfile
from datetime import datetime, time, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from html import escape as html_escape, unescape
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

CNN_API_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_API_URL = "https://api.alternative.me/fng/?limit=1"
YAHOO_QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
COINGECKO_BTC_URL = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_BTC_URL = "https://api.coinbase.com/v2/prices/spot"
STOOQ_SPX_CSV_URL = "https://stooq.com/q/l/?s=%5Espx&i=1"
FRED_SPX_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
FRANKFURTER_LATEST_URL = "https://api.frankfurter.app/latest"
OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/EUR"
STOOQ_EURUSD_CSV_URL = "https://stooq.com/q/l/?s=eurusd&i=1"
STOOQ_EURRUB_CSV_URL = "https://stooq.com/q/l/?s=eurrub&i=1"
WDD_RESERVOIRS_PAGE_URL = (
    "https://www.moa.gov.cy/moa/wdd/Wdd.nsf/page18_en/page18_en?opendocument"
)
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
NEWS_HISTORY_FILE = "news_history.json"
NEWS_HISTORY_HOURS = 72
BOT_STATE_FILE = "bot_state.json"
BOT_VERSION = "v2.2.0"
REQUEST_HEADERS_CNN = {
    # CNN often blocks non-browser default clients (python-requests).
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}
REQUEST_HEADERS_GENERIC = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
LAST_BTC_PRICE: float | None = None
LAST_SPX_PRICE: float | None = None
ALLOWED_USER_ID: int | None = None
CYPRUS_TZ = ZoneInfo("Europe/Nicosia")
SCHEDULER_STATUS = "not-initialized"
NEWS_CACHE: dict[str, object] = {}
NEWS_TARGETS: list[tuple[str, int]] = [("politics", 5), ("technology", 10), ("markets", 10)]
MAX_TELEGRAM_MESSAGE_LEN = 3900
NEWS_RSS_FEEDS: dict[str, list[str]] = {
    "politics": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.cnn.com/rss/edition_world.rss",
        "https://www.theguardian.com/world/rss",
    ],
    "technology": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
    ],
    "markets": [
        "https://www.marketwatch.com/rss/topstories",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    ],
}

# ── Digest V2 ────────────────────────────────────────────────────────────────
DIGEST_SOURCE_RATINGS_FILE = "source_ratings.json"
DIGEST_SENT_FILE = "sent_digests.json"
DIGEST_SENT_HOURS = 72
DIGEST_TARGET_COUNT = 12
DIGEST_NEW_SOURCE_RATIO = 0.33
DIGEST_MAX_AGE_HOURS = 48
DIGEST_REACTION_MAP: dict[str, str] = {}  # hash → source name (in-memory)
DIGEST_REACTION_DELTAS = {"fire": 10, "like": 5, "dislike": -3, "poop": -5}
DIGEST_CATEGORY_ICONS = {
    "ai": "🤖", "investments": "💰", "robotics": "🦾", "vibecoding": "🛠️",
    "tech": "⚡", "business": "💼", "other": "📰",
}
DIGEST_RSS_FEEDS: dict[str, list[str]] = {
    "ai": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://venturebeat.com/category/ai/feed/",
        "https://news.mit.edu/rss/topic/artificial-intelligence2",
    ],
    "robotics": [
        "https://spectrum.ieee.org/feeds/topic/robotics.rss",
        "https://techcrunch.com/category/robotics/feed/",
        "https://www.therobotreport.com/feed/",
    ],
    "vibecoding": [
        "https://hnrss.org/newest?q=AI+coding+OR+copilot+OR+cursor+OR+claude",
        "https://dev.to/feed/tag/ai",
        "https://simonwillison.net/atom/everything/",
    ],
    "tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
    ],
    "business": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    ],
    "investments": [
        "https://www.marketwatch.com/rss/topstories",
    ],
}


class NewsFetchError(Exception):
    def __init__(self, message: str, debug_logs: list[str] | None = None):
        super().__init__(message)
        self.debug_logs = debug_logs or []


def with_version(text: str) -> str:
    return f"[{BOT_VERSION}]\n{text}"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def split_telegram_text(text: str, max_len: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= max_len:
            current = block
            continue
        # Hard-split very long blocks.
        start = 0
        while start < len(block):
            end = start + max_len
            chunks.append(block[start:end])
            start = end
    if current:
        chunks.append(current)
    return chunks


async def reply_long_text(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    for chunk in split_telegram_text(text):
        await message.reply_text(chunk)


async def send_long_text_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for chunk in split_telegram_text(text):
        await context.bot.send_message(chat_id=chat_id, text=chunk)


async def send_long_text_to_chat_html(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
) -> None:
    for chunk in split_telegram_text(text):
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def reply_long_text_html(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    for chunk in split_telegram_text(text):
        await message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)


def render_block(
    block_name: str,
    *,
    include_version: bool = False,
    force_refresh: bool = False,
    debug_mode: bool = False,
    news_spoilers: bool = True,
) -> tuple[str, bool]:
    html_mode = False
    if block_name == "fg":
        text = build_fear_greed_block()
    elif block_name == "st":
        text = build_st_block()
    elif block_name == "fx":
        text = build_fx_block()
    elif block_name == "dam":
        text = build_dam_block()
    elif block_name == "news":
        html_mode = news_spoilers and not debug_mode
        text = build_news_block(
            force_refresh=force_refresh,
            debug_mode=debug_mode,
            use_spoilers=html_mode,
        )
    else:
        raise ValueError(f"Unknown block: {block_name}")

    if include_version:
        text = with_version(text)
    return text, html_mode


async def render_block_async(**kwargs) -> tuple[str, bool]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(render_block, **kwargs))


async def send_rendered_update(update: Update, text: str, html_mode: bool) -> None:
    if html_mode:
        await reply_long_text_html(update, text)
    else:
        await reply_long_text(update, text)


async def send_rendered_chat(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, html_mode: bool
) -> None:
    if html_mode:
        await send_long_text_to_chat_html(context, chat_id, text)
    else:
        await send_long_text_to_chat(context, chat_id, text)


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


def parse_stooq_csv_line(csv_text: str) -> tuple[float, str]:
    """
    Parse Stooq CSV text and return (close_price, source_label).
    Supports payloads with or without a header row.
    """
    lines = [line.strip() for line in csv_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("stooq empty response")

    data_line = None
    for line in lines:
        if line.lower().startswith("symbol,"):
            continue
        data_line = line
        break
    if data_line is None:
        raise ValueError("stooq rows missing")

    parts = [p.strip() for p in data_line.split(",")]
    if len(parts) < 7:
        raise ValueError("stooq row malformed")
    close_raw = parts[6]
    if close_raw in {"", "N/D"}:
        raise ValueError("stooq close missing")

    price = float(close_raw)
    date_raw = parts[1] if len(parts) > 1 else ""
    time_raw = parts[2] if len(parts) > 2 else ""
    source = "Stooq"
    if date_raw and time_raw and len(date_raw) == 8 and len(time_raw) >= 4:
        source = (
            f"{source}, {int(date_raw[6:8])} "
            f"{datetime.strptime(date_raw[4:6], '%m').strftime('%b')} "
            f"{time_raw[:2]}:{time_raw[2:4]}"
        )
    return price, source


def get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment or .env file.")


def get_url_text(url: str, headers: dict[str, str] | None = None) -> str:
    req_headers = headers or REQUEST_HEADERS_GENERIC
    response = requests.get(url, headers=req_headers, timeout=30)
    response.raise_for_status()
    return response.text


def get_url_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    req_headers = headers or REQUEST_HEADERS_GENERIC
    response = requests.get(url, headers=req_headers, timeout=30)
    response.raise_for_status()
    return response.content


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", strip_html(text)).strip()


def col_from_ref(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def parse_wdd_report_date(text: str) -> str:
    # Usually in URL/file name: 18-FEB-2026 (or with spaces/underscores)
    match = re.search(r"(\d{1,2})[-_ ]([A-Z]{3})[-_ ](\d{4})", text, flags=re.I)
    if not match:
        return "n/a"
    day, mon, year = match.groups()
    dt = datetime.strptime(f"{day} {mon.upper()} {year}", "%d %b %Y").replace(
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
    response = requests.get(CNN_API_URL, headers=REQUEST_HEADERS_CNN, timeout=15)
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


def fetch_market_prices() -> tuple[float, float, float | None, str]:
    """Return (btc_price, spx_price, spx_premarket_price_or_None, debug)."""
    global LAST_BTC_PRICE, LAST_SPX_PRICE

    btc_price: float | None = None
    spx_price: float | None = None
    spx_premarket: float | None = None

    # Primary source: Yahoo Finance for BTC and S&P.
    _spy_debug = ""
    try:
        params = {"symbols": "BTC-USD,^GSPC"}
        response = requests.get(
            YAHOO_QUOTE_URL, params=params, headers=REQUEST_HEADERS_GENERIC, timeout=15
        )
        response.raise_for_status()
        data = response.json()

        results = data["quoteResponse"]["result"]
        for item in results:
            symbol = item.get("symbol")
            price = item.get("regularMarketPrice")
            if symbol == "BTC-USD" and price is not None:
                btc_price = float(price)
            elif symbol == "^GSPC" and price is not None:
                spx_price = float(price)
    except Exception:
        pass

    # SPY extended-hours via Yahoo v8 chart API (separate rate limits from v7).
    try:
        spy_resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            params={"range": "1d", "interval": "1m", "includePrePost": "true"},
            headers=REQUEST_HEADERS_GENERIC, timeout=15
        )
        spy_resp.raise_for_status()
        spy_meta = spy_resp.json()["chart"]["result"][0]["meta"]
        market_state = spy_meta.get("currentTradingPeriod", {})
        reg_price = spy_meta.get("regularMarketPrice")
        chart_price = spy_meta.get("chartPreviousClose")
        # currentPrice reflects the latest price including extended hours.
        current_price = spy_meta.get("regularMarketPrice")
        pre_p = spy_meta.get("preMarketPrice")
        post_p = spy_meta.get("postMarketPrice")
        mkt_state = spy_meta.get("marketState", "")
        _spy_debug = (f"v8 state={mkt_state} pre={pre_p} post={post_p} "
                      f"reg={reg_price} prevClose={chart_price}")
        extended_price = None
        if mkt_state in ("PRE", "PREPRE"):
            extended_price = pre_p
        elif mkt_state in ("POST", "POSTPOST"):
            extended_price = post_p
        if extended_price is not None:
            spx_premarket = float(extended_price) * 10
    except Exception as exc:
        _spy_debug = f"v8 ERR: {exc}"

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

    # S&P fallback 1: Stooq (^SPX intraday price from CSV).
    if spx_price is None:
        try:
            spx_response = requests.get(STOOQ_SPX_CSV_URL, timeout=15)
            spx_response.raise_for_status()
            spx_price, _ = parse_stooq_csv_line(spx_response.text)
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

    return btc_price, spx_price, spx_premarket, _spy_debug


def fetch_fx_yahoo() -> tuple[float, float, str]:
    response = requests.get(
        YAHOO_QUOTE_URL,
        params={"symbols": "EURUSD=X,EURRUB=X,RUB=X,USDRUB=X"},
        headers=REQUEST_HEADERS_GENERIC,
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
        if symbol == "RUB=X" and price is not None:
            usd_rub = float(price)
            market_time = item.get("regularMarketTime", market_time)
        if symbol == "USDRUB=X" and price is not None:
            usd_rub = float(price)
            market_time = item.get("regularMarketTime", market_time)

    if not eur_usd or eur_usd <= 0:
        raise ValueError("EURUSD not available")
    if eur_rub_direct and eur_rub_direct > 0:
        eur_rub = eur_rub_direct
        source = "Yahoo FX (direct EURRUB)"
    elif usd_rub and usd_rub > 0:
        eur_rub = eur_usd * usd_rub
        source = "Yahoo FX (EURUSD x USDRUB)"
    else:
        raise ValueError("EURRUB and USDRUB not available")

    if market_time:
        dt_utc = datetime.fromtimestamp(float(market_time), tz=timezone.utc)
        source = f"{source}, {format_cyprus_time(dt_utc)}"
    return eur_usd, eur_rub, source


def fetch_fx_stooq() -> tuple[float, float, str]:
    eur_usd, source_usd = parse_stooq_csv_line(get_url_text(STOOQ_EURUSD_CSV_URL))
    eur_rub, source_rub = parse_stooq_csv_line(get_url_text(STOOQ_EURRUB_CSV_URL))
    if eur_usd <= 0 or eur_rub <= 0:
        raise ValueError("invalid stooq values")
    # Prefer RUB timestamp for source label, fallback to EURUSD.
    source = source_rub if source_rub.startswith("Stooq,") else source_usd
    return eur_usd, eur_rub, source


def fetch_fx_frankfurter() -> tuple[float, float, str]:
    response = requests.get(
        FRANKFURTER_LATEST_URL,
        params={"from": "EUR", "to": "USD,RUB"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    rates = data["rates"]
    eur_usd = float(rates["USD"])
    if "RUB" not in rates:
        raise ValueError("RUB not provided by Frankfurter")
    eur_rub = float(rates["RUB"])
    if eur_usd <= 0 or eur_rub <= 0:
        raise ValueError("invalid frankfurter values")
    date_raw = str(data.get("date", "")).strip()
    source = "Frankfurter/ECB"
    if date_raw:
        try:
            dt_utc = datetime.strptime(date_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            source = f"{source}, {format_cyprus_date(dt_utc)}"
        except ValueError:
            source = f"{source}, {date_raw}"
    return eur_usd, eur_rub, source


def fetch_fx_open_er() -> tuple[float, float, str]:
    response = requests.get(OPEN_ER_API_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    rates = data["rates"]
    eur_usd = float(rates["USD"])
    eur_rub = float(rates["RUB"])
    if eur_usd <= 0 or eur_rub <= 0:
        raise ValueError("invalid open.er-api values")
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


def fetch_fx_rates() -> tuple[float, float, str]:
    """
    Compact production mode for /fg:
    Stooq first (best current reliability), then fallbacks.
    """
    try:
        return fetch_fx_stooq()
    except Exception:
        pass
    try:
        return fetch_fx_yahoo()
    except Exception:
        pass
    try:
        return fetch_fx_frankfurter()
    except Exception:
        pass
    return fetch_fx_open_er()


def load_news_history() -> dict[str, float]:
    try:
        with open(NEWS_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: dict[str, float] = {}
            for key, value in data.items():
                try:
                    out[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            return out
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def save_news_history(history: dict[str, float]) -> None:
    with open(NEWS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)


def load_bot_state() -> dict[str, object]:
    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def save_bot_state(state: dict[str, object]) -> None:
    with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def format_state_saved_time(saved_at: object) -> str:
    raw = normalize_text(str(saved_at))
    if not raw:
        return "n/a"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_cyprus_time(dt.astimezone(timezone.utc))
    except Exception:
        return raw


def trend_icon(current_value: float, previous_value: float) -> str:
    if current_value > previous_value:
        return "🟢"
    if current_value < previous_value:
        return "🔴"
    return "⚫"


def prune_news_history(history: dict[str, float], now_ts: float) -> dict[str, float]:
    cutoff = now_ts - (NEWS_HISTORY_HOURS * 3600)
    return {k: ts for k, ts in history.items() if ts >= cutoff}


def news_item_fingerprint(item: dict[str, str]) -> str:
    url = normalize_text(item.get("url", ""))
    if url:
        return url.lower()
    source = normalize_text(item.get("source", "")).lower()
    title = normalize_text(item.get("headline_en", "")).lower()
    return f"{source}|{title}"


def normalize_category(value: str) -> str:
    raw = normalize_text(value).lower()
    if raw in {"politics", "political", "geopolitics", "world", "government"}:
        return "politics"
    if raw in {"technology", "tech", "ai", "science", "startup", "startups"}:
        return "technology"
    if raw in {"markets", "market", "finance", "business", "economy", "economics"}:
        return "markets"
    return raw or "news"


def parse_ai_news_items(text: str) -> list[dict[str, str]]:
    payload = text.strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```[a-zA-Z0-9]*\n?", "", payload)
        payload = re.sub(r"\n?```$", "", payload).strip()
    parsed = json.loads(payload)
    items = parsed.get("items", [])
    if not isinstance(items, list):
        raise ValueError("AI JSON format invalid")

    out: list[dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = {
            "category": normalize_category(str(raw.get("category", ""))),
            "headline_en": normalize_text(str(raw.get("headline_en", ""))),
            "headline_ru": normalize_text(str(raw.get("headline_ru", ""))),
            "source": normalize_text(str(raw.get("source", ""))),
            "published_at": normalize_text(str(raw.get("published_at", ""))),
            "details_en": normalize_text(str(raw.get("details_en", ""))),
            "url": normalize_text(str(raw.get("url", ""))),
        }
        if item["headline_en"] and item["details_en"] and item["url"]:
            out.append(item)
    return out


def parse_feed_datetime(raw: str) -> datetime | None:
    text = normalize_text(raw)
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    candidates = [
        text,
        text.replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_news_feed_items(xml_text: str, category: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    out: list[dict[str, str]] = []

    # RSS format: channel/item
    rss_items = root.findall(".//channel/item")
    for node in rss_items:
        title = normalize_text(unescape(node.findtext("title", default="")))
        link = normalize_text(node.findtext("link", default=""))
        source = normalize_text(node.findtext("source", default="")) or "RSS"
        details = normalize_text(unescape(node.findtext("description", default="")))
        published_raw = (
            node.findtext("pubDate", default="")
            or node.findtext("date", default="")
            or node.findtext("{http://purl.org/dc/elements/1.1/}date", default="")
        )
        published_dt = parse_feed_datetime(published_raw)
        if not title or not link or not published_dt:
            continue
        out.append(
            {
                "category": category,
                "headline_en": title,
                "headline_ru": title,
                "source": source,
                "published_at": format_cyprus_time(published_dt),
                "details_en": details or "Details are not available in feed.",
                "details_ru": "",
                "url": link,
                "_published_dt": published_dt.isoformat(),
            }
        )

    # Atom format: entry with XML namespace.
    atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for node in atom_entries:
        title = normalize_text(
            unescape(node.findtext("{http://www.w3.org/2005/Atom}title", default=""))
        )
        source = "RSS"
        details = normalize_text(
            unescape(node.findtext("{http://www.w3.org/2005/Atom}summary", default=""))
        )
        if not details:
            details = normalize_text(
                unescape(node.findtext("{http://www.w3.org/2005/Atom}content", default=""))
            )
        link = ""
        for link_node in node.findall("{http://www.w3.org/2005/Atom}link"):
            href = normalize_text(link_node.attrib.get("href", ""))
            rel = normalize_text(link_node.attrib.get("rel", "alternate")).lower()
            if href and rel in {"", "alternate"}:
                link = href
                break
            if href and not link:
                link = href
        published_raw = (
            node.findtext("{http://www.w3.org/2005/Atom}updated", default="")
            or node.findtext("{http://www.w3.org/2005/Atom}published", default="")
        )
        published_dt = parse_feed_datetime(published_raw)
        if not title or not link or not published_dt:
            continue
        out.append(
            {
                "category": category,
                "headline_en": title,
                "headline_ru": title,
                "source": source,
                "published_at": format_cyprus_time(published_dt),
                "details_en": details or "Details are not available in feed.",
                "details_ru": "",
                "url": link,
                "_published_dt": published_dt.isoformat(),
            }
        )
    return out


def parse_json_payload(text: str) -> object:
    payload = text.strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```[a-zA-Z0-9]*\n?", "", payload)
        payload = re.sub(r"\n?```$", "", payload).strip()
    return json.loads(payload)


def translate_news_best_effort(
    items: list[dict[str, str]], debug_logs: list[str] | None = None
) -> None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not items:
        if debug_logs is not None:
            debug_logs.append("Translation: skipped (no OPENAI_API_KEY or empty list)")
        return
    batch_size = int(os.getenv("NEWS_TRANSLATE_BATCH_SIZE", "5"))
    batch_size = max(1, min(batch_size, 10))
    translated_any = False
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        payload_items = [
            {
                "id": idx + start,
                "headline_en": item["headline_en"],
                "details_en": item["details_en"],
            }
            for idx, item in enumerate(batch)
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "Translate news fields from English to Russian. "
                    "Return strict JSON only with schema: "
                    '{"items":[{"id":0,"headline_ru":"","details_ru":""}]}. '
                    "headline_ru should be short and natural. "
                    "details_ru should be informative and concise, formatted as 8-10 short lines "
                    "separated by newline characters. Do not add markdown."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"items": payload_items}, ensure_ascii=False),
            },
        ]
        try:
            text = call_openai_chat(
                messages,
                temperature=0.0,
                stage_label=f"news translation batch {start // batch_size + 1}",
            )
            raw = parse_json_payload(text)
            if not isinstance(raw, dict):
                continue
            rows = raw.get("items", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    idx = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                if idx < 0 or idx >= len(items):
                    continue
                headline_ru = normalize_text(str(row.get("headline_ru", "")))
                details_ru_raw = str(row.get("details_ru", ""))
                details_ru_lines = [normalize_text(x) for x in details_ru_raw.splitlines()]
                details_ru = "\n".join([line for line in details_ru_lines if line]).strip()
                if headline_ru:
                    items[idx]["headline_ru"] = headline_ru
                if details_ru:
                    items[idx]["details_ru"] = details_ru
                translated_any = True
        except Exception as exc:
            if debug_logs is not None:
                debug_logs.append(f"Translation batch failed ({exc})")
            continue
    if debug_logs is not None:
        debug_logs.append("Translation: done" if translated_any else "Translation: skipped")


def format_expanded_details(item: dict[str, str]) -> str:
    raw = item.get("details_ru") or item.get("details_en") or ""
    text = raw.strip()
    if not text:
        return "Подробности недоступны."

    if "\n" in text:
        lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    else:
        lines: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", normalize_text(text)):
            if not sentence:
                continue
            lines.extend(
                textwrap.wrap(
                    sentence,
                    width=72,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
            if len(lines) >= 10:
                break
        if len(lines) < 8:
            lines = textwrap.wrap(
                normalize_text(text),
                width=56,
                break_long_words=False,
                break_on_hyphens=False,
            )
    return "\n".join(lines[:10]) if lines else "Подробности недоступны."


def call_openai_chat(
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    stage_label: str = "request",
) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set")
    timeout_seconds = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "40"))
    max_attempts = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
    max_attempts = max(1, min(max_attempts, 5))
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": temperature,
        "messages": messages,
        "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "2200")),
    }

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                OPENAI_CHAT_COMPLETIONS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
        except requests.exceptions.Timeout as exc:
            if attempt < (max_attempts - 1):
                time_module.sleep(1.5 * (attempt + 1))
                continue
            raise ValueError(
                f"OpenAI timeout at {stage_label} after {max_attempts} attempts "
                f"({timeout_seconds}s each): {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            if attempt < (max_attempts - 1):
                time_module.sleep(1.5 * (attempt + 1))
                continue
            raise ValueError(f"OpenAI request error at {stage_label}: {exc}") from exc

        if response.status_code == 429:
            retry_after_raw = response.headers.get("Retry-After", "").strip()
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else 0.0
            except ValueError:
                retry_after = 0.0
            if attempt < (max_attempts - 1):
                wait_seconds = max(retry_after, 2.0 * (attempt + 1))
                time_module.sleep(wait_seconds)
                continue
            raise ValueError(f"OpenAI 429 at {stage_label}: {response.text[:220]}")

        if not response.ok:
            raise ValueError(
                f"OpenAI {response.status_code} at {stage_label}: {response.text[:220]}"
            )

        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    raise ValueError("OpenAI request failed after retries")


def fetch_news_items_via_ai(debug_mode: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    now_utc_dt = datetime.now(timezone.utc)
    now_utc = now_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = now_utc_dt.timestamp() - 24 * 3600
    by_category: dict[str, list[dict[str, str]]] = {k: [] for k, _ in NEWS_TARGETS}
    seen: set[str] = set()
    debug_logs: list[str] = []
    if debug_mode:
        debug_logs.append(f"UTC now: {now_utc}")
        debug_logs.append("Cache: miss (forced live fetch)")
    for category, count in NEWS_TARGETS:
        if debug_mode:
            debug_logs.append(f"RSS: category {category} target {count}")
        feeds = NEWS_RSS_FEEDS.get(category, [])
        collected: list[dict[str, str]] = []
        for feed_url in feeds:
            if len(collected) >= count:
                break
            try:
                xml_text = get_url_text(feed_url)
                feed_items = parse_news_feed_items(xml_text, category)
                fresh_items: list[dict[str, str]] = []
                for item in feed_items:
                    published_raw = item.pop("_published_dt", "")
                    published_dt = parse_feed_datetime(published_raw)
                    if not published_dt:
                        continue
                    if published_dt.timestamp() < cutoff:
                        continue
                    fp = news_item_fingerprint(item)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    item["_ts"] = str(published_dt.timestamp())
                    fresh_items.append(item)
                fresh_items.sort(key=lambda x: float(x.get("_ts", "0")), reverse=True)
                for item in fresh_items:
                    if len(collected) >= count:
                        break
                    item.pop("_ts", None)
                    collected.append(item)
                if debug_mode:
                    debug_logs.append(
                        f"RSS: {category} from {feed_url} +{len(fresh_items)} "
                        f"(have={len(collected)}/{count})"
                    )
            except Exception as exc:
                if debug_mode:
                    debug_logs.append(f"RSS: {category} source failed {feed_url} ({exc})")
                continue
        by_category[category] = collected[:count]

    final_items: list[dict[str, str]] = []
    for category, count in NEWS_TARGETS:
        final_items.extend(by_category[category][:count])

    translate_news_best_effort(final_items, debug_logs if debug_mode else None)

    if debug_mode:
        debug_logs.append(
            "Final items: "
            f"p={len(by_category['politics'])}, "
            f"t={len(by_category['technology'])}, "
            f"m={len(by_category['markets'])}, "
            f"total={len(final_items)}"
        )

    if not final_items:
        raise NewsFetchError("No fresh RSS items from last 24h", debug_logs)
    return final_items, debug_logs


def build_news_block(
    force_refresh: bool = False, debug_mode: bool = False, use_spoilers: bool = False
) -> str:
    ttl = int(os.getenv("NEWS_CACHE_TTL_SECONDS", "300"))
    fallback_ttl = int(os.getenv("NEWS_FALLBACK_CACHE_TTL_SECONDS", "120"))
    now_ts = datetime.now(timezone.utc).timestamp()
    cache_key = "html" if (use_spoilers and not debug_mode) else "plain"
    cached_entry = NEWS_CACHE.get(cache_key)
    if (
        not force_refresh
        and isinstance(cached_entry, dict)
        and cached_entry.get("content")
        and float(cached_entry.get("expires_at", 0.0)) > now_ts
    ):
        cached = str(cached_entry["content"])
        if debug_mode:
            return f"News service (debug):\n- Cache: hit\n\n{cached}"
        return cached

    try:
        ai_items, debug_logs = fetch_news_items_via_ai(debug_mode=debug_mode)
        if debug_mode:
            lines = ["News service (debug):"]
            for log in debug_logs:
                lines.append(f"- {log}")
            lines.append("")
            lines.append("News digest:")
        else:
            lines = ["News digest:"]
        for item in ai_items[:25]:
            cat = item.get("category", "news").capitalize()
            expanded_details = format_expanded_details(item)
            if use_spoilers and not debug_mode:
                lines.append(
                    f"• <b>[{html_escape(cat)}] {html_escape(item['headline_ru'])}</b> "
                    f"({html_escape(item['source'])}, {html_escape(item['published_at'])})"
                )
                lines.append(
                    f"<blockquote expandable>{html_escape(expanded_details)}</blockquote>"
                )
                safe_url = html_escape(item["url"], quote=True)
                lines.append(f'<a href="{safe_url}">Source link</a>')
            else:
                lines.append(
                    f"- [{cat}] {item['headline_ru']} ({item['source']}, {item['published_at']})"
                )
                lines.append(expanded_details)
                lines.append(item["url"])
            lines.append("")

        lines.append("")
        lines.append(f"Updated: {format_cyprus_time(datetime.now(timezone.utc))}")
        content = "\n".join(lines)
        NEWS_CACHE[cache_key] = {
            "content": content,
            "expires_at": now_ts + max(ttl, 60),
            "updated_at": now_ts,
        }
        return content
    except Exception as exc:
        lines = [f"News AI: fallback ({exc})"]
        if isinstance(exc, NewsFetchError) and debug_mode and exc.debug_logs:
            lines.append("")
            lines.append("News service (debug):")
            for log in exc.debug_logs:
                lines.append(f"- {log}")
        lines.append("")
        lines.append(f"Updated: {format_cyprus_time(datetime.now(timezone.utc))}")
        content = "\n".join(lines)
        NEWS_CACHE[cache_key] = {
            "content": content,
            "expires_at": now_ts + max(fallback_ttl, 30),
            "updated_at": now_ts,
        }
        return content


# ── Digest V2: source ratings ────────────────────────────────────────────────


def load_source_ratings() -> dict[str, dict]:
    try:
        with open(DIGEST_SOURCE_RATINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_source_ratings(ratings: dict[str, dict]) -> None:
    with open(DIGEST_SOURCE_RATINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=2)


def get_source_rating_score(ratings: dict, source: str) -> float:
    entry = ratings.get(source.lower().strip(), {})
    return float(entry.get("score", 0))


def is_proven_source(ratings: dict, source: str) -> bool:
    entry = ratings.get(source.lower().strip(), {})
    return int(entry.get("count", 0)) >= 3


def update_source_rating(source: str, delta: float) -> None:
    ratings = load_source_ratings()
    key = source.lower().strip()
    entry = ratings.get(key, {"score": 0, "count": 0})
    entry["score"] = float(entry.get("score", 0)) + delta
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    ratings[key] = entry
    save_source_ratings(ratings)


def source_hash(source: str) -> str:
    return hashlib.md5(source.lower().strip().encode()).hexdigest()[:12]


DIGEST_BACKUP_CAPTION = "#digest_ratings_backup"


async def restore_ratings_from_telegram(bot) -> bool:
    """Download source_ratings.json from pinned message on startup."""
    chat_id = get_target_chat_id()
    if not chat_id:
        return False
    try:
        chat = await bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return False
        if not pinned.caption or DIGEST_BACKUP_CAPTION not in pinned.caption:
            return False
        tg_file = await bot.get_file(pinned.document.file_id)
        raw = await tg_file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            save_source_ratings(data)
            return True
    except Exception:
        pass
    return False


async def backup_ratings_to_telegram(bot) -> None:
    """Upload source_ratings.json as pinned document."""
    chat_id = get_target_chat_id()
    if not chat_id:
        return
    ratings = load_source_ratings()
    if not ratings:
        return
    try:
        content = json.dumps(ratings, ensure_ascii=False, indent=2).encode("utf-8")
        doc = io.BytesIO(content)
        doc.name = "source_ratings.json"
        msg = await bot.send_document(
            chat_id=chat_id,
            document=doc,
            caption=DIGEST_BACKUP_CAPTION,
        )
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception:
        pass


# ── Digest V2: sent history ─────────────────────────────────────────────────


def load_sent_digests() -> list[dict]:
    try:
        with open(DIGEST_SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_sent_digests(digests: list[dict]) -> None:
    with open(DIGEST_SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(digests, f, ensure_ascii=False)


def prune_sent_digests(digests: list[dict], now_ts: float) -> list[dict]:
    cutoff = now_ts - DIGEST_SENT_HOURS * 3600
    return [d for d in digests if float(d.get("sent_at", 0)) >= cutoff]


def is_already_sent(digests: list[dict], fingerprint: str) -> bool:
    return any(d.get("fingerprint") == fingerprint for d in digests)


# ── Digest V2: AI scoring ───────────────────────────────────────────────────


def score_digest_items_via_ai(items: list[dict[str, str]]) -> list[dict[str, str]]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        for item in items:
            item["ai_score"] = "0"
        return items

    payload = [
        {
            "id": i,
            "title": item.get("headline_en", "")[:120],
            "source": item.get("source", ""),
            "published_at": item.get("published_at", ""),
            "summary": item.get("details_en", "")[:200],
        }
        for i, item in enumerate(items)
    ]

    # Process in batches of 25 to stay within token limits.
    batch_size = 25
    for start in range(0, len(payload), batch_size):
        batch = payload[start : start + batch_size]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a tech news curator. Score each item 0-100.\n\n"
                    "Scoring:\n"
                    "- Event importance (0-40): 35-40 breakthroughs/major, 25-34 significant, "
                    "15-24 trends, 5-14 minor\n"
                    "- Topic priority (0-30): AI=30, Investments=27, Robotics=23, "
                    "Vibe coding tools=18, Tech=12, Business=6, Other=1\n"
                    "- Source quality (0-20): Top tier=20, Good=15, Average=10, Unknown=5\n"
                    "- Freshness (0-10): <6h=10, 6-24h=7, 24-48h=3\n\n"
                    "Return strict JSON only:\n"
                    '{"items":[{"id":0,"score":85,"category":"ai",'
                    '"headline_ru":"заголовок","details_ru":"описание 2-3 предложения"}]}\n\n'
                    "Categories: ai, investments, robotics, vibecoding, tech, business, other\n"
                    "headline_ru and details_ru must be in Russian."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"items": batch}, ensure_ascii=False),
            },
        ]
        try:
            text = call_openai_chat(
                messages, temperature=0.1, stage_label=f"digest scoring batch {start // batch_size + 1}"
            )
            result = parse_json_payload(text)
            if not isinstance(result, dict):
                continue
            scored = result.get("items", [])
            if not isinstance(scored, list):
                continue
            for s in scored:
                if not isinstance(s, dict):
                    continue
                try:
                    idx = int(s["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if idx < 0 or idx >= len(items):
                    continue
                items[idx]["ai_score"] = str(float(s.get("score", 0)))
                items[idx]["ai_category"] = str(s.get("category", items[idx].get("category", "other")))
                headline_ru = normalize_text(str(s.get("headline_ru", "")))
                details_ru = normalize_text(str(s.get("details_ru", "")))
                if headline_ru:
                    items[idx]["headline_ru"] = headline_ru
                if details_ru:
                    items[idx]["details_ru"] = details_ru
        except Exception:
            continue

    return items


# ── Digest V2: selection ─────────────────────────────────────────────────────


def select_digest_items(
    items: list[dict[str, str]], source_ratings: dict
) -> list[dict[str, str]]:
    target = DIGEST_TARGET_COUNT

    proven = [i for i in items if is_proven_source(source_ratings, i.get("source", ""))]
    new = [i for i in items if not is_proven_source(source_ratings, i.get("source", ""))]

    proven.sort(key=lambda x: float(x.get("ai_score", "0")), reverse=True)
    new.sort(key=lambda x: float(x.get("ai_score", "0")), reverse=True)

    new_count = max(1, int(target * DIGEST_NEW_SOURCE_RATIO))
    proven_count = target - new_count

    selected = proven[:proven_count] + new[:new_count]

    if len(selected) < target:
        remaining = proven[proven_count:] + new[new_count:]
        remaining.sort(key=lambda x: float(x.get("ai_score", "0")), reverse=True)
        selected.extend(remaining[: target - len(selected)])

    selected.sort(key=lambda x: float(x.get("ai_score", "0")), reverse=True)
    return selected[:target]


# ── Digest V2: build ─────────────────────────────────────────────────────────


def build_digest_v2() -> tuple[list[dict[str, str]], str]:
    now_utc = datetime.now(timezone.utc)
    cutoff_ts = now_utc.timestamp() - DIGEST_MAX_AGE_HOURS * 3600

    source_ratings = load_source_ratings()
    sent_digests = prune_sent_digests(load_sent_digests(), now_utc.timestamp())

    all_items: list[dict[str, str]] = []
    seen_fps: set[str] = set()

    for category, feeds in DIGEST_RSS_FEEDS.items():
        for feed_url in feeds:
            try:
                xml_text = get_url_text(feed_url)
                feed_items = parse_news_feed_items(xml_text, category)
                for item in feed_items:
                    pub_raw = item.pop("_published_dt", "")
                    pub_dt = parse_feed_datetime(pub_raw)
                    if not pub_dt or pub_dt.timestamp() < cutoff_ts:
                        continue
                    fp = news_item_fingerprint(item)
                    if fp in seen_fps or is_already_sent(sent_digests, fp):
                        continue
                    seen_fps.add(fp)
                    # Skip sources with very negative rating.
                    src = item.get("source", "")
                    if get_source_rating_score(source_ratings, src) < -5:
                        continue
                    all_items.append(item)
            except Exception:
                continue

    if not all_items:
        return [], "Нет свежих новостей за последние 48ч."

    all_items = score_digest_items_via_ai(all_items)

    # Add source rating bonus to AI score.
    for item in all_items:
        bonus = max(-5.0, min(get_source_rating_score(source_ratings, item.get("source", "")), 10.0))
        item["ai_score"] = str(float(item.get("ai_score", "0")) + bonus)

    selected = select_digest_items(all_items, source_ratings)

    # Record sent fingerprints.
    for item in selected:
        fp = news_item_fingerprint(item)
        sent_digests.append({"fingerprint": fp, "sent_at": now_utc.timestamp()})
    save_sent_digests(sent_digests)

    # Populate reaction map (hash → source) for callback handler.
    for item in selected:
        src = item.get("source", "")
        DIGEST_REACTION_MAP[source_hash(src)] = src

    return selected, ""


def build_fear_greed_block() -> str:
    state = load_bot_state()
    prev_fg = state.get("fg")
    prev_fg_dict = prev_fg if isinstance(prev_fg, dict) else {}
    next_fg: dict[str, object] = dict(prev_fg_dict)
    state_dirty = False

    score: float | None = None
    c_score: int | None = None

    try:
        score, rating, updated_at = fetch_fear_and_greed()
        stock_block = f"Stock Fear & Greed (CNN): {score:.2f} {rating} {updated_at}"
        next_fg["stock_score"] = score
        next_fg["stock_rating"] = rating
        next_fg["stock_updated_at"] = updated_at
        state_dirty = True
    except Exception as exc:
        stock_block = f"Stock Fear & Greed (CNN): ошибка ({exc})"

    try:
        c_score, c_rating, c_updated_at = fetch_crypto_fear_and_greed()
        crypto_block = f"Crypto Fear & Greed: {c_score} {c_rating} {c_updated_at}"
        next_fg["crypto_score"] = c_score
        next_fg["crypto_rating"] = c_rating
        next_fg["crypto_updated_at"] = c_updated_at
        state_dirty = True
    except Exception as exc:
        crypto_block = f"Crypto Fear & Greed: ошибка ({exc})"

    try:
        if "stock_score" in prev_fg_dict:
            prev_stock_score = float(prev_fg_dict["stock_score"])
            icon = trend_icon(score, prev_stock_score) if score is not None else "⚫"
            stock_prev = (
                f"[{prev_stock_score:.2f} "
                f"{str(prev_fg_dict.get('stock_rating', 'n/a'))} "
                f"{str(prev_fg_dict.get('stock_updated_at', 'n/a'))}]"
            )
            if "ошибка" not in stock_block:
                stock_block = f"{stock_block} {icon} {stock_prev}"
    except Exception:
        pass

    try:
        if "crypto_score" in prev_fg_dict:
            prev_crypto_score = int(float(prev_fg_dict["crypto_score"]))
            icon = trend_icon(float(c_score), float(prev_crypto_score)) if c_score is not None else "⚫"
            crypto_prev = (
                "["
                f"{prev_crypto_score} "
                f"{str(prev_fg_dict.get('crypto_rating', 'n/a'))} "
                f"{str(prev_fg_dict.get('crypto_updated_at', 'n/a'))}]"
            )
            if "ошибка" not in crypto_block:
                crypto_block = f"{crypto_block} {icon} {crypto_prev}"
    except Exception:
        pass

    if state_dirty:
        next_fg["saved_at"] = datetime.now(timezone.utc).isoformat()
        state["fg"] = next_fg
        save_bot_state(state)

    return f"{stock_block}\n{crypto_block}"


def build_st_block() -> str:
    state = load_bot_state()
    prev_st = state.get("st")
    prev_st_dict = prev_st if isinstance(prev_st, dict) else {}
    try:
        btc_price, spx_price, spx_premarket, spy_debug = fetch_market_prices()
        btc_line = f"Bitcoin (BTC-USD): ${btc_price:,.2f}"
        spx_line = f"S&P 500 (^GSPC): {spx_price:,.2f}"
        if spx_premarket is not None:
            pct = (spx_premarket - spx_price) / spx_price * 100
            sign = "+" if pct >= 0 else ""
            spx_line += f"\nS&P 500 Pre-Market (SPY×10): {spx_premarket:,.2f} ({sign}{pct:.2f}%)"
        spx_line += f"\n[debug] {spy_debug}"
        next_st: dict[str, object] = dict(prev_st_dict)
        next_st["btc_price"] = btc_price
        next_st["spx_price"] = spx_price
        next_st["saved_at"] = datetime.now(timezone.utc).isoformat()
        state["st"] = next_st
        save_bot_state(state)

        try:
            prev_time = format_state_saved_time(prev_st_dict.get("saved_at", ""))
            if "btc_price" in prev_st_dict:
                prev_btc = float(prev_st_dict["btc_price"])
                icon = trend_icon(btc_price, prev_btc)
                btc_line = (
                    f"{btc_line} "
                    f"{icon} [${prev_btc:,.2f} {prev_time}]"
                )
            if "spx_price" in prev_st_dict:
                prev_spx = float(prev_st_dict["spx_price"])
                icon = trend_icon(spx_price, prev_spx)
                spx_line = (
                    f"{spx_line} "
                    f"{icon} [{prev_spx:,.2f} {prev_time}]"
                )
        except Exception:
            pass
        return f"{btc_line}\n{spx_line}"
    except Exception as exc:
        return f"Рыночные цены: временно недоступны ({exc})"


def build_fx_block() -> str:
    try:
        eur_usd, eur_rub, fx_source = fetch_fx_rates()
        return (
            f"EUR/USD: {eur_usd:.5f}\n"
            f"EUR/RUB: {eur_rub:.5f}\n"
            f"FX source: {fx_source}"
        )
    except Exception as exc:
        return f"FX курсы: временно недоступны ({exc})"


def build_dam_block() -> str:
    try:
        return fetch_cyprus_reservoirs_summary()
    except Exception as exc:
        return f"Cyprus reservoirs: временно недоступно ({exc})"


def get_target_chat_id() -> int | None:
    raw = os.getenv("TELEGRAM_TARGET_CHAT_ID", "").strip()
    if not raw:
        return None
    return int(raw)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    await update.message.reply_text(
        with_version(
            "Привет! Я показываю Fear & Greed Index.\n"
            "Команды:\n"
            "/fg - Fear & Greed блок\n"
            "/st - Bitcoin и S&P\n"
            "/fx - валюты\n"
            "/dam - Cyprus reservoirs\n"
            "/news - новостной дайджест (v1)\n"
            "/digest - tech-дайджест с ИИ-скорингом\n"
            "/all - все блоки\n\n"
            "Авто-отправка в 08:00 и 20:00 (Кипр) отправляет /all, если в Render задана "
            "переменная TELEGRAM_TARGET_CHAT_ID."
        )
    )


async def fg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    text, html_mode = await render_block_async(block_name="fg", include_version=True)
    await send_rendered_update(update, text, html_mode)


async def fx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    text, html_mode = await render_block_async(block_name="fx", include_version=True)
    await send_rendered_update(update, text, html_mode)


async def st(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    text, html_mode = await render_block_async(block_name="st", include_version=True)
    await send_rendered_update(update, text, html_mode)


async def dam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    text, html_mode = await render_block_async(block_name="dam", include_version=True)
    await send_rendered_update(update, text, html_mode)


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    force_refresh = False
    debug_mode = False
    if context.args:
        first = context.args[0].strip().lower()
        force_refresh = first in {"refresh", "r", "now", "new"}
        debug_mode = first in {"debug", "dbg", "trace"}
    if debug_mode:
        force_refresh = True
    text, html_mode = await render_block_async(
        block_name="news",
        include_version=True,
        force_refresh=force_refresh,
        debug_mode=debug_mode,
        news_spoilers=True,
    )
    await send_rendered_update(update, text, html_mode)


async def all_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    block_order = ["fg", "st", "fx", "dam", "news"]
    for idx, block_name in enumerate(block_order):
        text, html_mode = await render_block_async(
            block_name=block_name,
            include_version=(idx == 0),
            news_spoilers=True,
        )
        await send_rendered_update(update, text, html_mode)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    target_chat_id = os.getenv("TELEGRAM_TARGET_CHAT_ID", "(not set)")
    openai_key_set = "yes" if os.getenv("OPENAI_API_KEY", "").strip() else "no"
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    deploy_notify = "yes" if env_flag("SEND_DEPLOY_NOTIFICATION", True) else "no"
    has_job_queue = "yes" if context.application.job_queue is not None else "no"
    jobs = context.application.job_queue.jobs() if context.application.job_queue else []
    job_names = ", ".join(job.name for job in jobs) if jobs else "(none)"
    await update.effective_message.reply_text(
        with_version(
            "Статус бота:\n"
            f"- Scheduler: {SCHEDULER_STATUS}\n"
            f"- job_queue available: {has_job_queue}\n"
            f"- TELEGRAM_TARGET_CHAT_ID: {target_chat_id}\n"
            f"- OPENAI_API_KEY set: {openai_key_set}\n"
            f"- OPENAI_MODEL: {openai_model}\n"
            f"- SEND_DEPLOY_NOTIFICATION: {deploy_notify}\n"
            f"- jobs: {job_names}"
        )
    )


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    block_order = ["fg", "st", "fx", "dam", "news"]
    for idx, block_name in enumerate(block_order):
        text, html_mode = await render_block_async(
            block_name=block_name,
            include_version=(idx == 0),
            news_spoilers=True,
        )
        await send_rendered_chat(context, chat_id, text, html_mode)


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    msg = await update.effective_message.reply_text("⏳ Собираю дайджест...")

    loop = asyncio.get_running_loop()
    selected, error = await loop.run_in_executor(None, build_digest_v2)

    if error:
        await msg.edit_text(with_version(error))
        return

    await msg.delete()

    for item in selected:
        cat = item.get("ai_category", item.get("category", "other"))
        icon = DIGEST_CATEGORY_ICONS.get(cat, "📰")
        headline = item.get("headline_ru", item.get("headline_en", ""))
        details = item.get("details_ru", item.get("details_en", ""))
        source = item.get("source", "")
        url = item.get("url", "")
        score_val = int(float(item.get("ai_score", "0")))

        text = f"{icon} <b>{html_escape(headline)}</b>\n"
        if details:
            text += f"<blockquote expandable>{html_escape(details)}</blockquote>\n"
        text += f"📊 {score_val} | {html_escape(source)}\n"
        if url:
            text += f'<a href="{html_escape(url, quote=True)}">Читать →</a>'

        src_h = source_hash(source)
        DIGEST_REACTION_MAP[src_h] = source
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔥", callback_data=f"dr:fire:{src_h}"),
                    InlineKeyboardButton("👍", callback_data=f"dr:like:{src_h}"),
                    InlineKeyboardButton("👎", callback_data=f"dr:dislike:{src_h}"),
                    InlineKeyboardButton("💩", callback_data=f"dr:poop:{src_h}"),
                ]
            ]
        )

        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    await update.effective_message.reply_text(
        with_version(
            f"Дайджест: {len(selected)} новостей | "
            f"{format_cyprus_time(datetime.now(timezone.utc))}"
        )
    )


async def digest_reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not _check_access(update):
        await query.answer("⛔")
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "dr":
        return

    reaction = parts[1]
    src_h = parts[2]
    delta = DIGEST_REACTION_DELTAS.get(reaction)
    if delta is None:
        await query.answer("?")
        return

    source_name = DIGEST_REACTION_MAP.get(src_h, src_h)
    update_source_rating(source_name, delta)

    icons = {"fire": "🔥", "like": "👍", "dislike": "👎", "poop": "💩"}
    await query.answer(f"{icons.get(reaction, '✓')} {source_name}")

    # Backup ratings to Telegram so they survive redeploys.
    await backup_ratings_to_telegram(context.bot)


async def on_startup(app) -> None:
    global SCHEDULER_STATUS
    # Restore source ratings from Telegram backup.
    restored = await restore_ratings_from_telegram(app.bot)
    if restored:
        ratings = load_source_ratings()
        count = len(ratings)
        print(f"[digest] Restored {count} source ratings from Telegram backup")
    await app.bot.set_my_commands(
        [
            BotCommand("start", "помощь"),
            BotCommand("fg", "Fear & Greed блок"),
            BotCommand("st", "Bitcoin и S&P"),
            BotCommand("fx", "валюты"),
            BotCommand("dam", "Cyprus reservoirs"),
            BotCommand("news", "новостной дайджест (v1)"),
            BotCommand("digest", "tech-дайджест с ИИ-скорингом"),
            BotCommand("all", "все блоки"),
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

    if env_flag("SEND_DEPLOY_NOTIFICATION", True):
        try:
            await app.bot.send_message(
                chat_id=target_chat_id,
                text=with_version(
                    "Deploy: bot restarted successfully.\n"
                    f"Time: {format_cyprus_time(datetime.now(timezone.utc))}\n"
                    f"Scheduler: {SCHEDULER_STATUS}"
                ),
            )
        except Exception:
            pass


def _check_access(update: Update) -> bool:
    if ALLOWED_USER_ID is None:
        return True
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


logger = logging.getLogger(__name__)


def main() -> None:
    global ALLOWED_USER_ID
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = get_token()
    raw_uid = os.getenv("ALLOWED_USER_ID", "").strip()
    if raw_uid:
        ALLOWED_USER_ID = int(raw_uid)

    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fg", fg))
    app.add_handler(CommandHandler("st", st))
    app.add_handler(CommandHandler("fx", fx))
    app.add_handler(CommandHandler("dam", dam))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CommandHandler("all", all_report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(digest_reaction_callback, pattern=r"^dr:"))

    app.run_polling()


if __name__ == "__main__":
    main()
