"""
Price Feed
----------
Fetches current token prices from GeckoTerminal for open positions.
Uses /networks/eth/tokens/{address}/pools endpoint — returns the
highest liquidity pool's current price.

Rate limit: 30 calls/minute — enforced via semaphore with 2s delay.
"""

import asyncio
import aiohttp
import logging

log = logging.getLogger(__name__)

GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
GT_HEADERS    = {
    "Accept":     "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
GT_DELAY_SECS = 2.0


class PriceFeed:
    def __init__(self):
        self._cache: dict[str, float] = {}  # address -> last known price

    async def get_prices(self, token_addresses: list[str]) -> dict[str, float]:
        """
        Fetch current USD prices for a list of token addresses.
        Returns dict of address -> price_usd.
        Missing tokens return last cached price or 0.
        """
        if not token_addresses:
            return {}

        semaphore = asyncio.Semaphore(1)
        results   = {}

        async with aiohttp.ClientSession(headers=GT_HEADERS) as session:
            tasks = [
                self._fetch_price(session, addr, semaphore)
                for addr in token_addresses
            ]
            fetched = await asyncio.gather(*tasks, return_exceptions=True)

        for addr, price in zip(token_addresses, fetched):
            if isinstance(price, Exception) or price is None:
                # Fall back to last cached price
                results[addr] = self._cache.get(addr, 0.0)
            else:
                self._cache[addr] = price
                results[addr]     = price

        return results

    async def get_price(self, token_address: str) -> float:
        """Fetch current price for a single token."""
        prices = await self.get_prices([token_address])
        return prices.get(token_address, 0.0)

    async def _fetch_price(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        semaphore: asyncio.Semaphore,
    ) -> float | None:
        await semaphore.acquire()
        try:
            await asyncio.sleep(GT_DELAY_SECS)
            url    = f"{GECKOTERMINAL}/networks/eth/tokens/{token_address}/pools"
            params = {"page": 1}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug(f"PriceFeed HTTP {resp.status} for {token_address}")
                    return None
                data  = await resp.json()
                pools = data.get("data", [])
                if not pools:
                    return None
                # Use highest liquidity pool
                best  = max(
                    pools,
                    key=lambda p: float(p.get("attributes", {}).get("reserve_in_usd", 0) or 0)
                )
                price = best.get("attributes", {}).get("base_token_price_usd")
                if price is None:
                    return None
                return float(price)
        except Exception as e:
            log.debug(f"PriceFeed error for {token_address}: {e}")
            return None
        finally:
            await asyncio.sleep(GT_DELAY_SECS)
            semaphore.release()
