"""bybit run — boots the trading loop + event detector as cooperative asyncio tasks."""
from __future__ import annotations

import asyncio
import signal

from bybit_agent.config.env import get_env
from bybit_agent.control.agent_loop import AgentLoop, FALLBACK_SYMBOLS
from bybit_agent.core.logger import get_logger
from bybit_agent.events.detector import EventDetector
from bybit_agent.exchange.bybit_client import BybitClient
from bybit_agent.exchange.credentials import resolve_bybit_auth
from bybit_agent.market.market_data import MarketDataService
from bybit_agent.persistence.db import NeonHttpClient, get_db

log = get_logger().bind(module="service")


async def start_service(testnet: bool = True) -> None:
    env = get_env()
    auth = resolve_bybit_auth(env)
    is_testnet = env.BYBIT_ENV == "testnet" if not testnet else testnet

    db: NeonHttpClient = get_db()
    client = BybitClient(auth, is_testnet=is_testnet)
    market = MarketDataService(client)

    loop_obj = AgentLoop(client, db, is_testnet=is_testnet)
    detector = EventDetector(db, market, symbols=list(FALLBACK_SYMBOLS))

    loop_task     = asyncio.create_task(_run_loop(loop_obj))
    detector_task = asyncio.create_task(detector.run())

    ev_loop = asyncio.get_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Shutdown signal received", signal=sig.name)
        loop_obj.stop()
        detector.stop()
        loop_task.cancel()
        detector_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            ev_loop.add_signal_handler(sig, _shutdown, sig)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    log.info("Service starting", testnet=is_testnet)
    try:
        await asyncio.gather(loop_task, detector_task, return_exceptions=True)
    finally:
        await client.aclose()
        from bybit_agent.persistence.db import close_db
        await close_db()
    log.info("Service stopped")


async def _run_loop(loop_obj: AgentLoop) -> None:
    try:
        await loop_obj.start()
    except asyncio.CancelledError:
        pass
