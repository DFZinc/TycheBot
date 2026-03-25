"""
TycheBot — Sim Trade Agent
--------------------------
Simulates copy-trading wallets on the watchlist.

Key design:
- Startup timestamp cutoff: any tx older than startup is IGNORED,
  regardless of hash state. This is bulletproof against restart loops.
- Balance check before buy scan each cycle — no spam when balance full.
- BaseException handling — catches asyncio.CancelledError cleanly.
"""

import asyncio
import aiohttp
import logging
import traceback
import time
from datetime import datetime, timezone

from sim_config       import SimConfig
from sim_portfolio    import SimPortfolio
from sim_watchlist    import SimWatchlist
from position_tracker import PositionTracker
from price_feed       import PriceFeed
from tp_sl_engine     import TpSlEngine
from trade_history    import TradeHistory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

ETHERSCAN   = "https://api.etherscan.io/v2/api"
COINGECKO   = "https://api.coingecko.com/api/v3"

def _load_api_key() -> str:
    """Load API key from agent_settings.json — no more hardcoding."""
    try:
        import json
        from pathlib import Path
        path = Path(__file__).parent / "agent_settings.json"
        if path.exists():
            data = json.loads(path.read_text())
            key = data.get("api_key", "").strip()
            if key:
                return key
    except Exception:
        pass
    return ""

API_KEY = _load_api_key()  # loaded at startup; restart agent after saving a new key
CHAIN_ID    = 1
WEI_TO_ETH  = 1e18

WALLET_COLORS = ["#39d0d8", "#3fb950", "#bc8cff", "#e3b341", "#f85149"]

EXCLUDED = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

SWAP_GAS_UNITS = 150_000


class SimTradeAgent:
    def __init__(self):
        self.config    = SimConfig()
        self.portfolio = SimPortfolio(
            starting_balance_eth=self.config.get("starting_balance_eth")
        )
        self.watchlist  = SimWatchlist()
        self.positions  = PositionTracker()
        self.price_feed = PriceFeed()
        self.tp_sl      = TpSlEngine()
        self.history    = TradeHistory()
        self._eth_price  = 0.0
        self._cycle_count = 0
        # Timestamp cutoff: ignore all txs with timestamp <= startup time.
        # This is the primary guard against reprocessing historical txs.
        self._startup_ts = int(time.time())

    async def run(self):
        log.info("TycheBot started.")
        log.info(
            f"Balance: {self.config.get('starting_balance_eth')} ETH | "
            f"Trade: {self.config.get('trade_size_eth')} ETH | "
            f"TP: {self.config.get('take_profit_pct')}% | "
            f"SL: {self.config.get('stop_loss_pct')}%"
        )
        log.info(f"Watching {self.watchlist.count()} wallet(s) | Cutoff: {datetime.fromtimestamp(self._startup_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC")
        log.info("Monitoring for new trades from now.")

        while True:
            try:
                await self._cycle()
                self._cycle_count += 1
                if self._cycle_count % 10 == 0:
                    log.info(
                        f"Heartbeat #{self._cycle_count} | "
                        f"{len(self.positions.get_open_positions())} open | "
                        f"Available: {self.portfolio.available_eth:.6f} ETH | "
                        f"ETH: ${self._eth_price:,.2f}"
                    )
                # Sleep is inside try/except so CancelledError cannot escape
                await asyncio.sleep(int(self.config.get("poll_interval_seconds")))
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                log.info("Agent stopping.")
                break
            except BaseException as e:
                msg = f"CYCLE ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                log.error(msg)
                print(msg, flush=True)
                # Write error to file so it persists
                try:
                    with open("agent_error.log", "a") as f:
                        f.write(msg + "\n---\n")
                except Exception:
                    pass
                await asyncio.sleep(10)  # brief pause after error before retrying

    async def _cycle(self):
        # Fetch ETH price first
        self._eth_price = await self._fetch_eth_price()

        # Reload portfolio and positions from disk AFTER fetching price
        # but before anything that writes — picks up manual buys from dashboard
        self.positions = PositionTracker()
        self.portfolio = SimPortfolio(self.config.get("starting_balance_eth"))
        self.history   = TradeHistory()

        if self._eth_price > 0:
            self.portfolio.update_eth_price(self._eth_price)

        wallets = self.watchlist.get_all()
        if not wallets:
            return

        for i, w in enumerate(wallets):
            w["_color"] = WALLET_COLORS[i % len(WALLET_COLORS)]

        trade_size_eth = self.config.get("trade_size_eth")
        can_open_new   = self.portfolio.available_eth >= (trade_size_eth + 0.005)

        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            for wallet in wallets:
                await self._process_wallet(session, wallet, can_open_new)

        # Update prices on open positions
        open_positions = self.positions.get_open_positions()
        if not open_positions:
            return

        prices = await self.price_feed.get_prices(
            list({p["token_address"] for p in open_positions})
        )
        for pos in open_positions:
            price = prices.get(pos["token_address"], 0.0)
            if price > 0 and self._eth_price > 0:
                self.positions.update_price(pos["id"], price, self._eth_price)

        # Check TP/SL
        tp = self.config.get("take_profit_pct")
        sl = self.config.get("stop_loss_pct")
        for pos_id, reason in self.tp_sl.check(self.positions.get_open_positions(), tp, sl):
            await self._close_position(pos_id, reason)

        self.portfolio.update_unrealized(self.positions.total_unrealized_pnl_eth())

    async def _process_wallet(self, session, wallet, can_open_new):
        addr  = wallet["address"]
        label = wallet["label"]
        color = wallet.get("_color", "#ffffff")

        try:
            params = {
                "chainid": CHAIN_ID,
                "module":  "account",
                "action":  "tokentx",
                "address": addr,
                "page":    1,
                "offset":  50,
                "sort":    "desc",
                "apikey":  API_KEY,
            }
            async with session.get(
                ETHERSCAN, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                txs  = data.get("result", [])
                if not isinstance(txs, list) or not txs:
                    return

            # PRIMARY GUARD: only process txs with timestamp > startup time.
            # This is bulletproof — works even if last_seen_hash is missing or stale.
            new_txs = [
                tx for tx in txs
                if int(tx.get("timeStamp", 0)) > self._startup_ts
            ]

            if not new_txs:
                return  # Nothing new since startup

            # Identify router/contract addresses by frequency
            from_counts: dict[str, int] = {}
            for tx in txs:
                f = tx.get("from", "").lower()
                from_counts[f] = from_counts.get(f, 0) + 1
            contract_addrs = {a for a, c in from_counts.items() if c >= 3}

            for tx in new_txs:
                token_addr   = tx.get("contractAddress", "").lower()
                token_symbol = tx.get("tokenSymbol", "?")
                from_addr    = tx.get("from", "").lower()
                to_addr      = tx.get("to", "").lower()

                if token_addr in EXCLUDED:
                    continue

                is_buy  = (to_addr == addr and from_addr in contract_addrs)
                is_sell = (from_addr == addr and to_addr in contract_addrs)

                if is_buy and can_open_new:
                    await self._on_buy(addr, label, color, token_addr, token_symbol)
                elif is_sell:
                    await self._on_sell(addr, label, token_addr, token_symbol)

        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            log.error(f"Wallet error {addr}: {e}")

    async def _on_buy(self, wallet_addr, wallet_label, wallet_color, token_addr, token_symbol):
        if self.positions.get_open_for_token_wallet(token_addr, wallet_addr):
            return

        price = await self.price_feed.get_price(token_addr)
        if price <= 0:
            log.debug(f"  No price for {token_symbol} — skip")
            return

        gas_eth        = await self._fetch_gas_cost_eth()
        trade_size_eth = self.config.get("trade_size_eth")

        if not self.portfolio.allocate(trade_size_eth, gas_eth):
            log.debug(f"  Insufficient balance for {token_symbol}")
            return

        log.info(f"  👁 {wallet_label} BUY {token_symbol} — opening position")

        pos_id = self.positions.open_position(
            wallet_address=wallet_addr,
            wallet_label=wallet_label,
            wallet_color=wallet_color,
            token_address=token_addr,
            token_symbol=token_symbol,
            entry_price_usd=price,
            trade_size_eth=trade_size_eth,
            eth_price_usd=self._eth_price,
            gas_eth=gas_eth,
        )
        if not pos_id:
            self.portfolio.deallocate(trade_size_eth)

    async def _on_sell(self, wallet_addr, wallet_label, token_addr, token_symbol):
        # Only close positions opened by this specific wallet — never close manual positions
        pos = self.positions.get_open_for_token_wallet(token_addr, wallet_addr)
        if not pos:
            return
        if pos["wallet_address"] == "0x0000000000000000000000000000000000000001":
            return  # Never auto-close manual positions
        log.info(f"  👁 {wallet_label} SELL {token_symbol} — closing position")
        await self._close_position(pos["id"], "wallet_sold")

    async def _close_position(self, pos_id, reason):
        pos = next((p for p in self.positions.get_open_positions() if p["id"] == pos_id), None)
        if not pos:
            return
        gas_eth = await self._fetch_gas_cost_eth()
        result  = self.positions.close_position(pos_id, reason)
        if result is not None:
            pnl_usd, pnl_eth = result
            self.portfolio.record_closed_trade(
                trade_size_eth=pos["trade_size_eth"],
                pnl_eth=pnl_eth,
                gas_eth=gas_eth,
            )
            closed = next((p for p in self.positions.get_closed_positions() if p["id"] == pos_id), None)
            if closed:
                self.history.record(closed)

    async def _fetch_eth_price(self) -> float:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{COINGECKO}/simple/price?ids=ethereum&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        return float(d.get("ethereum", {}).get("usd", 0))
        except Exception:
            pass
        return self._eth_price

    async def _fetch_gas_cost_eth(self) -> float:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    ETHERSCAN,
                    params={"chainid": CHAIN_ID, "module": "gastracker",
                            "action": "gasoracle", "apikey": API_KEY},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.status == 200:
                        d        = await r.json()
                        gas_gwei = float(d.get("result", {}).get("SafeGasPrice", 5))
                        return round((SWAP_GAS_UNITS * gas_gwei * 1e9) / WEI_TO_ETH, 8)
        except Exception:
            pass
        return 0.003


if __name__ == "__main__":
    import sys
    ERROR_FILE = "agent_error.log"

    def _write_error(msg: str):
        print(msg, flush=True)
        try:
            with open(ERROR_FILE, "a") as f:
                f.write(msg + "\n---\n")
        except Exception:
            pass

    try:
        agent = SimTradeAgent()
    except Exception as e:
        _write_error(f"INIT CRASH: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        sys.exit(1)

    try:
        asyncio.run(agent.run())
    except (KeyboardInterrupt, SystemExit):
        print("Agent stopped.", flush=True)
    except Exception as e:
        _write_error(f"FATAL CRASH: {type(e).__name__}: {e}\n{traceback.format_exc()}")
