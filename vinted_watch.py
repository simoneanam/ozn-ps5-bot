import asyncio
import html
import os
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
from curl_cffi import AsyncSession


# ============================================================================
# CONFIG
# ============================================================================

VINTED_BASE_URL = "https://www.vinted.it"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEARCH_TEXT = os.getenv("VINTED_SEARCH_TEXT", "ps5")

PRICE_FROM = 300.0
PRICE_TO = 450.0

REQUIRE_PHOTO = os.getenv("VINTED_REQUIRE_PHOTO", "1") != "0"
# Euristiche sul testo disponibile, non una verifica del venditore.
EXCLUDED_TERMS = tuple(term.strip().casefold() for term in (
    os.getenv("VINTED_EXCLUDED_TERMS") or
    "solo scatola,scatola vuota,solo confezione,box only,empty box,"
    "non funzionante,non funziona,guasta,guasto,rotta,rotto,"
    "da riparare,per ricambi,for parts,not working,"
    "solo account,account digitale,whatsapp,telegram,"
    "pagamento esterno,fuori vinted,bonifico,ricarica postepay,"
    "paypal amici,paypal friends"
).split(",") if term.strip())

PER_PAGE = 20


# ============================================================================
# PERSISTENCE (Upstash REST, using the existing HTTP dependency)
# ============================================================================

class SeenStore:
    def __init__(self, client, url, token, prefix, retention_days):
        self.client = client
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.key = f"{prefix}:seen"
        self.initialized_key = f"{prefix}:initialized"
        self.retention_seconds = retention_days * 86400

    async def command(self, *args):
        response = await self.client.post(
            self.url, headers=self.headers, json=list(args)
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            # Never log raw responses: they can contain credentials or input.
            raise RuntimeError("Risposta Redis non valida o comando rifiutato")
        return data["result"]

    async def initialized(self):
        value = await self.command("GET", self.initialized_key)
        if value not in (None, "1"):
            raise RuntimeError("Stato inizializzazione Redis non valido")
        return value == "1"

    async def mark_seen(self, item_ids):
        if item_ids:
            now = int(time.time())
            pairs = [value for item_id in item_ids for value in (now, item_id)]
            await self.command("ZADD", self.key, *pairs)

    async def initialize(self, item_ids):
        await self.mark_seen(item_ids)
        # Marker written LAST. A failed baseline is safely repeated without sends.
        # Separate marker also handles a first successful scan with zero results.
        await self.command("SET", self.initialized_key, "1")

    async def known_ids(self, item_ids):
        if not item_ids:
            return set()
        scores = await self.command("ZMSCORE", self.key, *item_ids)
        if not isinstance(scores, list) or len(scores) != len(item_ids):
            raise RuntimeError("Risposta Redis ZMSCORE non valida")
        return {item_id for item_id, score in zip(item_ids, scores) if score is not None}

    async def prune(self):
        cutoff = int(time.time()) - self.retention_seconds
        await self.command("ZREMRANGEBYSCORE", self.key, "-inf", f"({cutoff}")


# ============================================================================
# TELEGRAM
# ============================================================================

async def telegram_call(
    client: httpx.AsyncClient,
    method: str,
    *,
    json=None,
    params=None,
):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    if json is not None:
        response = await client.post(
            url,
            json=json,
        )
    else:
        response = await client.get(
            url,
            params=params,
        )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError("Telegram ha rifiutato il messaggio")

    return data["result"]


# ============================================================================
# VINTED DATA HELPERS
# ============================================================================

def get_item_id(item: dict) -> str:
    item_id = item.get("id")

    if item_id is None:
        raise RuntimeError(
            f"Annuncio senza id: {item}"
        )

    return str(item_id)


def get_item_title(item: dict) -> str:
    return (
        item.get("title")
        or item.get("name")
        or "Annuncio Vinted"
    )


def get_item_url(item: dict) -> str:
    url = item.get("url")

    if url:
        if url.startswith("http"):
            return url

        if url.startswith("/"):
            return (
                f"{VINTED_BASE_URL}{url}"
            )

    item_id = get_item_id(item)

    return (
        f"{VINTED_BASE_URL}/items/{item_id}"
    )


def get_item_price(item: dict) -> str:
    price = item.get("price")

    if isinstance(price, dict):
        amount = (
            price.get("amount")
            or price.get("value")
            or "?"
        )

        currency = (
            price.get("currency_code")
            or price.get("currency")
            or "EUR"
        )

        if currency == "EUR":
            currency = "€"

        return f"{amount} {currency}"

    if price is not None:
        currency = (
            item.get("currency")
            or "EUR"
        )

        if currency == "EUR":
            currency = "€"

        return f"{price} {currency}"

    return "Prezzo non disponibile"


def get_photo_url(
    item: dict,
) -> str | None:

    photo = item.get("photo")

    if isinstance(photo, dict):
        return (
            photo.get("url")
            or photo.get("full_size_url")
            or photo.get("high_resolution", {}).get("url")
        )

    photos = item.get("photos")

    if (
        isinstance(photos, list)
        and photos
        and isinstance(photos[0], dict)
    ):
        return (
            photos[0].get("url")
            or photos[0].get(
                "full_size_url"
            )
        )

    return None


# ============================================================================
# TELEGRAM MESSAGE
# ============================================================================

def rejection_reason(item: dict) -> str | None:
    price = item.get("price")
    if isinstance(price, dict):
        amount = price.get("amount", price.get("value"))
        currency = price.get("currency_code") or price.get("currency")
    else:
        amount = price
        currency = item.get("currency")
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return "prezzo mancante o non valido"
    if not amount.is_finite() or not PRICE_FROM <= amount <= PRICE_TO:
        return "prezzo fuori intervallo o non valido"
    if currency != "EUR":
        return "valuta mancante o diversa da EUR"
    if REQUIRE_PHOTO and not get_photo_url(item):
        return "foto mancante"
    text = " ".join(re.findall(
        r"\w+", f"{get_item_title(item)} {item.get('description') or ''}".casefold()
    ))
    for term in EXCLUDED_TERMS:
        normalized = " ".join(re.findall(r"\w+", term))
        if normalized and f" {normalized} " in f" {text} ":
            return f"testo da verificare: {term}"
    return None


async def send_item(
    client: httpx.AsyncClient,
    chat_id: str,
    item: dict,
):
    title = get_item_title(item)
    price = get_item_price(item)
    url = get_item_url(item)
    photo = get_photo_url(item)

    text = (
        "🔥 <b>Nuova PS5 su Vinted</b>\n\n"
        f"🎮 <b>{html.escape(title)}</b>\n"
        f"💰 <b>{html.escape(price)}</b>\n\n"
        f'👉 <a href="{html.escape(url)}">'
        "Apri su Vinted"
        "</a>"
    )

    if photo:
        try:
            await telegram_call(
                client,
                "sendPhoto",
                json={
                    "chat_id": chat_id,
                    "photo": photo,
                    "caption": text,
                    "parse_mode": "HTML",
                },
            )

            return

        except httpx.HTTPStatusError as exc:
            # Only a definite rejection permits fallback. A timeout or 5xx
            # may occur AFTER delivery; sending text then could duplicate it.
            if exc.response.status_code != 400:
                raise
            print("Telegram ha rifiutato la foto (HTTP 400), provo il testo.")

    await telegram_call(
        client,
        "sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {
                "is_disabled": False,
            },
        },
    )


# ============================================================================
# VINTED SEARCH
# ============================================================================

def build_search_url() -> str:
    params = {
        "search_text": SEARCH_TEXT,
        "price_from": PRICE_FROM,
        "price_to": PRICE_TO,
        "currency": "EUR",
        "order": "newest_first",
    }

    return (
        f"{VINTED_BASE_URL}/catalog?"
        f"{urlencode(params)}"
    )


def create_vinted_client() -> AsyncSession:
    return AsyncSession(
        impersonate="chrome",
        timeout=20,
        headers={
            "Accept": "application/json",
            "Accept-Language": "it-IT,it;q=0.9",
            "Locale": "it-IT",
            "Platform": "web",
            "Referer": VINTED_BASE_URL,
            "X-Money-Object": "true",
        },
    )


async def search_vinted(client: AsyncSession) -> list[dict]:
    # Il catalogo usa il gateway; /api/v2/catalog/items restituisce 404.
    params = {
        "search_text": SEARCH_TEXT,
        "price_from": PRICE_FROM,
        "price_to": PRICE_TO,
        "currency": "EUR",
        "order": "newest_first",
        "page": 1,
        "per_page": PER_PAGE,
        "time": int(time.time()),
    }

    if not client.cookies:
        bootstrap = await client.head(VINTED_BASE_URL)
        bootstrap.raise_for_status()

    for attempt in range(2):
        response = await client.get(
            f"{VINTED_BASE_URL}/web/gateway/svc-catalogue/items",
            params=params,
        )
        if response.status_code in (401, 403) and attempt == 0:
            client.cookies.clear()
            bootstrap = await client.head(VINTED_BASE_URL)
            bootstrap.raise_for_status()
            continue
        response.raise_for_status()
        break

    data = response.json()
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or item.get("id") is None for item in items
    ):
        raise RuntimeError(
            "Risposta Vinted inattesa: manca una lista valida di annunci."
        )

    return items


# ============================================================================
# PROCESS ITEMS
# ============================================================================

async def process_items(
    telegram_client: httpx.AsyncClient,
    chat_id: str,
    store: SeenStore,
    items: list[dict],
):
    # Deduplicate the response too; preserve Vinted's newest-first ordering.
    by_id = {get_item_id(item): item for item in items}
    item_ids = list(by_id)
    if not await store.initialized():
        await store.initialize(item_ids)
        print(f"Primo avvio: {len(item_ids)} annunci registrati, nessuna notifica.")
        return

    known = await store.known_ids(item_ids)
    # Refresh BEFORE pruning: listings still visible must not expire.
    await store.mark_seen(sorted(known))
    await store.prune()
    sent = 0
    for item_id in reversed(item_ids):
        if item_id in known:
            continue
        item = by_id[item_id]
        reason = rejection_reason(item)
        if reason:
            # Recheck rejected listings on later runs if the seller edits them.
            print(f"Scartato annuncio {item_id}: filtri non soddisfatti.")
            continue
        await send_item(telegram_client, chat_id, item)
        # Never record success before Telegram confirms delivery.
        await store.mark_seen([item_id])
        sent += 1
        print(f"Notifica inviata e registrata: {item_id}")
    print(f"Scansione completata: {sent} notifiche nuove.")


# ============================================================================
# MAIN: one scan, then exit (nonzero on failure)
# ============================================================================

async def notify_error(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Avviso errore non inviabile: configurazione Telegram mancante.")
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await telegram_call(client, "sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "⚠️ Vinted watcher: " + message,
            })
        print("Avviso errore inviato su Telegram.")
    except Exception:
        print("Invio avviso errore fallito: controllare i log del job.")


async def main():
    global PRICE_FROM, PRICE_TO, PER_PAGE
    required = (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        message = "Configurazione mancante: " + ", ".join(missing)
        print(message)
        await notify_error(message)
        return 1

    phase = "configurazione"
    try:
        PRICE_FROM = float(os.getenv("VINTED_PRICE_FROM", "300"))
        PRICE_TO = float(os.getenv("VINTED_PRICE_TO", "450"))
        PER_PAGE = int(os.getenv("VINTED_PER_PAGE", "20"))
        retention_days = int(os.getenv("VINTED_SEEN_RETENTION_DAYS", "30"))
        if not (0 <= PRICE_FROM <= PRICE_TO < float("inf")):
            raise ValueError("Intervallo prezzi non valido")
        if retention_days <= 0 or PER_PAGE <= 0:
            raise ValueError("Retention e dimensione pagina devono essere positive")
        redis_url = os.environ["UPSTASH_REDIS_REST_URL"]
        if not redis_url.startswith("https://"):
            raise ValueError("Upstash richiede un URL HTTPS")
        async with httpx.AsyncClient(timeout=20) as client:
            store = SeenStore(
                client, redis_url, os.environ["UPSTASH_REDIS_REST_TOKEN"],
                os.getenv("VINTED_REDIS_PREFIX", "vinted:ps5"), retention_days,
            )
            phase = "Vinted"
            print("Avvio scansione singola Vinted.")
            async with create_vinted_client() as vinted_client:
                items = await search_vinted(vinted_client)
            print(f"Vinted: {len(items)} annunci ricevuti.")
            phase = "Redis/Telegram"
            await process_items(client, TELEGRAM_CHAT_ID, store, items)
        return 0
    except Exception as exc:
        # Exception strings/tracebacks can expose the Telegram token in URLs.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f", HTTP {status}" if isinstance(status, int) else ""
        message = (f"ERRORE {phase}: {type(exc).__name__}{detail}. "
                   "Esecuzione interrotta; nuovo tentativo alla prossima scansione.")
        print(message)
        await notify_error(message)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nWatcher interrotto.")
        raise SystemExit(130)
