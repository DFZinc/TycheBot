"""
Sim Trade Agent — Server
------------------------
FastAPI backend serving the simulation dashboard.
Runs on port 8001 to avoid conflict with WalletEQ Agent on 8000.
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Agent process management ──────────────────────────────────────────

agent_process = None
agent_logs    = []

async def _stream_agent_output(process):
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue
        agent_logs.append({"text": text, "ts": datetime.now(timezone.utc).isoformat()})
        # Hard cap — never keep more than 200 entries
        while len(agent_logs) > 200:
            agent_logs.pop(0)

async def _background_price_update():
    """Update open position prices every 15 minutes when agent is stopped."""
    import aiohttp as _aiohttp
    GT      = "https://api.geckoterminal.com/api/v2"
    GT_HDR  = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    CG      = "https://api.coingecko.com/api/v3"

    while True:
        await asyncio.sleep(900)
        if agent_process and agent_process.returncode is None:
            continue  # Agent is running — it handles pricing
        try:
            positions = load_json(BASE_DIR / "sim_positions.json")
            if not isinstance(positions, dict):
                continue
            open_pos = [p for p in positions.values() if p.get("status") == "open"]
            if not open_pos:
                continue

            eth_price = 0.0
            async with _aiohttp.ClientSession() as s:
                async with s.get(f"{CG}/simple/price?ids=ethereum&vs_currencies=usd",
                                  timeout=_aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        d = await r.json()
                        eth_price = float(d.get("ethereum", {}).get("usd", 0))

            if eth_price <= 0:
                continue

            for pos in open_pos:
                try:
                    await asyncio.sleep(2)
                    token_addr = pos.get("token_address", "")
                    async with _aiohttp.ClientSession(headers=GT_HDR) as s:
                        async with s.get(
                            f"{GT}/networks/eth/tokens/{token_addr}/pools?page=1",
                            timeout=_aiohttp.ClientTimeout(total=10)
                        ) as r:
                            if r.status == 200:
                                data  = await r.json()
                                pools = data.get("data", [])
                                if pools:
                                    best  = max(pools, key=lambda p: float(
                                        p.get("attributes", {}).get("reserve_in_usd", 0) or 0))
                                    price = best.get("attributes", {}).get("base_token_price_usd")
                                    if price:
                                        price   = float(price)
                                        qty     = pos.get("token_quantity", 0)
                                        size    = pos.get("trade_size_usd", 0)
                                        pnl_usd = round(qty * price - size, 2)
                                        entry   = pos.get("entry_price_usd", 0)
                                        pnl_pct = round((price - entry) / entry * 100, 2) if entry > 0 else 0
                                        pid = pos["id"]
                                        positions[pid]["current_price_usd"]  = round(price, 8)
                                        positions[pid]["unrealized_pnl_usd"] = pnl_usd
                                        positions[pid]["unrealized_pnl_eth"] = round(
                                            pnl_usd / eth_price, 8) if eth_price > 0 else 0
                                        positions[pid]["pnl_pct"] = pnl_pct
                except Exception:
                    pass

            (BASE_DIR / "sim_positions.json").write_text(json.dumps(positions, indent=2))
            port = load_json(BASE_DIR / "sim_portfolio.json")
            if isinstance(port, dict):
                port["eth_price_usd"]      = eth_price
                port["unrealized_pnl_eth"] = round(sum(
                    p.get("unrealized_pnl_eth", 0)
                    for p in positions.values() if p.get("status") == "open"), 8)
                (BASE_DIR / "sim_portfolio.json").write_text(json.dumps(port, indent=2))
        except Exception as e:
            log.debug(f"Background price update error: {e}")


# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_background_price_update())
    yield


# ── App ───────────────────────────────────────────────────────────────

app = FastAPI(title="TycheBot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ── Helper ────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}

# ── Routes ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse(str(BASE_DIR / "static" / "sim_index.html"))

@app.get("/api/status")
async def get_status():
    portfolio = load_json(BASE_DIR / "sim_portfolio.json")
    watchlist = load_json(BASE_DIR / "sim_watchlist.json")
    positions = load_json(BASE_DIR / "sim_positions.json")
    open_count = sum(1 for p in (positions.values() if isinstance(positions, dict) else [])
                     if p.get("status") == "open")
    running = agent_process is not None and agent_process.returncode is None
    return {
        "agent_running":   running,
        "watched_wallets": len(watchlist) if isinstance(watchlist, dict) else 0,
        "open_positions":  open_count,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "portfolio":       portfolio,
    }

@app.get("/api/portfolio")
async def get_portfolio():
    data = load_json(BASE_DIR / "sim_portfolio.json")
    if not data:
        return {}
    total     = data.get("total_trades", 0)
    wins      = data.get("winning_trades", 0)
    eth_price = data.get("eth_price_usd", 0)
    data["win_rate_pct"]        = round(wins / total * 100, 1) if total > 0 else 0.0
    data["total_pnl_eth"]       = round(data.get("realized_pnl_eth", 0) + data.get("unrealized_pnl_eth", 0), 8)
    data["total_pnl_usd"]       = round(data["total_pnl_eth"] * eth_price, 2) if eth_price else 0
    data["current_balance_usd"] = round(data.get("current_balance_eth", 0) * eth_price, 2) if eth_price else 0
    data["available_eth"]       = round(data.get("current_balance_eth", 0) - data.get("allocated_eth", 0), 8)
    data["available_usd"]       = round(data["available_eth"] * eth_price, 2) if eth_price else 0
    return data

@app.get("/api/positions")
async def get_positions():
    data = load_json(BASE_DIR / "sim_positions.json")
    if not isinstance(data, dict):
        return {"open": [], "closed": []}
    open_pos   = sorted([p for p in data.values() if p.get("status") == "open"],
                        key=lambda x: x["opened_at"], reverse=True)
    closed_pos = sorted([p for p in data.values() if p.get("status") == "closed"],
                        key=lambda x: x.get("closed_at", ""), reverse=True)
    return {"open": open_pos, "closed": closed_pos[:50]}

@app.get("/api/trades")
async def get_trades():
    data = load_json(BASE_DIR / "sim_trades.json")
    if not isinstance(data, list):
        return []
    return list(reversed(data))[:50]

@app.get("/api/watchlist")
async def get_watchlist():
    data = load_json(BASE_DIR / "sim_watchlist.json")
    if not isinstance(data, dict):
        return []
    return list(data.values())

@app.post("/api/watchlist")
async def add_to_watchlist(request: dict):
    from sim_watchlist import SimWatchlist
    wl      = SimWatchlist()
    address = request.get("address", "").strip()
    label   = request.get("label", "")
    ok, msg = wl.add(address, label)
    return {"success": ok, "message": msg}

@app.delete("/api/watchlist/{address}")
async def remove_from_watchlist(address: str):
    from sim_watchlist import SimWatchlist
    wl      = SimWatchlist()
    removed = wl.remove(address)
    return {"success": removed}

@app.get("/api/agent-settings")
async def get_agent_settings():
    """Load API key and theme settings from agent_settings.json."""
    path = BASE_DIR / "agent_settings.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"api_key": "", "theme": "A", "blur": 6, "opacity": 45}

@app.post("/api/agent-settings")
async def save_agent_settings(request: dict):
    """Save API key and theme settings to agent_settings.json."""
    path = BASE_DIR / "agent_settings.json"
    # Load existing, merge updates
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
    existing.update(request)
    path.write_text(json.dumps(existing, indent=2))
    return existing

@app.get("/api/config")
async def get_config():
    from sim_config import SimConfig
    return SimConfig().all()

@app.post("/api/config")
async def update_config(request: dict):
    from sim_config import SimConfig
    cfg = SimConfig()
    cfg.update(request)
    return cfg.all()

@app.post("/api/positions/{pos_id}/close")
async def manual_close_position(pos_id: str):
    from position_tracker import PositionTracker
    from sim_portfolio    import SimPortfolio
    from trade_history    import TradeHistory
    from sim_config       import SimConfig

    cfg  = SimConfig()
    pt   = PositionTracker()
    port = SimPortfolio(cfg.get("starting_balance_eth"))
    hist = TradeHistory()

    pos = next((p for p in pt.get_open_positions() if p["id"] == pos_id), None)
    if not pos:
        return JSONResponse(status_code=404, content={"error": "Position not found"})

    result = pt.close_position(pos_id, "manual")
    pnl_usd = 0
    if result is not None:
        pnl_usd, pnl_eth = result
        port.record_closed_trade(trade_size_eth=pos["trade_size_eth"], pnl_eth=pnl_eth, gas_eth=0.0)
        closed = next((p for p in pt.get_closed_positions() if p["id"] == pos_id), None)
        if closed:
            hist.record(closed)
    return {"success": True, "pnl_usd": pnl_usd}

@app.post("/api/portfolio/reset")
async def reset_portfolio(request: dict):
    from sim_portfolio import SimPortfolio
    from sim_config    import SimConfig
    cfg     = SimConfig()
    balance = request.get("starting_balance_eth", cfg.get("starting_balance_eth"))
    port    = SimPortfolio(balance)
    port.reset(balance)
    return {"success": True, "starting_balance_eth": balance}

@app.get("/api/logs")
async def get_logs():
    return agent_logs[-200:]

# ── Manual swap ──────────────────────────────────────────────────────

@app.get("/api/token/lookup/{address}")
async def token_lookup(address: str):
    """Fetch token info from GeckoTerminal for manual swap panel."""
    import aiohttp as _aio
    addr = address.lower().strip()
    GT   = "https://api.geckoterminal.com/api/v2"
    HDR  = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        async with _aio.ClientSession(headers=HDR) as s:
            # Step 1: fetch token info (name, symbol, image, price)
            async with s.get(
                f"{GT}/networks/eth/tokens/{addr}",
                timeout=_aio.ClientTimeout(total=12)
            ) as r:
                if r.status != 200:
                    return JSONResponse(status_code=404, content={"error": "Token not found"})
                token_data = await r.json()

            token_attrs = token_data.get("data", {}).get("attributes", {})
            name        = token_attrs.get("name", addr[:8] + "...")
            symbol      = token_attrs.get("symbol", "?")
            image_url   = token_attrs.get("image_url", None)
            price_usd   = float(token_attrs.get("price_usd", 0) or 0)

            # Step 2: fetch pools for price change % and volume
            async with s.get(
                f"{GT}/networks/eth/tokens/{addr}/pools?page=1",
                timeout=_aio.ClientTimeout(total=12)
            ) as r2:
                pools_data = await r2.json() if r2.status == 200 else {}

            pools = pools_data.get("data", [])
            inc   = {}
            vol   = 0.0
            if pools:
                best  = max(pools, key=lambda p: float(
                    p.get("attributes", {}).get("reserve_in_usd", 0) or 0))
                attrs = best.get("attributes", {})
                inc   = attrs.get("price_change_percentage", {})
                vol   = float(attrs.get("volume_usd", {}).get("h24", 0) or 0)
                # Use pool price if token endpoint returned 0
                if price_usd == 0:
                    price_usd = float(attrs.get("base_token_price_usd", 0) or 0)

            return {
                "address":   addr,
                "name":      name,
                "symbol":    symbol,
                "image_url": image_url,
                "price_usd": price_usd,
                "change_1h": float(inc.get("h1",  0) or 0),
                "change_24h":float(inc.get("h24", 0) or 0),
                "change_7d": float(inc.get("d7",  0) or 0),
                "volume_24h":vol,
            }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/manual/buy")
async def manual_buy(request: dict):
    """Open a manual simulated position."""
    from position_tracker import PositionTracker
    from sim_portfolio    import SimPortfolio
    from sim_config       import SimConfig

    addr       = request.get("token_address", "").lower()
    symbol     = request.get("token_symbol", "?")
    price      = float(request.get("price_usd", 0))
    size_eth   = float(request.get("trade_size_eth", 0.05))
    eth_price  = float(request.get("eth_price_usd", 0))

    if not addr or price <= 0 or size_eth <= 0:
        return JSONResponse(status_code=400, content={"error": "Invalid parameters"})
    if eth_price <= 0:
        eth_price = 2000.0  # fallback — will update on next agent price fetch

    cfg  = SimConfig()
    pt   = PositionTracker()
    port = SimPortfolio(cfg.get("starting_balance_eth"))

    if not port.allocate(size_eth, 0.003):  # estimated gas
        return JSONResponse(status_code=400, content={"error": "Insufficient balance"})

    pos_id = pt.open_position(
        wallet_address="0x0000000000000000000000000000000000000001",
        wallet_label="Manual",
        wallet_color="#ffffff",
        token_address=addr,
        token_symbol=symbol,
        entry_price_usd=price,
        trade_size_eth=size_eth,
        eth_price_usd=eth_price,
        gas_eth=0.003,
    )

    if not pos_id:
        port.deallocate(size_eth)
        return JSONResponse(status_code=500, content={"error": "Failed to open position"})

    return {"success": True, "position_id": pos_id, "symbol": symbol, "price": price}


@app.get("/api/errors")
async def get_errors():
    """Return contents of agent_error.log if it exists."""
    err_file = BASE_DIR / "agent_error.log"
    if err_file.exists():
        return {"errors": err_file.read_text()}
    return {"errors": ""}

# ── Agent control ─────────────────────────────────────────────────────

@app.post("/api/agent/start")
async def start_agent():
    global agent_process, agent_logs
    if agent_process and agent_process.returncode is None:
        return {"status": "already_running"}
    agent_logs = []
    import os as _os
    env = _os.environ.copy()
    agent_process = await asyncio.create_subprocess_exec(
        sys.executable, str(BASE_DIR / "sim_agent.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(BASE_DIR),
        env=env,
    )
    asyncio.create_task(_stream_agent_output(agent_process))
    return {"status": "started"}

@app.post("/api/agent/stop")
async def stop_agent():
    global agent_process
    if agent_process and agent_process.returncode is None:
        agent_process.terminate()
        try:
            await asyncio.wait_for(agent_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            agent_process.kill()
            await agent_process.wait()
    return {"status": "stopped"}

# ── WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    # Start from current end — history is loaded via /api/logs on page init
    # This prevents resending entire history on every reconnect
    last = len(agent_logs)
    try:
        while True:
            if len(agent_logs) > last:
                for entry in agent_logs[last:]:
                    await websocket.send_json(entry)
                last = len(agent_logs)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass

# ── Run ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
