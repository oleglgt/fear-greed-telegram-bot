import asyncio
import logging
import os
import io
import json
import platform
import re
import socket
import textwrap
import threading
import time as time_module
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from html import escape as html_escape, unescape
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

CNN_API_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CRYPTO_API_URL = "https://api.alternative.me/fng/?limit=8"
YAHOO_QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINGECKO_BTC_URL = "https://api.coingecko.com/api/v3/simple/price"
COINBASE_BTC_URL = "https://api.coinbase.com/v2/prices/spot"
# Stooq /q/l/ answers 404 without the full field spec (f=...&e=csv).
STOOQ_SPX_CSV_URL = "https://stooq.com/q/l/?s=%5Espx&f=sd2t2ohlcv&h&e=csv"
STOOQ_ES_FUTURES_CSV_URL = "https://stooq.com/q/l/?s=es.f&f=sd2t2ohlcv&h&e=csv"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/"
FRED_SPX_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
FRANKFURTER_LATEST_URL = "https://api.frankfurter.app/latest"
OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/EUR"
STOOQ_EURUSD_CSV_URL = "https://stooq.com/q/l/?s=eurusd&f=sd2t2ohlcv&h&e=csv"
STOOQ_EURRUB_CSV_URL = "https://stooq.com/q/l/?s=eurrub&f=sd2t2ohlcv&h&e=csv"
WDD_RESERVOIRS_PAGE_URL = (
    "https://www.moa.gov.cy/moa/wdd/Wdd.nsf/page18_en/page18_en?opendocument"
)
# Fragmata aggregates the same WDD weekly reports and exposes them as JSON
# (open API, no auth; docs: github.com/vbougay/fragmata.info API.md). The old
# WDD Lotus Notes page stopped serving UK.xlsx after the gov.cy migration.
FRAGMATA_SUMMARY_URL = "https://fragmata.info/api/v1/summary/"
# Candidate sources for /dam debug probe: the old Lotus Notes WDD site is being
# phased out in favour of the new MOA portal and the Cyprus Dams Monitor, so
# probe all of them from production (which has open egress) to pick a new source.
DAM_DEBUG_PROBE_URLS: list[str] = [
    FRAGMATA_SUMMARY_URL,
    WDD_RESERVOIRS_PAGE_URL,
    "https://www.moi.gov.cy/moa/wdd/wdd.nsf/page18_en/page18_en?opendocument",
    "https://moa.gov.cy/sectors/water-resources/water-development-department/?lang=en",
    "https://www.gov.cy/moa-wdd/en/",
    "https://dams.wdd.moa.gov.cy/",
    "https://dams.wdd.moa.gov.cy/api/dams",
    "https://cyprus-water.appspot.com/api",
    "https://fragmata.info/",
]
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
NEWS_HISTORY_FILE = "news_history.json"
NEWS_HISTORY_HOURS = 72
BOT_STATE_FILE = "bot_state.json"
BOT_VERSION = "v4.4.0"
BOT_STARTED_AT = datetime.now(timezone.utc)

# Env markers the common hosting platforms inject; lets /status answer
# "where is this bot actually running?".
HOSTING_ENV_MARKERS = [
    ("RENDER_SERVICE_NAME", "Render"),
    ("RENDER", "Render"),
    ("RAILWAY_PROJECT_NAME", "Railway"),
    ("RAILWAY_ENVIRONMENT", "Railway"),
    ("DYNO", "Heroku"),
    ("FLY_APP_NAME", "Fly.io"),
    ("KOYEB_APP_NAME", "Koyeb"),
    ("PYTHONANYWHERE_DOMAIN", "PythonAnywhere"),
    ("WEBSITE_SITE_NAME", "Azure App Service"),
    ("K_SERVICE", "Google Cloud Run"),
    ("AWS_EXECUTION_ENV", "AWS"),
]


def detect_hosting() -> str:
    hits: dict[str, str] = {}
    for var, label in HOSTING_ENV_MARKERS:
        value = os.getenv(var, "").strip()
        if value and label not in hits:
            hits[label] = f"{label} ({var}={value})"
    if hits:
        return "; ".join(hits.values())
    return "no PaaS markers (plain VM/desktop?)"
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
NEWS_TARGETS: list[tuple[str, int]] = [
    ("ai", 10),
    ("agentpay", 5),
    ("finance", 8),
    ("robotics", 7),
]
NEWS_CATEGORY_LABELS = {
    "ai": "AI",
    "agentpay": "AI Payments",
    "finance": "Finance",
    "robotics": "Robotics",
}
# Feeds for agentpay are broad fintech/payments streams, so items must also
# match this topical filter to qualify as AI-agent-payments news.
AGENTPAY_KEYWORD_RE = re.compile(
    r"agentic\s+(commerce|payments?|checkout|shopping|transactions?|spending|banking)"
    r"|agent\s+payments?\s+protocol|\bAP2\b|\bx402\b|agentic\s+commerce\s+protocol|\bACP\b"
    r"|\b(ai|autonomous|shopping)\s+agents?\b.{0,60}\b(pay(s|ment|ments)?|purchas\w+|buy(s|ing)?|"
    r"checkout|commerce|transactions?|wallets?|stablecoins?|credit\s+cards?)\b"
    r"|\b(pay(ment|ments)?|commerce|checkout|wallets?|stablecoins?|banks?|banking|fintech)\b"
    r".{0,60}\b(ai|autonomous)\s+agents?\b",
    re.IGNORECASE,
)
AGENTPAY_WINDOW_HOURS = 48  # niche topic: a 24h window often yields too few items
MAX_TELEGRAM_MESSAGE_LEN = 3900
HTTP_TIMEOUT_SHORT = 15  # market/FX APIs
HTTP_TIMEOUT_LONG = 30  # RSS feeds, XLSX downloads
# Finance = markets/economy/company news; deliberately no MarketWatch-style
# personal-finance feeds (tax tips, retirement advice for US consumers).
NEWS_RSS_FEEDS: dict[str, list[str]] = {
    "ai": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://venturebeat.com/category/ai/feed/",
        "https://news.mit.edu/rss/topic/artificial-intelligence2",
    ],
    "finance": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.theguardian.com/uk/business/rss",
    ],
    "robotics": [
        "https://spectrum.ieee.org/feeds/topic/robotics.rss",
        "https://www.therobotreport.com/feed/",
        "https://techcrunch.com/tag/robotics/feed/",
        "https://news.mit.edu/rss/topic/robotics",
    ],
    "agentpay": [
        "https://hnrss.org/newest?q=%22agentic+commerce%22+OR+%22agentic+payments%22+OR+%22agent+payments%22+OR+x402+OR+AP2",
        "https://www.pymnts.com/feed/",
        "https://thefintechtimes.com/feed/",
        "https://techcrunch.com/category/fintech/feed/",
    ],
}

# ── /hot: most-discussed stories ─────────────────────────────────────────────
# "Discussed" = covered by many distinct outlets: similar headlines from
# different feeds are clustered, clusters ranked by distinct source count.
# Wider feed pool than /news so the coverage signal has something to measure.
HOT_RSS_FEEDS: dict[str, list[str]] = {
    "ai": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://venturebeat.com/category/ai/feed/",
        "https://news.mit.edu/rss/topic/artificial-intelligence2",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://www.wired.com/feed/tag/ai/latest/rss",
    ],
    "finance": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.theguardian.com/uk/business/rss",
        "https://fortune.com/feed/",
        "https://finance.yahoo.com/news/rssindex",
    ],
    "robotics": [
        "https://spectrum.ieee.org/feeds/topic/robotics.rss",
        "https://www.therobotreport.com/feed/",
        "https://techcrunch.com/tag/robotics/feed/",
        "https://news.mit.edu/rss/topic/robotics",
        "https://robohub.org/feed/",
    ],
}
HOT_WINDOW_HOURS = 24
HOT_TARGET_COUNT = 10

# ── /ideas: Smart Money (13F funds + analyst consensus) ─────────────────────
# Universe of candidates = fresh buys/adds from respected funds' 13F filings
# (SEC EDGAR, free); the live layer = analyst strong-buy consensus per ticker
# (Finnhub, free API key). CUSIP→ticker resolved via OpenFIGI (free, key optional).
IDEAS_UNIVERSE_FILE = "ideas_universe.json"
IDEAS_UNIVERSE_MAX_AGE_DAYS = 30
IDEAS_TARGET_COUNT = 10
IDEAS_MAX_ANALYST_LOOKUPS = 25  # Finnhub free tier is 60 req/min
IDEAS_MIN_COVERAGE = 5  # analysts covering; below this the rating is noise
IDEAS_CHANGE_PCT = 20.0  # shares +/-20% counts as increased/decreased
IDEAS_CACHE: dict[str, object] = {}
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/"
OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
FINNHUB_RECO_URL = "https://finnhub.io/api/v1/stock/recommendation"
# CIK -> fallback label; the display name is taken from EDGAR's own response.
IDEAS_FUNDS: dict[str, str] = {
    "0001067983": "Berkshire Hathaway",
    "0001336528": "Pershing Square",
    "0001040273": "Third Point",
    "0001656456": "Appaloosa",
    "0001061768": "Baupost Group",
    "0001649339": "Scion Asset Management",
}
HOT_CATEGORY_ICONS = {"ai": "🤖", "finance": "💹", "robotics": "🦾"}
HOT_TITLE_STOPWORDS = frozenset(
    "the and for with that this from are was were has have had been will would "
    "could should say says said after amid over under about into more than "
    "new live update updates report reports news its his her their our your "
    "who what when where why how not out off all can may might just also "
    "against between during before because while still being".split()
)

# ── Digest V2 ────────────────────────────────────────────────────────────────
DIGEST_SOURCE_RATINGS_FILE = "source_ratings.json"
DIGEST_SENT_FILE = "sent_digests.json"
DIGEST_SENT_HOURS = 72
DIGEST_TARGET_COUNT = 12
DIGEST_NEW_SOURCE_RATIO = 0.33
DIGEST_MAX_AGE_HOURS = 48
DIGEST_SCORE_MAX_ITEMS = 100  # cap AI scoring cost: only the freshest N go to OpenAI
DIGEST_REACTION_DELTAS = {"fire": 10, "like": 5, "dislike": -3, "poop": -5}
DIGEST_CATEGORY_ICONS = {
    "ai": "🤖", "investments": "💰", "payments": "💳", "vibecoding": "🛠️",
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
    "payments": [
        "https://hnrss.org/newest?q=AI+agent+payment+OR+stablecoin+agent+OR+agentic+commerce",
        "https://www.pymnts.com/feed/",
        "https://thefintechtimes.com/feed/",
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
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    ],
    "investments": [
        "https://www.marketwatch.com/rss/topstories",
    ],
}


# Serializes read-modify-write cycles on the JSON state files: scheduled jobs
# and user commands render in separate executor threads and may overlap.
STATE_LOCK = threading.RLock()


class NewsFetchError(Exception):
    def __init__(self, message: str, debug_logs: list[str] | None = None):
        super().__init__(message)
        self.debug_logs = debug_logs or []


def atomic_write_json(path: str, data: object, indent: int | None = None) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp_path, path)


def source_name_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
    except ValueError:
        return ""
    return host.lower().split(":")[0].removeprefix("www.")


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
    elif block_name == "hot":
        html_mode = news_spoilers and not debug_mode
        text = build_hot_news_block(force_refresh=force_refresh, use_spoilers=html_mode)
    elif block_name == "ideas":
        text = build_ideas_block(force_refresh=force_refresh)
    else:
        raise ValueError(f"Unknown block: {block_name}")

    if include_version:
        text = with_version(text)
    return text, html_mode


async def render_block_async(**kwargs) -> tuple[str, bool]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, partial(render_block, **kwargs))
    except Exception as exc:
        block_name = kwargs.get("block_name", "?")
        logger.exception("render_block(%s) crashed", block_name)
        text = f"Блок {block_name}: временно недоступен ({exc})"
        if kwargs.get("include_version"):
            text = with_version(text)
        return text, False


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
    response = requests.get(url, headers=req_headers, timeout=HTTP_TIMEOUT_LONG)
    response.raise_for_status()
    return response.text


def get_url_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    req_headers = headers or REQUEST_HEADERS_GENERIC
    response = requests.get(url, headers=req_headers, timeout=HTTP_TIMEOUT_LONG)
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


def fetch_cyprus_reservoirs_fragmata() -> str:
    response = requests.get(
        FRAGMATA_SUMMARY_URL,
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    data = response.json()

    inflow_24h = float(data["inflowLast24h"])
    inflow_since = float(data["inflowSinceOctober"])
    current_mcm = float(data["totalStorage"])
    current_pct = float(data["totalStoragePercent"])
    last_year_mcm = float(data["lastYearStorage"])
    last_year_pct = float(data["lastYearStoragePercent"])
    report_date = parse_wdd_report_date(str(data.get("reportDate", "")))

    return (
        "Cyprus reservoirs:\n"
        f"Inflow: +{inflow_24h:.3f} MCM (24h), +{inflow_since:.3f} MCM (since 1 Oct)\n"
        f"Now: {current_mcm:.3f} MCM ({current_pct:.2f}%)\n"
        f"Last year: {last_year_mcm:.3f} MCM ({last_year_pct:.2f}%)\n"
        f"Report date: {report_date}\n"
        "Source: fragmata.info (WDD weekly data)"
    )


def fetch_cyprus_reservoirs_wdd_xlsx() -> str:
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


def fetch_fear_and_greed() -> tuple[float, str, str, float | None]:
    """Return (score, rating, updated_at, prev_1_week_score_or_None)."""
    response = requests.get(CNN_API_URL, headers=REQUEST_HEADERS_CNN, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    data = response.json()

    fg = data["fear_and_greed"]
    score = float(fg["score"])
    rating = str(fg["rating"])
    dt_utc = parse_timestamp_utc(fg["timestamp"])
    updated_at = format_cyprus_time(dt_utc)

    prev_week_raw = fg.get("previous_1_week")
    prev_week: float | None = None
    if prev_week_raw is not None:
        try:
            prev_week = float(prev_week_raw)
        except (TypeError, ValueError):
            prev_week = None

    return score, rating, updated_at, prev_week


def fetch_crypto_fear_and_greed() -> tuple[int, str, str, int | None]:
    """Return (score, rating, updated_at, score_7d_ago_or_None). API returns newest first."""
    response = requests.get(CRYPTO_API_URL, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    rows = response.json().get("data", [])
    if not rows:
        raise ValueError("crypto FG: empty data")

    latest = rows[0]
    score = int(latest["value"])
    rating = str(latest["value_classification"])
    dt_utc = parse_timestamp_utc(latest["timestamp"])
    updated_at = format_cyprus_time(dt_utc)

    prev_week: int | None = None
    if len(rows) >= 8:
        try:
            prev_week = int(rows[7]["value"])
        except (KeyError, TypeError, ValueError):
            prev_week = None

    return score, rating, updated_at, prev_week


CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"


def _try_source(label: str, fetch):
    """Call fetch() and log failures without raising. Returns None on error."""
    try:
        return fetch()
    except Exception as exc:
        logger.warning("market source %s failed: %s", label, exc)
        return None


def _fetch_yahoo_btc_spx() -> tuple[float | None, float | None, float | None]:
    """Return (btc, spx_cash, spx_futures). ES=F trades ~23/5, so it doubles as the
    pre-/after-market proxy for S&P 500 — much more accurate than SPY×10."""
    response = requests.get(
        YAHOO_QUOTE_URL,
        params={"symbols": "BTC-USD,^GSPC,ES=F"},
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    btc = spx = futures = None
    for item in response.json()["quoteResponse"]["result"]:
        price = item.get("regularMarketPrice")
        if price is None:
            continue
        sym = item.get("symbol")
        if sym == "BTC-USD":
            btc = float(price)
        elif sym == "^GSPC":
            spx = float(price)
        elif sym == "ES=F":
            futures = float(price)
    return btc, spx, futures


def _fetch_yahoo_week_ago_prices() -> tuple[float | None, float | None]:
    """Return (btc_week_ago, spx_week_ago) — closes from ~7 days ago.

    Uses Yahoo spark endpoint which batches multiple symbols. range=10d with
    interval=1d gives ~10 points for BTC (24/7) and ~6-7 points for ^GSPC
    (weekdays only); we pick the close whose timestamp is nearest to 7 days
    ago (falling back to the oldest close if timestamps are missing).
    """
    response = requests.get(
        YAHOO_SPARK_URL,
        params={"symbols": "BTC-USD,^GSPC", "range": "10d", "interval": "1d"},
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    target_ts = datetime.now(timezone.utc).timestamp() - 7 * 86400
    btc_wk = spx_wk = None
    for entry in response.json().get("spark", {}).get("result", []):
        symbol = entry.get("symbol")
        responses = entry.get("response", [])
        if not responses:
            continue
        indicators = responses[0].get("indicators", {}).get("quote", [{}])
        closes = (indicators[0] if indicators else {}).get("close", [])
        timestamps = responses[0].get("timestamp", [])
        pairs = [
            (float(ts), float(close))
            for ts, close in zip(timestamps, closes)
            if ts is not None and close is not None
        ]
        if len(pairs) >= 2:
            week_ago = min(pairs, key=lambda p: abs(p[0] - target_ts))[1]
        else:
            valid_closes = [c for c in closes if c is not None]
            if len(valid_closes) < 2:
                continue
            week_ago = float(valid_closes[0])
        if symbol == "BTC-USD":
            btc_wk = week_ago
        elif symbol == "^GSPC":
            spx_wk = week_ago
    return btc_wk, spx_wk


def _fetch_stooq_daily_oldest_close(symbol: str, days: int = 12) -> float:
    """Fetch daily OHLC CSV from Stooq over the last `days`; return earliest close.

    Stooq path is /q/d/l/?s=SYMBOL&d1=YYYYMMDD&d2=YYYYMMDD&i=d. Symbol examples:
    btcusd, ^spx. CSV header: Date,Open,High,Low,Close,Volume — we want col 4 (Close).
    """
    today = datetime.now(timezone.utc).date()
    params = {
        "s": symbol,
        "d1": (today - timedelta(days=days)).strftime("%Y%m%d"),
        "d2": today.strftime("%Y%m%d"),
        "i": "d",
    }
    response = requests.get(STOOQ_DAILY_URL, params=params, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    body = response.text
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"stooq daily: empty response for {symbol}")
    start = 1 if lines[0].lower().startswith("date") else 0
    for row in lines[start:]:
        parts = [p.strip() for p in row.split(",")]
        if len(parts) >= 5 and parts[4] not in {"", "N/D"}:
            return float(parts[4])
    snippet = body[:140].replace("\n", " | ")
    raise ValueError(f"stooq daily: no numeric close for {symbol}; body: {snippet!r}")


def _fetch_coingecko_btc_week_ago() -> float:
    """CoinGecko market_chart — 7-day BTC history; return oldest price.

    For days<=90 CoinGecko returns hourly data, the first entry is ~7 days old.
    """
    response = requests.get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": "7"},
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    prices = response.json().get("prices", [])
    if not prices:
        raise ValueError("coingecko history: empty prices")
    return float(prices[0][1])


def _fetch_fred_spx_week_ago() -> float:
    """FRED SP500 daily series — last close on or before (today - 7 days).

    FRED returns ascending CSV (DATE,SP500); close-less days appear as '.' or ''.
    """
    response = requests.get(FRED_SPX_CSV_URL, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    rows = [line.strip() for line in response.text.splitlines() if line.strip()]
    if len(rows) < 2:
        raise ValueError("FRED: empty response")
    target = datetime.now(timezone.utc).date() - timedelta(days=7)
    best: float | None = None
    for line in rows[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1] in {"", "."}:
            continue
        try:
            row_date = datetime.strptime(parts[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if row_date <= target:
            best = float(parts[1])
        else:
            break  # rows are ascending; past target now
    if best is None:
        raise ValueError("FRED: no row on or before 7 days ago")
    return best


def _fetch_week_ago_prices() -> tuple[float | None, float | None]:
    """Return (btc_week_ago, spx_week_ago). Tries Yahoo spark (batched) first,
    then per-symbol fallbacks: CoinGecko for BTC; Stooq daily → FRED daily for ^SPX."""
    btc_wk: float | None = None
    spx_wk: float | None = None

    yahoo = _try_source("yahoo_spark", _fetch_yahoo_week_ago_prices)
    if yahoo:
        btc_wk, spx_wk = yahoo

    if btc_wk is None:
        btc_wk = _try_source("coingecko_hist", _fetch_coingecko_btc_week_ago)
    if spx_wk is None:
        spx_wk = _try_source(
            "stooq_daily_spx",
            lambda: _fetch_stooq_daily_oldest_close("^spx"),
        )
    if spx_wk is None:
        spx_wk = _try_source("fred_spx_wk", _fetch_fred_spx_week_ago)
    return btc_wk, spx_wk


def _fetch_cnbc_spx() -> float:
    """CNBC .SPX real-time — fallback for S&P cash when Yahoo is rate-limited."""
    return _fetch_cnbc_quote_last(".SPX")


def _fetch_cnbc_futures() -> float:
    """Try known CNBC symbol variants for E-mini S&P 500 front-month futures.

    Restricted to futures-specific symbol formats. Bare 'ES' is excluded because
    it collides with NYSE:ES (Eversource Energy stock, ~$70) — in v2.7.2 that
    collision surfaced as a '-99% vs cash' nonsense reading on /st.
    """
    last_err: Exception | None = None
    for sym in ("@ES.1", "@ES", "ES.1"):
        try:
            return _fetch_cnbc_quote_last(sym)
        except (ValueError, requests.RequestException) as exc:
            last_err = exc
            continue
    raise ValueError(f"CNBC: no ES futures symbol worked (last: {last_err})")


def _fetch_yahoo_chart_price(symbol: str) -> float:
    """Yahoo v8 chart meta price — crumb-free endpoint that keeps working when
    the v7 quote API answers 401 (Invalid Crumb) or 429. For ES=F the meta
    price is live nearly 24/5, which is what makes the futures line usable as
    the pre-/after-market proxy."""
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"range": "1d", "interval": "5m"},
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    price = result.get("meta", {}).get("regularMarketPrice")
    if price is None:
        raise ValueError(f"yahoo chart: no meta price for {symbol}")
    return float(price)


def _fetch_cnbc_quote_last(symbol: str) -> float:
    response = requests.get(
        CNBC_QUOTE_URL,
        params={
            "symbols": symbol,
            "requestMethod": "itv",
            "noBody": "1",
            "partnerId": "2",
            "fund": "1",
            "output": "json",
        },
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    quotes = response.json().get("FormattedQuoteResult", {}).get("FormattedQuote", [])
    entry = None
    for q in quotes:
        if q.get("symbol") == symbol:
            entry = q
            break
    if entry is None:
        seen = [q.get("symbol") for q in quotes]
        raise ValueError(f"CNBC: {symbol!r} not in response (got {seen[:5]})")
    # Off-session the price moves out of "last" into the extended-market quote
    # (that's why the futures line vanished exactly when it was needed).
    extended = entry.get("ExtendedMktQuote") or {}
    for raw in (entry.get("last"), extended.get("last"), entry.get("previous_day_closing")):
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            continue
    raise ValueError(
        f"CNBC: {symbol!r} present but no price fields (keys: {sorted(entry)[:10]})"
    )


def _fetch_coinbase_btc() -> float:
    response = requests.get(
        COINBASE_BTC_URL, params={"currency": "USD"}, timeout=HTTP_TIMEOUT_SHORT
    )
    response.raise_for_status()
    return float(response.json()["data"]["amount"])


def _fetch_coingecko_btc() -> float:
    response = requests.get(
        COINGECKO_BTC_URL,
        params={"ids": "bitcoin", "vs_currencies": "usd"},
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    return float(response.json()["bitcoin"]["usd"])


def _fetch_stooq_spx() -> float:
    response = requests.get(STOOQ_SPX_CSV_URL, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    price, _ = parse_stooq_csv_line(response.text)
    return price


def _fetch_stooq_es_futures() -> float:
    """Stooq continuous E-mini S&P 500 futures (es.f) — last-resort futures
    source for when Yahoo is rate-limited and CNBC misbehaves. The ±10%%
    vs-cash sanity check in fetch_market_prices guards against this symbol
    ever resolving to a different instrument."""
    response = requests.get(STOOQ_ES_FUTURES_CSV_URL, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    price, _ = parse_stooq_csv_line(response.text)
    return price


def _fetch_fred_spx() -> float:
    # FRED daily S&P500 series, CSV columns: DATE,SP500. Walk backwards for latest numeric.
    response = requests.get(FRED_SPX_CSV_URL, timeout=HTTP_TIMEOUT_SHORT)
    response.raise_for_status()
    rows = [line.strip() for line in response.text.splitlines() if line.strip()]
    for line in reversed(rows[1:]):
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] not in {"", "."}:
            return float(parts[1])
    raise ValueError("FRED: no numeric row")


def fetch_market_prices() -> tuple[float, float, float | None]:
    """Return (btc_price, spx_price, spx_futures_or_None).

    spx_futures is the ES=F E-mini S&P 500 futures price when available; during
    regular hours it tracks the cash index with small basis, off-hours it acts
    as the pre-/after-market proxy.
    """
    global LAST_BTC_PRICE, LAST_SPX_PRICE

    btc_price: float | None = None
    spx_price: float | None = None
    spx_futures: float | None = None

    yahoo = _try_source("yahoo", _fetch_yahoo_btc_spx)
    if yahoo:
        btc_price, spx_price, spx_futures = yahoo

    if spx_futures is None:
        spx_futures = _try_source(
            "yahoo_chart_es", lambda: _fetch_yahoo_chart_price("ES=F")
        )
    if spx_futures is None:
        spx_futures = _try_source("cnbc_es", _fetch_cnbc_futures)
    if spx_futures is None:
        spx_futures = _try_source("stooq_es", _fetch_stooq_es_futures)
    if spx_price is None:
        spx_price = _try_source("cnbc", _fetch_cnbc_spx)
    if spx_price is None:
        spx_price = _try_source(
            "yahoo_chart_spx", lambda: _fetch_yahoo_chart_price("^GSPC")
        )
    if btc_price is None:
        btc_price = _try_source("coinbase", _fetch_coinbase_btc)
    if btc_price is None:
        btc_price = _try_source("coingecko", _fetch_coingecko_btc)
    if spx_price is None:
        spx_price = _try_source("stooq", _fetch_stooq_spx)
    if spx_price is None:
        spx_price = _try_source("fred", _fetch_fred_spx)

    # Last-resort: last successful values in memory.
    if btc_price is None:
        btc_price = LAST_BTC_PRICE
    if spx_price is None:
        spx_price = LAST_SPX_PRICE

    if btc_price is None or spx_price is None:
        raise ValueError("не удалось получить цены BTC/S&P ни из одного источника")

    LAST_BTC_PRICE = btc_price
    LAST_SPX_PRICE = spx_price

    # Sanity: ES front-month should track cash S&P within a few %. Anything
    # further off is almost certainly a wrong symbol (ticker collision or
    # API returning a different instrument).
    if spx_futures is not None and not (0.9 * spx_price <= spx_futures <= 1.1 * spx_price):
        logger.warning(
            "spx_futures=%.2f discarded (far from spx=%.2f, likely wrong symbol)",
            spx_futures, spx_price,
        )
        spx_futures = None

    return btc_price, spx_price, spx_futures


def build_st_debug_block() -> str:
    """Probe every market data source with raw values/errors — shows why the
    futures (pre-/after-market) line is missing when it is."""
    lines = ["Market sources probe:"]

    try:
        response = requests.get(
            YAHOO_QUOTE_URL,
            params={"symbols": "BTC-USD,^GSPC,ES=F"},
            headers=REQUEST_HEADERS_GENERIC,
            timeout=HTTP_TIMEOUT_SHORT,
        )
        lines.append(f"• yahoo v7 quote: HTTP {response.status_code}")
        if response.ok:
            results = response.json().get("quoteResponse", {}).get("result", [])
            if not results:
                lines.append("  (empty result list)")
            for item in results:
                parts = [f"  {item.get('symbol')}: state={item.get('marketState', '?')}"]
                for field in ("regularMarketPrice", "preMarketPrice", "postMarketPrice"):
                    value = item.get(field)
                    if value is not None:
                        parts.append(f"{field}={value}")
                lines.append(" ".join(parts))
        else:
            lines.append(f"  body: {normalize_text(response.text)[:160]}")
    except Exception as exc:
        lines.append(f"• yahoo v7 quote FAILED {type(exc).__name__}: {exc}")

    for label, fetch in (
        ("yahoo v8 chart ^GSPC", lambda: _fetch_yahoo_chart_price("^GSPC")),
        ("yahoo v8 chart ES=F", lambda: _fetch_yahoo_chart_price("ES=F")),
        ("cnbc .SPX", _fetch_cnbc_spx),
        ("cnbc @ES.1", lambda: _fetch_cnbc_quote_last("@ES.1")),
        ("cnbc @ES", lambda: _fetch_cnbc_quote_last("@ES")),
        ("cnbc ES.1", lambda: _fetch_cnbc_quote_last("ES.1")),
        ("stooq es.f", _fetch_stooq_es_futures),
        ("coinbase BTC", _fetch_coinbase_btc),
        ("coingecko BTC", _fetch_coingecko_btc),
        ("stooq ^SPX", _fetch_stooq_spx),
        ("fred SP500", _fetch_fred_spx),
    ):
        try:
            lines.append(f"• {label}: {fetch():,.2f}")
        except Exception as exc:
            lines.append(f"• {label} FAILED {type(exc).__name__}: {str(exc)[:140]}")

    lines.append("")
    lines.append("Пришлите этот вывод — по нему чинится строка futures/pre-market в /st.")
    return "\n".join(lines)


def fetch_fx_yahoo() -> tuple[float, float, str]:
    response = requests.get(
        YAHOO_QUOTE_URL,
        params={"symbols": "EURUSD=X,EURRUB=X,RUB=X,USDRUB=X"},
        headers=REQUEST_HEADERS_GENERIC,
        timeout=HTTP_TIMEOUT_SHORT,
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
        timeout=HTTP_TIMEOUT_SHORT,
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
    response = requests.get(OPEN_ER_API_URL, timeout=HTTP_TIMEOUT_SHORT)
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
    Stooq first (best current reliability), then fallbacks. Last source raises on failure.
    """
    for label, fetch in (
        ("stooq", fetch_fx_stooq),
        ("yahoo", fetch_fx_yahoo),
        ("frankfurter", fetch_fx_frankfurter),
    ):
        result = _try_source(f"fx:{label}", fetch)
        if result is not None:
            return result
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
    except Exception as exc:
        logger.warning("load_news_history failed: %s", exc)
        return {}
    return {}


def save_news_history(history: dict[str, float]) -> None:
    atomic_write_json(NEWS_HISTORY_FILE, history)


def load_bot_state() -> dict[str, object]:
    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("load_bot_state failed: %s", exc)
        return {}
    return {}


def save_bot_state(state: dict[str, object]) -> None:
    atomic_write_json(BOT_STATE_FILE, state)


def format_state_saved_time(saved_at: object) -> str:
    raw = normalize_text(str(saved_at))
    if not raw:
        return "n/a"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_cyprus_time(dt.astimezone(timezone.utc))
    except Exception as exc:
        logger.debug("format_state_saved_time fallback for %r: %s", raw, exc)
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


ATOM_NS = "{http://www.w3.org/2005/Atom}"
DC_DATE_TAG = "{http://purl.org/dc/elements/1.1/}date"


def _build_feed_item(
    category: str,
    title: str,
    link: str,
    source: str,
    details: str,
    published_dt: datetime | None,
) -> dict[str, str] | None:
    if not title or not link or not published_dt:
        return None
    return {
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


def _atom_pick_link(node) -> str:
    link = ""
    for link_node in node.findall(f"{ATOM_NS}link"):
        href = normalize_text(link_node.attrib.get("href", ""))
        rel = normalize_text(link_node.attrib.get("rel", "alternate")).lower()
        if href and rel in {"", "alternate"}:
            return href
        if href and not link:
            link = href
    return link


def parse_news_feed_items(
    xml_text: str, category: str, feed_url: str = ""
) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    out: list[dict[str, str]] = []
    # Feeds almost never carry the optional <source> tag, so derive the source
    # name from the article domain (works for aggregators like hnrss too),
    # falling back to the feed's own domain. A literal "RSS" bucket would
    # collapse all sources into one rating entry.
    feed_source = source_name_from_url(feed_url)

    # RSS format: channel/item
    for node in root.findall(".//channel/item"):
        title = normalize_text(unescape(node.findtext("title", default="")))
        link = normalize_text(node.findtext("link", default=""))
        source = (
            normalize_text(node.findtext("source", default=""))
            or source_name_from_url(link)
            or feed_source
            or "RSS"
        )
        details = normalize_text(unescape(node.findtext("description", default="")))
        published_raw = (
            node.findtext("pubDate", default="")
            or node.findtext("date", default="")
            or node.findtext(DC_DATE_TAG, default="")
        )
        item = _build_feed_item(
            category, title, link, source, details, parse_feed_datetime(published_raw)
        )
        if item:
            out.append(item)

    # Atom format: entry with XML namespace.
    for node in root.findall(f".//{ATOM_NS}entry"):
        title = normalize_text(unescape(node.findtext(f"{ATOM_NS}title", default="")))
        details = normalize_text(unescape(node.findtext(f"{ATOM_NS}summary", default="")))
        if not details:
            details = normalize_text(unescape(node.findtext(f"{ATOM_NS}content", default="")))
        link = _atom_pick_link(node)
        source = source_name_from_url(link) or feed_source or "RSS"
        published_raw = (
            node.findtext(f"{ATOM_NS}updated", default="")
            or node.findtext(f"{ATOM_NS}published", default="")
        )
        item = _build_feed_item(
            category, title, link, source, details, parse_feed_datetime(published_raw)
        )
        if item:
            out.append(item)

    return out


def parse_json_payload(text: str) -> object:
    payload = text.strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```[a-zA-Z0-9]*\n?", "", payload)
        payload = re.sub(r"\n?```$", "", payload).strip()
    return json.loads(payload)


def fetch_article_text(url: str) -> str:
    """Best-effort extraction of the article body for AI summarization."""
    max_chars = int(os.getenv("NEWS_ARTICLE_MAX_CHARS", "4000"))
    timeout_seconds = int(os.getenv("NEWS_ARTICLE_FETCH_TIMEOUT_SECONDS", "8"))
    try:
        response = requests.get(
            url, headers=REQUEST_HEADERS_GENERIC, timeout=timeout_seconds
        )
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        logger.debug("fetch_article_text failed for %s: %s", url, exc)
        return ""
    html = re.sub(r"(?is)<(script|style|noscript|svg|form)[^>]*>.*?</\1>", " ", html)
    chunks: list[str] = []
    total = 0
    for paragraph in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html):
        text = normalize_text(unescape(re.sub(r"(?s)<[^>]+>", " ", paragraph)))
        # Short fragments are almost always menus, captions or cookie banners.
        if len(text) < 60:
            continue
        chunks.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return " ".join(chunks)[:max_chars]


def attach_article_texts(items: list[dict[str, str]]) -> int:
    """Fetch article bodies concurrently into item['_article_en']. Returns hit count."""
    if os.getenv("NEWS_SUMMARY_FETCH_ARTICLES", "1").strip() != "1":
        return 0
    targets = [item for item in items if item.get("url") and not item.get("_article_en")]
    if not targets:
        return 0
    workers = max(1, min(int(os.getenv("NEWS_ARTICLE_FETCH_WORKERS", "6")), 12))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        texts = list(pool.map(lambda item: fetch_article_text(item["url"]), targets))
    fetched = 0
    for item, text in zip(targets, texts):
        if text:
            item["_article_en"] = text
            fetched += 1
    return fetched


def summarize_news_best_effort(
    items: list[dict[str, str]], debug_logs: list[str] | None = None
) -> None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not items:
        if debug_logs is not None:
            debug_logs.append("Summary: skipped (no OPENAI_API_KEY or empty list)")
        return
    fetched = attach_article_texts(items)
    if debug_logs is not None:
        debug_logs.append(f"Article fetch: {fetched}/{len(items)} bodies")
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
                "article_en": item.get("_article_en", ""),
            }
            for idx, item in enumerate(batch)
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a news editor. For each item write a Russian summary "
                    "based on article_en (the article body). "
                    "Return strict JSON only with schema: "
                    '{"items":[{"id":0,"headline_ru":"","details_ru":""}]}. '
                    "headline_ru: short natural Russian headline. "
                    "details_ru: a factual retelling of the story in Russian, 3-6 short "
                    "lines separated by newline characters, covering the concrete facts "
                    "(who, what, where, when, numbers) from article_en. "
                    "Use only facts stated in the provided fields. Never invent details "
                    "and never pad with generic filler sentences. If article_en is empty, "
                    "translate headline_en and details_en as-is; details_ru may then be "
                    "1-2 lines. Do not add markdown."
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
                stage_label=f"news summary batch {start // batch_size + 1}",
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
                debug_logs.append(f"Summary batch failed ({exc})")
            continue
    if debug_logs is not None:
        debug_logs.append("Summary: done" if translated_any else "Summary: skipped")


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
    now_ts = now_utc_dt.timestamp()
    history = prune_news_history(load_news_history(), now_ts)
    by_category: dict[str, list[dict[str, str]]] = {k: [] for k, _ in NEWS_TARGETS}
    seen: set[str] = set()
    debug_logs: list[str] = []
    if debug_mode:
        debug_logs.append(f"UTC now: {now_utc}")
        debug_logs.append(f"History: {len(history)} items in last {NEWS_HISTORY_HOURS}h")
        debug_logs.append("Cache: miss (forced live fetch)")
    # Niche agentpay goes first: its stories also appear in the broad AI feeds,
    # and whichever category collects an item first wins via the dedup set.
    fetch_order = sorted(NEWS_TARGETS, key=lambda t: t[0] != "agentpay")
    for category, count in fetch_order:
        window_hours = AGENTPAY_WINDOW_HOURS if category == "agentpay" else 24
        cutoff = now_ts - window_hours * 3600
        if debug_mode:
            debug_logs.append(f"RSS: category {category} target {count} window {window_hours}h")
        feeds = NEWS_RSS_FEEDS.get(category, [])
        collected: list[dict[str, str]] = []
        for feed_url in feeds:
            if len(collected) >= count:
                break
            try:
                xml_text = get_url_text(feed_url)
                feed_items = parse_news_feed_items(xml_text, category, feed_url)
                if category == "agentpay":
                    feed_items = [
                        item
                        for item in feed_items
                        if AGENTPAY_KEYWORD_RE.search(
                            f"{item['headline_en']} {item['details_en']}"
                        )
                    ]
                fresh_items: list[dict[str, str]] = []
                for item in feed_items:
                    published_raw = item.pop("_published_dt", "")
                    published_dt = parse_feed_datetime(published_raw)
                    if not published_dt:
                        continue
                    if published_dt.timestamp() < cutoff:
                        continue
                    fp = news_item_fingerprint(item)
                    if fp in seen or fp in history:
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

    summarize_news_best_effort(final_items, debug_logs if debug_mode else None)

    if debug_mode:
        counts = ", ".join(f"{cat}={len(by_category[cat])}" for cat, _ in NEWS_TARGETS)
        debug_logs.append(f"Final items: {counts}, total={len(final_items)}")

    if not final_items:
        raise NewsFetchError("No fresh RSS items from last 24h", debug_logs)

    with STATE_LOCK:
        history = prune_news_history(load_news_history(), now_ts)
        for item in final_items:
            history[news_item_fingerprint(item)] = now_ts
        save_news_history(history)

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
        max_items = sum(count for _, count in NEWS_TARGETS)
        for item in ai_items[:max_items]:
            raw_cat = item.get("category", "news")
            cat = NEWS_CATEGORY_LABELS.get(raw_cat, raw_cat.capitalize())
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


# ── /hot: most-discussed stories ─────────────────────────────────────────────


def ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def _title_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in HOT_TITLE_STOPWORDS}


def _same_story(tokens_a: set[str], tokens_b: set[str]) -> bool:
    if not tokens_a or not tokens_b:
        return False
    inter = len(tokens_a & tokens_b)
    overlap = inter / min(len(tokens_a), len(tokens_b))
    # Different outlets word the same event differently; 3+ shared significant
    # words (or 2+ covering most of the shorter headline) is a solid match.
    return inter >= 3 or (inter >= 2 and overlap >= 0.6)


def _fetch_hot_feed(category: str, feed_url: str) -> list[dict[str, str]]:
    try:
        xml_text = get_url_text(feed_url)
        return parse_news_feed_items(xml_text, category, feed_url)
    except Exception as exc:
        logger.warning("hot feed %s failed: %s", feed_url, exc)
        return []


def collect_hot_items() -> list[dict[str, str]]:
    cutoff = datetime.now(timezone.utc).timestamp() - HOT_WINDOW_HOURS * 3600
    tasks = [(cat, url) for cat, urls in HOT_RSS_FEEDS.items() for url in urls]
    items: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for feed_items in pool.map(lambda t: _fetch_hot_feed(*t), tasks):
            items.extend(feed_items)

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in items:
        pub_raw = item.pop("_published_dt", "")
        pub_dt = parse_feed_datetime(pub_raw)
        if not pub_dt or pub_dt.timestamp() < cutoff:
            continue
        fp = news_item_fingerprint(item)
        if fp in seen:
            continue
        seen.add(fp)
        item["_ts"] = str(pub_dt.timestamp())
        out.append(item)
    return out


def cluster_hot_items(items: list[dict[str, str]]) -> list[dict]:
    """Group items into story clusters by headline token similarity."""
    clusters: list[dict] = []
    for item in items:
        tokens = _title_tokens(item.get("headline_en", ""))
        target = None
        for cluster in clusters:
            if any(_same_story(tokens, member) for member in cluster["token_sets"]):
                target = cluster
                break
        if target is None:
            target = {"items": [], "token_sets": []}
            clusters.append(target)
        target["items"].append(item)
        target["token_sets"].append(tokens)

    for cluster in clusters:
        cluster["sources"] = {i.get("source", "") for i in cluster["items"]}
        cluster["newest_ts"] = max(float(i.get("_ts", "0")) for i in cluster["items"])
    return clusters


def build_hot_news_block(force_refresh: bool = False, use_spoilers: bool = False) -> str:
    ttl = int(os.getenv("NEWS_CACHE_TTL_SECONDS", "300"))
    fallback_ttl = int(os.getenv("NEWS_FALLBACK_CACHE_TTL_SECONDS", "120"))
    now_ts = datetime.now(timezone.utc).timestamp()
    cache_key = "hot_html" if use_spoilers else "hot_plain"
    cached_entry = NEWS_CACHE.get(cache_key)
    if (
        not force_refresh
        and isinstance(cached_entry, dict)
        and cached_entry.get("content")
        and float(cached_entry.get("expires_at", 0.0)) > now_ts
    ):
        return str(cached_entry["content"])

    try:
        items = collect_hot_items()
        if not items:
            raise NewsFetchError(f"нет свежих новостей за последние {HOT_WINDOW_HOURS}ч")

        clusters = cluster_hot_items(items)
        clusters.sort(
            key=lambda c: (len(c["sources"]), len(c["items"]), c["newest_ts"]),
            reverse=True,
        )
        top = clusters[:HOT_TARGET_COUNT]

        # One representative (freshest item) per cluster; translated as a batch.
        reps = [
            max(c["items"], key=lambda x: float(x.get("_ts", "0"))) for c in top
        ]
        summarize_news_best_effort(reps)

        lines = [f"🔥 Самые обсуждаемые ({HOT_WINDOW_HOURS}ч):", ""]
        for idx, (cluster, rep) in enumerate(zip(top, reps), 1):
            n_src = len(cluster["sources"])
            icon = HOT_CATEGORY_ICONS.get(rep.get("category", ""), "📰")
            headline = rep.get("headline_ru") or rep.get("headline_en", "")
            src_label = f"{n_src} {ru_plural(n_src, 'источник', 'источника', 'источников')}"
            details = format_expanded_details(rep)

            # Newest link per distinct source, capped to keep messages compact.
            newest_per_source: dict[str, dict[str, str]] = {}
            for member in sorted(
                cluster["items"], key=lambda x: float(x.get("_ts", "0")), reverse=True
            ):
                newest_per_source.setdefault(member.get("source", ""), member)
            link_items = list(newest_per_source.items())[:4]

            if use_spoilers:
                lines.append(f"{idx}. {icon} <b>{html_escape(headline)}</b> — {src_label}")
                lines.append(f"<blockquote expandable>{html_escape(details)}</blockquote>")
                lines.append(
                    " · ".join(
                        f'<a href="{html_escape(member["url"], quote=True)}">{html_escape(source)}</a>'
                        for source, member in link_items
                    )
                )
            else:
                lines.append(f"{idx}. {icon} {headline} — {src_label}")
                lines.append(details)
                for source, member in link_items:
                    lines.append(f"{source}: {member['url']}")
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
        content = (
            f"Обсуждаемые новости: временно недоступно ({exc})\n\n"
            f"Updated: {format_cyprus_time(datetime.now(timezone.utc))}"
        )
        NEWS_CACHE[cache_key] = {
            "content": content,
            "expires_at": now_ts + max(fallback_ttl, 30),
            "updated_at": now_ts,
        }
        return content


# ── /ideas: Smart Money (13F + analysts) ────────────────────────────────────


def _edgar_headers() -> dict[str, str]:
    # SEC asks automated clients to identify themselves via User-Agent.
    ua = os.getenv(
        "EDGAR_USER_AGENT",
        "fear-greed-telegram-bot/1.0 (+https://github.com/oleglgt/fear-greed-telegram-bot)",
    )
    return {"User-Agent": ua, "Accept": "application/json,text/xml,*/*"}


def _edgar_get(url: str) -> requests.Response:
    time_module.sleep(0.15)  # stay far below SEC's 10 req/s limit
    response = requests.get(url, headers=_edgar_headers(), timeout=HTTP_TIMEOUT_LONG)
    response.raise_for_status()
    return response


def _fund_latest_13f_filings(cik: str) -> tuple[str, list[dict[str, str]]]:
    """Return (fund_name, [latest filing, previous-period filing]).

    Amendments (13F-HR/A) share the period with the original; per period we
    keep the most recently filed document.
    """
    data = _edgar_get(EDGAR_SUBMISSIONS_URL.format(cik=cik)).json()
    name = str(data.get("name") or IDEAS_FUNDS.get(cik, cik)).title()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    rows = [
        {
            "acc": recent["accessionNumber"][i],
            "period": recent["reportDate"][i],
            "filed": recent["filingDate"][i],
        }
        for i in range(len(forms))
        if str(forms[i]).startswith("13F-HR")
    ]
    by_period: dict[str, dict[str, str]] = {}
    for row in rows:  # rows come newest-filed first
        by_period.setdefault(row["period"], row)
    periods = sorted(by_period, reverse=True)[:2]
    return name, [by_period[p] for p in periods]


def _parse_13f_infotable(xml_text: str) -> dict[str, dict[str, object]]:
    """Aggregate a 13F information table into {cusip: {issuer, value, shares}}.

    Namespace-agnostic (funds use different xmlns prefixes); derivative rows
    (putCall) are skipped; multiple rows per CUSIP (split voting authority)
    are summed.
    """
    root = ET.fromstring(xml_text)
    holdings: dict[str, dict[str, object]] = {}
    for table in root.iter():
        if not table.tag.lower().endswith("infotable"):
            continue
        fields: dict[str, str] = {}
        for child in table.iter():
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if child.text and child.text.strip() and tag not in fields:
                fields[tag] = child.text.strip()
        if fields.get("putcall"):
            continue
        cusip = fields.get("cusip", "").upper()
        if not cusip:
            continue
        try:
            value = float(fields.get("value", "0"))
        except ValueError:
            value = 0.0
        try:
            shares = float(fields.get("sshprnamt", "0"))
        except ValueError:
            shares = 0.0
        entry = holdings.setdefault(
            cusip, {"issuer": fields.get("nameofissuer", cusip), "value": 0.0, "shares": 0.0}
        )
        entry["value"] = float(entry["value"]) + value
        entry["shares"] = float(entry["shares"]) + shares
    return holdings


def _fetch_13f_holdings(cik: str, acc: str) -> dict[str, dict[str, object]]:
    base = EDGAR_ARCHIVES_URL.format(cik_int=str(int(cik)), acc=acc.replace("-", ""))
    index = _edgar_get(base + "index.json").json()
    xml_name = None
    for item in index.get("directory", {}).get("item", []):
        low = str(item.get("name", "")).lower()
        if not low.endswith(".xml") or "primary_doc" in low:
            continue
        xml_name = item["name"]
        if "infotable" in low or "form13f" in low:
            break
    if not xml_name:
        raise ValueError(f"13F info table xml not found in {base}")
    return _parse_13f_infotable(_edgar_get(base + xml_name).text)


def _diff_13f(
    cur: dict[str, dict[str, object]], prev: dict[str, dict[str, object]]
) -> tuple[list[dict], list[dict]]:
    buys: list[dict] = []
    sells: list[dict] = []
    for cusip, cur_e in cur.items():
        prev_e = prev.get(cusip)
        if prev_e is None:
            buys.append({"cusip": cusip, **cur_e, "action": "new", "pct": None})
            continue
        prev_shares = float(prev_e.get("shares", 0.0))
        if prev_shares <= 0:
            continue
        pct = (float(cur_e["shares"]) - prev_shares) / prev_shares * 100
        if pct >= IDEAS_CHANGE_PCT:
            buys.append({"cusip": cusip, **cur_e, "action": "increased", "pct": pct})
        elif pct <= -IDEAS_CHANGE_PCT:
            sells.append({"cusip": cusip, **cur_e, "action": "decreased", "pct": pct})
    for cusip, prev_e in prev.items():
        if cusip not in cur:
            sells.append({"cusip": cusip, **prev_e, "action": "exited", "pct": None})
    return buys, sells


def _map_cusips_to_tickers(cusips: list[str]) -> dict[str, str]:
    api_key = os.getenv("OPENFIGI_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    batch_size = 100 if api_key else 8
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    out: dict[str, str] = {}
    for start in range(0, len(cusips), batch_size):
        chunk = cusips[start : start + batch_size]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        try:
            response = requests.post(
                OPENFIGI_MAPPING_URL, json=jobs, headers=headers, timeout=HTTP_TIMEOUT_SHORT
            )
            if response.status_code == 429:
                time_module.sleep(15)
                response = requests.post(
                    OPENFIGI_MAPPING_URL, json=jobs, headers=headers, timeout=HTTP_TIMEOUT_SHORT
                )
            response.raise_for_status()
            for cusip, result in zip(chunk, response.json()):
                rows = result.get("data") or []
                ticker = str(rows[0].get("ticker", "")).strip() if rows else ""
                if ticker:
                    out[cusip] = ticker
        except Exception as exc:
            logger.warning("openfigi batch %d failed: %s", start // batch_size + 1, exc)
        time_module.sleep(0.3 if api_key else 3.0)  # free tier: 25 req/min
    return out


def build_ideas_universe() -> dict:
    """Rebuild the candidate universe from the funds' two latest 13F filings."""
    candidates: dict[str, dict] = {}
    exits: dict[str, dict] = {}
    funds_meta: list[dict[str, str]] = []
    last_error: Exception | None = None
    for cik, label in IDEAS_FUNDS.items():
        try:
            name, filings = _fund_latest_13f_filings(cik)
            if len(filings) < 2:
                logger.warning("ideas: %s has <2 13F periods, skipped", label)
                continue
            cur = _fetch_13f_holdings(cik, filings[0]["acc"])
            prev = _fetch_13f_holdings(cik, filings[1]["acc"])
            buys, sells = _diff_13f(cur, prev)
            funds_meta.append({"name": name, "period": filings[0]["period"]})
            for row in buys:
                c = candidates.setdefault(
                    row["cusip"], {"issuer": row["issuer"], "actions": []}
                )
                c["actions"].append(
                    {"fund": name, "action": row["action"], "pct": row["pct"], "value": row["value"]}
                )
            for row in sells:
                e = exits.setdefault(row["cusip"], {"issuer": row["issuer"], "actions": []})
                e["actions"].append({"fund": name, "action": row["action"], "pct": row["pct"]})
        except Exception as exc:
            last_error = exc
            logger.warning("ideas: fund %s (CIK %s) failed: %s", label, cik, exc)
    if not candidates:
        raise ValueError(
            f"13F: не удалось собрать покупки ни одного фонда; "
            f"последняя ошибка: {type(last_error).__name__}: {str(last_error)[:180]}"
        )

    tickers = _map_cusips_to_tickers(list(candidates) + [c for c in exits if c not in candidates])
    for cusip, entry in list(candidates.items()) + list(exits.items()):
        entry["ticker"] = tickers.get(cusip, "")

    universe = {
        "built_at": datetime.now(timezone.utc).timestamp(),
        "funds": funds_meta,
        "candidates": candidates,
        "exits": exits,
    }
    with STATE_LOCK:
        atomic_write_json(IDEAS_UNIVERSE_FILE, universe)
    return universe


def load_ideas_universe() -> dict | None:
    try:
        with open(IDEAS_UNIVERSE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("candidates") else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _fetch_finnhub_recommendation(ticker: str) -> dict | None:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        return None
    response = requests.get(
        FINNHUB_RECO_URL,
        params={"symbol": ticker, "token": api_key},
        timeout=HTTP_TIMEOUT_SHORT,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        return None
    latest = rows[0]
    baseline = rows[3] if len(rows) > 3 else rows[-1]
    sb = int(latest.get("strongBuy", 0))
    return {
        "strong_buy": sb,
        "buy": int(latest.get("buy", 0)),
        "hold": int(latest.get("hold", 0)),
        "sell": int(latest.get("sell", 0)) + int(latest.get("strongSell", 0)),
        "momentum": sb - int(baseline.get("strongBuy", 0)),
    }


def _score_idea(actions: list[dict], reco: dict | None) -> tuple[int, float | None]:
    """Return (score 0-100, strong_buy_ratio_or_None)."""
    fund_score = 0.0
    for action in actions:
        fund_score += 25.0 if action.get("action") == "new" else 15.0
    fund_score = min(fund_score, 50.0)

    analyst_score = 0.0
    ratio: float | None = None
    if reco:
        coverage = reco["strong_buy"] + reco["buy"] + reco["hold"] + reco["sell"]
        if coverage >= IDEAS_MIN_COVERAGE:
            ratio = reco["strong_buy"] / coverage
            momentum = max(-1.0, min(reco["momentum"] / 5.0, 1.0))
            analyst_score = max(0.0, min(ratio * 35.0 + momentum * 15.0, 50.0))
    return round(fund_score + analyst_score), ratio


def _fmt_usd(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    return f"${value:,.0f}"


def _fmt_fund_action(action: dict) -> str:
    fund = action.get("fund", "?")
    kind = action.get("action")
    pct = action.get("pct")
    value = float(action.get("value") or 0.0)
    if kind == "new":
        label = "новая позиция"
    elif kind == "increased":
        label = f"+{pct:.0f}%"
    elif kind == "decreased":
        label = f"{pct:.0f}%"
    else:
        label = "вышел"
    if value > 0 and kind in {"new", "increased"}:
        label += f" (~{_fmt_usd(value)})"
    return f"{fund}: {label}"


def build_ideas_block(force_refresh: bool = False, rebuild: bool = False) -> str:
    ttl = int(os.getenv("IDEAS_CACHE_TTL_SECONDS", "21600"))
    now_ts = datetime.now(timezone.utc).timestamp()
    cached_entry = IDEAS_CACHE.get("block")
    if (
        not force_refresh
        and not rebuild
        and isinstance(cached_entry, dict)
        and cached_entry.get("content")
        and float(cached_entry.get("expires_at", 0.0)) > now_ts
    ):
        return str(cached_entry["content"])

    try:
        universe = None if rebuild else load_ideas_universe()
        max_age = IDEAS_UNIVERSE_MAX_AGE_DAYS * 86400
        if universe is None or now_ts - float(universe.get("built_at", 0)) > max_age:
            universe = build_ideas_universe()

        candidates = universe.get("candidates", {})
        exits = universe.get("exits", {})

        # Analyst layer for the biggest fund bets only (Finnhub free tier).
        ranked = sorted(
            candidates.items(),
            key=lambda kv: max(float(a.get("value") or 0.0) for a in kv[1]["actions"]),
            reverse=True,
        )[:IDEAS_MAX_ANALYST_LOOKUPS]
        finnhub_on = bool(os.getenv("FINNHUB_API_KEY", "").strip())
        scored = []
        for cusip, entry in ranked:
            ticker = str(entry.get("ticker", ""))
            reco = None
            if ticker and finnhub_on:
                try:
                    reco = _fetch_finnhub_recommendation(ticker)
                except Exception as exc:
                    logger.warning("finnhub %s failed: %s", ticker, exc)
                time_module.sleep(1.05)  # 60 req/min free limit
            score, ratio = _score_idea(entry["actions"], reco)
            scored.append((score, ratio, ticker, entry, reco))
        scored.sort(key=lambda x: x[0], reverse=True)

        lines = ["💡 Smart Money Ideas (13F китов + аналитики):", ""]
        for idx, (score, ratio, ticker, entry, reco) in enumerate(
            scored[:IDEAS_TARGET_COUNT], 1
        ):
            name = ticker or str(entry.get("issuer", "?")).title()
            icon = "🤝" if (ratio is not None and ratio >= 0.4) else "🐋"
            lines.append(f"{idx}. {icon} {name} — {score}")
            for action in entry["actions"][:3]:
                lines.append(f"   🐋 {_fmt_fund_action(action)}")
            if reco:
                coverage = reco["strong_buy"] + reco["buy"] + reco["hold"] + reco["sell"]
                mom = reco["momentum"]
                mom_part = f", {'+' if mom >= 0 else ''}{mom} SB за 3 мес" if mom else ""
                if coverage >= IDEAS_MIN_COVERAGE:
                    lines.append(
                        f"   📈 {reco['strong_buy']} strongBuy / {reco['buy']} buy / "
                        f"{reco['hold']} hold / {reco['sell']} sell{mom_part}"
                    )
                else:
                    lines.append(f"   📈 мало покрытия ({coverage} аналитиков)")
            elif not finnhub_on:
                lines.append("   📈 аналитика выкл (нет FINNHUB_API_KEY)")
            elif not ticker:
                lines.append("   📈 тикер не определён (CUSIP без маппинга)")
            lines.append("")

        exit_rows = []
        for entry in exits.values():
            name = str(entry.get("ticker") or "") or str(entry.get("issuer", "?")).title()
            first = entry["actions"][0]
            exit_rows.append(f"{name} ({_fmt_fund_action(first)})")
        if exit_rows:
            lines.append("⚠️ Киты сокращают/выходят: " + "; ".join(exit_rows[:3]))
            lines.append("")

        period = universe.get("funds", [{}])[0].get("period", "?") if universe.get("funds") else "?"
        fund_names = ", ".join(f["name"] for f in universe.get("funds", []))
        built = format_cyprus_date(
            datetime.fromtimestamp(float(universe.get("built_at", now_ts)), tz=timezone.utc)
        )
        lines.append(f"13F за {period}, вселенная обновлена {built}. Фонды: {fund_names}")
        lines.append("Не инвестиционная рекомендация.")
        content = "\n".join(lines)
        IDEAS_CACHE["block"] = {"content": content, "expires_at": now_ts + max(ttl, 300)}
        return content
    except Exception as exc:
        logger.exception("ideas block failed")
        return f"Smart Money Ideas: временно недоступно ({exc})"


def build_ideas_debug_block() -> str:
    lines = ["Ideas sources probe:"]
    universe = load_ideas_universe()
    if universe:
        age_h = (datetime.now(timezone.utc).timestamp() - float(universe.get("built_at", 0))) / 3600
        lines.append(
            f"• universe: {len(universe.get('candidates', {}))} candidates, "
            f"{len(universe.get('exits', {}))} exits, age {age_h:.0f}h"
        )
    else:
        lines.append("• universe: not built yet")
    first_cik = next(iter(IDEAS_FUNDS))
    try:
        name, filings = _fund_latest_13f_filings(first_cik)
        lines.append(f"• EDGAR {name}: OK, periods {[f['period'] for f in filings]}")
    except Exception as exc:
        lines.append(f"• EDGAR FAILED {type(exc).__name__}: {str(exc)[:140]}")
    try:
        mapped = _map_cusips_to_tickers(["037833100"])  # Apple Inc
        lines.append(f"• OpenFIGI: {mapped or 'empty (check rate limit)'}")
    except Exception as exc:
        lines.append(f"• OpenFIGI FAILED {type(exc).__name__}: {str(exc)[:140]}")
    if os.getenv("FINNHUB_API_KEY", "").strip():
        try:
            lines.append(f"• Finnhub AAPL: {_fetch_finnhub_recommendation('AAPL')}")
        except Exception as exc:
            lines.append(f"• Finnhub FAILED {type(exc).__name__}: {str(exc)[:140]}")
    else:
        lines.append("• Finnhub: FINNHUB_API_KEY не задан (аналитический слой выключен)")
    return "\n".join(lines)


# ── Digest V2: source ratings ────────────────────────────────────────────────


def load_source_ratings() -> dict[str, dict]:
    try:
        with open(DIGEST_SOURCE_RATINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_source_ratings(ratings: dict[str, dict]) -> None:
    atomic_write_json(DIGEST_SOURCE_RATINGS_FILE, ratings, indent=2)


def get_source_rating_score(ratings: dict, source: str) -> float:
    entry = ratings.get(source.lower().strip(), {})
    return float(entry.get("score", 0))


def is_proven_source(ratings: dict, source: str) -> bool:
    entry = ratings.get(source.lower().strip(), {})
    return int(entry.get("count", 0)) >= 3


def update_source_rating(source: str, delta: float) -> None:
    with STATE_LOCK:
        ratings = load_source_ratings()
        key = source.lower().strip()
        entry = ratings.get(key, {"score": 0, "count": 0})
        entry["score"] = float(entry.get("score", 0)) + delta
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        ratings[key] = entry
        save_source_ratings(ratings)


def reaction_callback_data(reaction: str, source: str) -> str:
    """Build callback_data carrying the source name itself (Telegram limit: 64 bytes).

    Source names are feed/article domains, so they fit; an in-memory hash map
    would forget sources on restart and pollute ratings with hash keys.
    """
    safe_source = source.encode("utf-8")[:40].decode("utf-8", "ignore").strip() or "rss"
    return f"dr2:{reaction}:{safe_source}"


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
    except Exception as exc:
        logger.warning("restore_ratings_from_telegram failed: %s", exc)
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
        # Drop the previous backup message so pins/documents don't pile up.
        with STATE_LOCK:
            state = load_bot_state()
            prev_msg_id = state.get("ratings_backup_msg_id")
            state["ratings_backup_msg_id"] = msg.message_id
            save_bot_state(state)
        if isinstance(prev_msg_id, int) and prev_msg_id != msg.message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
            except Exception as exc:
                logger.debug("old ratings backup delete failed: %s", exc)
    except Exception as exc:
        logger.warning("backup_ratings_to_telegram failed: %s", exc)


# ── Digest V2: sent history ─────────────────────────────────────────────────


def load_sent_digests() -> list[dict]:
    try:
        with open(DIGEST_SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_sent_digests(digests: list[dict]) -> None:
    atomic_write_json(DIGEST_SENT_FILE, digests)


def record_sent_digests(items: list[dict[str, str]]) -> None:
    """Mark items as sent — call only after they were actually delivered."""
    now_ts = datetime.now(timezone.utc).timestamp()
    with STATE_LOCK:
        digests = prune_sent_digests(load_sent_digests(), now_ts)
        for item in items:
            digests.append({"fingerprint": news_item_fingerprint(item), "sent_at": now_ts})
        save_sent_digests(digests)


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
                    "- Topic priority (0-30): AI=30, Investments=27, AI Payment Agents=23, "
                    "Vibe coding tools=18, Tech=12, Business=6, Other=1\n"
                    "- Source quality (0-20): Top tier=20, Good=15, Average=10, Unknown=5\n"
                    "- Freshness (0-10): <6h=10, 6-24h=7, 24-48h=3\n\n"
                    "Return strict JSON only:\n"
                    '{"items":[{"id":0,"score":85,"category":"ai",'
                    '"headline_ru":"заголовок","details_ru":"описание 2-3 предложения"}]}\n\n'
                    "Categories: ai, investments, payments, vibecoding, tech, business, other\n"
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
        except Exception as exc:
            logger.warning("digest scoring batch %d failed: %s", start // batch_size + 1, exc)
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
                feed_items = parse_news_feed_items(xml_text, category, feed_url)
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
                    item["_ts"] = str(pub_dt.timestamp())
                    all_items.append(item)
            except Exception as exc:
                logger.warning("digest feed %s failed: %s", feed_url, exc)
                continue

    if not all_items:
        return [], "Нет свежих новостей за последние 48ч."

    all_items.sort(key=lambda x: float(x.get("_ts", "0")), reverse=True)
    if len(all_items) > DIGEST_SCORE_MAX_ITEMS:
        logger.info(
            "digest: capped %d items to %d freshest before AI scoring",
            len(all_items), DIGEST_SCORE_MAX_ITEMS,
        )
        all_items = all_items[:DIGEST_SCORE_MAX_ITEMS]
    for item in all_items:
        item.pop("_ts", None)

    all_items = score_digest_items_via_ai(all_items)

    # Add source rating bonus to AI score.
    for item in all_items:
        bonus = max(-5.0, min(get_source_rating_score(source_ratings, item.get("source", "")), 10.0))
        item["ai_score"] = str(float(item.get("ai_score", "0")) + bonus)

    selected = select_digest_items(all_items, source_ratings)

    # Sent fingerprints are recorded by the caller (record_sent_digests) only
    # for items that were actually delivered — otherwise a failed send would
    # lose those news forever.
    return selected, ""


def _format_week_delta(current: float, prev_week: float) -> str:
    """Format '(-5 vs 1wk)' with sign and arrow emoji."""
    delta = current - prev_week
    if abs(delta) < 0.5:
        arrow = "⚫"
    elif delta > 0:
        arrow = "🟢"
    else:
        arrow = "🔴"
    sign = "+" if delta >= 0 else ""
    return f"{arrow}({sign}{delta:.0f} vs 1wk)"


def _format_week_pct(current: float, week_ago: float) -> str:
    """Format '(+2.53% vs 1wk)' with sign and arrow emoji for price movers."""
    if not week_ago:
        return ""
    pct = (current - week_ago) / week_ago * 100
    if abs(pct) < 0.05:
        arrow = "⚫"
    elif pct > 0:
        arrow = "🟢"
    else:
        arrow = "🔴"
    sign = "+" if pct >= 0 else ""
    return f"{arrow}({sign}{pct:.2f}% vs 1wk)"


def build_fear_greed_block() -> str:
    state = load_bot_state()
    prev_fg = state.get("fg")
    prev_fg_dict = prev_fg if isinstance(prev_fg, dict) else {}
    next_fg: dict[str, object] = dict(prev_fg_dict)
    state_dirty = False

    score: float | None = None
    c_score: int | None = None

    try:
        score, rating, updated_at, stock_prev_week = fetch_fear_and_greed()
        stock_block = f"Stock Fear & Greed (CNN): {score:.2f} {rating} {updated_at}"
        if stock_prev_week is not None:
            stock_block += f" {_format_week_delta(score, stock_prev_week)}"
        next_fg["stock_score"] = score
        next_fg["stock_rating"] = rating
        next_fg["stock_updated_at"] = updated_at
        state_dirty = True
    except Exception as exc:
        stock_block = f"Stock Fear & Greed (CNN): ошибка ({exc})"

    try:
        c_score, c_rating, c_updated_at, crypto_prev_week = fetch_crypto_fear_and_greed()
        crypto_block = f"Crypto Fear & Greed: {c_score} {c_rating} {c_updated_at}"
        if crypto_prev_week is not None:
            crypto_block += f" {_format_week_delta(float(c_score), float(crypto_prev_week))}"
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
    except Exception as exc:
        logger.debug("fg stock trend attach skipped: %s", exc)

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
    except Exception as exc:
        logger.debug("fg crypto trend attach skipped: %s", exc)

    if state_dirty:
        next_fg["saved_at"] = datetime.now(timezone.utc).isoformat()
        with STATE_LOCK:
            state = load_bot_state()
            state["fg"] = next_fg
            save_bot_state(state)

    return f"{stock_block}\n{crypto_block}"


def build_st_block() -> str:
    state = load_bot_state()
    prev_st = state.get("st")
    prev_st_dict = prev_st if isinstance(prev_st, dict) else {}
    try:
        btc_price, spx_price, spx_futures = fetch_market_prices()
        btc_line = f"Bitcoin (BTC-USD): ${btc_price:,.2f}"
        spx_line = f"S&P 500 (^GSPC): {spx_price:,.2f}"
        if spx_futures is not None:
            pct = (spx_futures - spx_price) / spx_price * 100
            sign = "+" if pct >= 0 else ""
            futures_line = f"\nS&P 500 Futures (ES=F): {spx_futures:,.2f} ({sign}{pct:.2f}% vs cash)"
        else:
            # Surface the failure instead of silently dropping the line: the
            # futures price is the pre-/after-market signal for S&P 500.
            futures_line = "\nS&P 500 Futures (ES=F): n/a (источники недоступны, детали: /st debug)"

        btc_wk, spx_wk = _fetch_week_ago_prices()
        if btc_wk:
            btc_line = f"{btc_line} {_format_week_pct(btc_price, btc_wk)}"
        if spx_wk:
            spx_line = f"{spx_line} {_format_week_pct(spx_price, spx_wk)}"
        next_st: dict[str, object] = dict(prev_st_dict)
        next_st["btc_price"] = btc_price
        next_st["spx_price"] = spx_price
        next_st["saved_at"] = datetime.now(timezone.utc).isoformat()
        with STATE_LOCK:
            state = load_bot_state()
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
        except Exception as exc:
            logger.debug("st trend attach skipped: %s", exc)
        return f"{btc_line}\n{spx_line}{futures_line}"
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
    result = _try_source("fragmata", fetch_cyprus_reservoirs_fragmata)
    if result is not None:
        return result
    # Legacy WDD xlsx scrape — dead since the gov.cy migration, kept as a
    # cheap last resort in case Fragmata goes away and the page comes back.
    try:
        return fetch_cyprus_reservoirs_wdd_xlsx()
    except Exception as exc:
        return f"Cyprus reservoirs: временно недоступно ({exc})"


def build_dam_debug_block() -> str:
    """Probe candidate WDD data sources and report raw HTTP diagnostics.

    /dam relies on scraping; when the site moves, this probe (run from the
    deployment, which has open egress) shows which URL still serves usable data.
    """
    lines = [f"Cyprus reservoirs probe ({len(DAM_DEBUG_PROBE_URLS)} URLs):"]
    for url in DAM_DEBUG_PROBE_URLS:
        try:
            response = requests.get(
                url, headers=REQUEST_HEADERS_GENERIC, timeout=HTTP_TIMEOUT_SHORT
            )
            ctype = response.headers.get("Content-Type", "?").split(";")[0].strip()
            body = ""
            if any(t in ctype for t in ("text", "json", "xml")):
                body = response.text
            xlsx_refs = len(re.findall(r"\.xlsx", body, flags=re.I))
            note = f"xlsx_refs={xlsx_refs}"
            if "json" in ctype and body:
                note = f"body: {normalize_text(body)[:180]}"
            final_url = ""
            if response.url.rstrip("/") != url.rstrip("/"):
                final_url = f"\n  -> {response.url}"
            lines.append(
                f"• {url}{final_url}\n"
                f"  {response.status_code} {ctype} {len(response.content)}B {note}"
            )
        except Exception as exc:
            lines.append(f"• {url}\n  FAILED {type(exc).__name__}: {exc}")
    lines.append("")
    lines.append("Пришлите этот вывод — по нему выбирается рабочий источник для /dam.")
    return "\n".join(lines)


def get_target_chat_id() -> int | None:
    raw = os.getenv("TELEGRAM_TARGET_CHAT_ID", "").strip()
    if not raw:
        return None
    return int(raw)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        with_version(
            "Привет! Я показываю Fear & Greed Index.\n"
            "Команды:\n"
            "/fg - Fear & Greed блок\n"
            "/st - Bitcoin и S&P\n"
            "/fx - валюты\n"
            "/dam - Cyprus reservoirs\n"
            "/news - новостной дайджест (v1)\n"
            "/hot - самые обсуждаемые новости (по охвату в СМИ)\n"
            "/digest - tech-дайджест с ИИ-скорингом\n"
            "/ideas - Smart Money: покупки фондов (13F) + strong buy аналитиков\n"
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
    if context.args and context.args[0].strip().lower() in {"debug", "dbg", "probe"}:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, build_st_debug_block)
        except Exception as exc:
            logger.exception("st debug probe crashed")
            text = f"Market sources probe: ошибка ({exc})"
        await reply_long_text(update, with_version(text))
        return
    text, html_mode = await render_block_async(block_name="st", include_version=True)
    await send_rendered_update(update, text, html_mode)


async def dam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    if context.args and context.args[0].strip().lower() in {"debug", "dbg", "probe"}:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, build_dam_debug_block)
        except Exception as exc:
            logger.exception("dam debug probe crashed")
            text = f"Cyprus reservoirs probe: ошибка ({exc})"
        await reply_long_text(update, with_version(text))
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


async def hot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    force_refresh = bool(
        context.args and context.args[0].strip().lower() in {"refresh", "r", "now", "new"}
    )
    text, html_mode = await render_block_async(
        block_name="hot",
        include_version=True,
        force_refresh=force_refresh,
        news_spoilers=True,
    )
    await send_rendered_update(update, text, html_mode)


async def ideas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    arg = context.args[0].strip().lower() if context.args else ""
    loop = asyncio.get_running_loop()
    if arg in {"debug", "dbg", "probe"}:
        try:
            text = await loop.run_in_executor(None, build_ideas_debug_block)
        except Exception as exc:
            logger.exception("ideas debug probe crashed")
            text = f"Ideas sources probe: ошибка ({exc})"
        await reply_long_text(update, with_version(text))
        return
    message = update.effective_message
    wait_msg = None
    if message is not None and arg in {"rebuild", "refresh", "r"}:
        wait_msg = await message.reply_text("⏳ Собираю Smart Money Ideas...")
    text = await loop.run_in_executor(
        None,
        partial(
            build_ideas_block,
            force_refresh=arg in {"rebuild", "refresh", "r"},
            rebuild=arg == "rebuild",
        ),
    )
    if wait_msg is not None:
        try:
            await wait_msg.delete()
        except Exception as exc:
            logger.debug("ideas wait message delete failed: %s", exc)
    await reply_long_text(update, with_version(text))


async def all_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    block_order = ["fg", "st", "fx", "dam", "news", "ideas"]
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
    uptime = datetime.now(timezone.utc) - BOT_STARTED_AT
    uptime_str = f"{uptime.days}d {uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m"
    await update.effective_message.reply_text(
        with_version(
            "Статус бота:\n"
            f"- Host: {socket.gethostname()}\n"
            f"- OS: {platform.platform(terse=True)}\n"
            f"- Hosting: {detect_hosting()}\n"
            f"- Started: {format_cyprus_time(BOT_STARTED_AT)} (up {uptime_str})\n"
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
    block_order = ["fg", "st", "fx", "dam", "news", "ideas"]
    for idx, block_name in enumerate(block_order):
        text, html_mode = await render_block_async(
            block_name=block_name,
            include_version=(idx == 0),
            news_spoilers=True,
        )
        await send_rendered_chat(context, chat_id, text, html_mode)


async def _reply_digest_item(message, text: str, keyboard: InlineKeyboardMarkup) -> bool:
    """Send one digest message, waiting out Telegram flood limits. Returns success."""
    for _ in range(3):
        try:
            await message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            return True
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except Exception as exc:
            logger.warning("digest item send failed: %s", exc)
            return False
    logger.warning("digest item send failed: flood limit persisted after retries")
    return False


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return
    message = update.effective_message
    if message is None:
        return
    msg = await message.reply_text("⏳ Собираю дайджест...")

    loop = asyncio.get_running_loop()
    selected, error = await loop.run_in_executor(None, build_digest_v2)

    if error:
        await msg.edit_text(with_version(error))
        return

    await msg.delete()

    sent_items: list[dict[str, str]] = []
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

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔥", callback_data=reaction_callback_data("fire", source)),
                    InlineKeyboardButton("👍", callback_data=reaction_callback_data("like", source)),
                    InlineKeyboardButton("👎", callback_data=reaction_callback_data("dislike", source)),
                    InlineKeyboardButton("💩", callback_data=reaction_callback_data("poop", source)),
                ]
            ]
        )

        if await _reply_digest_item(message, text, keyboard):
            sent_items.append(item)

    if sent_items:
        await loop.run_in_executor(None, record_sent_digests, sent_items)

    summary = f"Дайджест: {len(sent_items)} новостей"
    failed_count = len(selected) - len(sent_items)
    if failed_count:
        summary += f" ({failed_count} не отправлено)"
    await message.reply_text(
        with_version(f"{summary} | {format_cyprus_time(datetime.now(timezone.utc))}")
    )


async def _ratings_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await backup_ratings_to_telegram(context.bot)


async def digest_reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not _check_access(update):
        await query.answer("⛔")
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] not in {"dr", "dr2"}:
        return

    tag, reaction, ref = parts
    delta = DIGEST_REACTION_DELTAS.get(reaction)
    if delta is None:
        await query.answer("?")
        return

    if tag == "dr":
        # Legacy buttons referenced an in-memory hash map that didn't survive
        # restarts; rating by hash would pollute source_ratings.json.
        await query.answer("⚠️ Кнопка устарела (бот перезапускался)")
        return

    source_name = ref
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, update_source_rating, source_name, delta)

    icons = {"fire": "🔥", "like": "👍", "dislike": "👎", "poop": "💩"}
    await query.answer(f"{icons.get(reaction, '✓')} {source_name}")

    # Backup ratings to Telegram so they survive redeploys. Debounce via
    # job_queue so a burst of reactions produces one upload, not N pins.
    job_queue = context.application.job_queue
    if job_queue is not None:
        for job in job_queue.get_jobs_by_name("ratings_backup"):
            job.schedule_removal()
        job_queue.run_once(_ratings_backup_job, when=60, name="ratings_backup")
    else:
        await backup_ratings_to_telegram(context.bot)


async def on_startup(app) -> None:
    global SCHEDULER_STATUS
    # Restore source ratings from Telegram backup.
    restored = await restore_ratings_from_telegram(app.bot)
    if restored:
        ratings = load_source_ratings()
        count = len(ratings)
        logger.info("digest: restored %d source ratings from Telegram backup", count)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "помощь"),
            BotCommand("fg", "Fear & Greed блок"),
            BotCommand("st", "Bitcoin и S&P"),
            BotCommand("fx", "валюты"),
            BotCommand("dam", "Cyprus reservoirs"),
            BotCommand("news", "новостной дайджест (v1)"),
            BotCommand("hot", "самые обсуждаемые новости"),
            BotCommand("digest", "tech-дайджест с ИИ-скорингом"),
            BotCommand("ideas", "Smart Money: 13F + strong buy"),
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
        except Exception as exc:
            logger.warning("deploy notification failed: %s", exc)


def _check_access(update: Update) -> bool:
    if ALLOWED_USER_ID is None:
        return True
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def main() -> None:
    global ALLOWED_USER_ID
    load_dotenv()
    token = get_token()
    raw_uid = os.getenv("ALLOWED_USER_ID", "").strip()
    if raw_uid:
        ALLOWED_USER_ID = int(raw_uid)
    else:
        logger.warning(
            "ALLOWED_USER_ID is not set — bot commands (including OpenAI-backed "
            "/digest and /news) are open to ANY Telegram user"
        )

    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("fg", fg))
    app.add_handler(CommandHandler("st", st))
    app.add_handler(CommandHandler("fx", fx))
    app.add_handler(CommandHandler("dam", dam))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("hot", hot))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CommandHandler("ideas", ideas))
    app.add_handler(CommandHandler("all", all_report))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(digest_reaction_callback, pattern=r"^dr2?:"))

    app.run_polling()


if __name__ == "__main__":
    main()
