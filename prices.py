"""
prices.py - Fetches live prices from free APIs (no API keys needed).

- Crypto (BTC, ETH, ...) comes from Binance's public market-data API.
  Prices are the latest trade on Binance's USDT pairs (e.g. BTCUSDT), so they
  are effectively real-time. USDT is a dollar stablecoin and trades within a
  few cents of $1, so BTCUSDT is a close stand-in for BTC/USD.
  If Binance can't be reached, we fall back to CoinGecko (about 1 minute behind).
- Metals (XAUUSD = gold, XAGUSD = silver) come from gold-api.com, which says
  it refreshes spot prices every 30 seconds. Spot metals don't trade on
  weekends, so the price stays flat from Friday night to Sunday night (UTC).
"""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

# Map the symbol a user types -> (Binance trading pair, CoinGecko coin ID).
CRYPTO_IDS = {
    "BTC": ("BTCUSDT", "bitcoin"),
    "ETH": ("ETHUSDT", "ethereum"),
    "SOL": ("SOLUSDT", "solana"),
    "XRP": ("XRPUSDT", "ripple"),
    "DOGE": ("DOGEUSDT", "dogecoin"),
    "BNB": ("BNBUSDT", "binancecoin"),
    "ADA": ("ADAUSDT", "cardano"),
}

# Map the symbol a user types -> the metal code gold-api.com uses.
METAL_IDS = {
    "XAUUSD": "XAU",  # gold
    "XAGUSD": "XAG",  # silver
}

SUPPORTED_SYMBOLS = list(CRYPTO_IDS) + list(METAL_IDS)

# Binance's official market-data-only host comes first; the main API host is
# blocked on some networks, so it's only a second choice.
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


def is_supported(symbol):
    """True if we know how to fetch a price for this symbol."""
    return symbol in CRYPTO_IDS or symbol in METAL_IDS


async def _crypto_from_binance(client, coins):
    """Latest Binance trade price for each coin, in one request."""
    pairs = {CRYPTO_IDS[s][0]: s for s in coins}  # "BTCUSDT" -> "BTC"
    for host in BINANCE_HOSTS:
        try:
            resp = await client.get(
                f"{host}/api/v3/ticker/price",
                # Binance wants the list as JSON text: ["BTCUSDT","ETHUSDT"]
                params={"symbols": json.dumps(list(pairs), separators=(",", ":"))},
            )
            resp.raise_for_status()
            return {pairs[item["symbol"]]: float(item["price"]) for item in resp.json()}
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning("Binance (%s) error: %s", host, e)
    return {}


async def _crypto_from_coingecko(client, coins):
    """Backup source: CoinGecko's average across exchanges (~1 minute delayed)."""
    ids = {CRYPTO_IDS[s][1]: s for s in coins}  # "bitcoin" -> "BTC"
    try:
        resp = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        return {ids[coin_id]: float(p["usd"]) for coin_id, p in resp.json().items()}
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("CoinGecko error: %s", e)
        return {}


async def get_prices(symbols):
    """
    Fetch current USD prices for several symbols at once.
    Returns a dict like {"BTC": 80615.56, "XAUUSD": 3996.9}.
    Symbols whose price could not be fetched are simply left out.
    """
    prices = {}
    async with httpx.AsyncClient(timeout=10) as client:
        # --- Crypto: Binance first, CoinGecko for anything still missing ---
        coins = [s for s in symbols if s in CRYPTO_IDS]
        if coins:
            prices.update(await _crypto_from_binance(client, coins))
            missing = [s for s in coins if s not in prices]
            if missing:
                logger.warning("Using CoinGecko backup for %s", missing)
                prices.update(await _crypto_from_coingecko(client, missing))

        # --- Metals: one request per metal ---
        for s in symbols:
            if s in METAL_IDS:
                try:
                    resp = await client.get(f"https://api.gold-api.com/price/{METAL_IDS[s]}")
                    resp.raise_for_status()
                    prices[s] = float(resp.json()["price"])
                except (httpx.HTTPError, KeyError, ValueError) as e:
                    logger.warning("gold-api error for %s: %s", s, e)

    return prices


async def get_price(symbol):
    """Convenience helper: fetch a single price, or None if unavailable."""
    return (await get_prices([symbol])).get(symbol)
