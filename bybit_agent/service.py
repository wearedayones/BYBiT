"""bybit run — boots the trading loop + event detector as cooperative asyncio tasks."""
from __future__ import annotations

import asyncio
import os
import signal

from bybit_agent.core.logger import get_logger
from bybit_agent.persistence.db import NeonHttpClient
from bybit_agent.exchange.bybit_client import BybitClient
from bybit_agent.exchange.credentials import resolve_bybit_auth
from bybit_agent.control.agent_loop import AgentLoop

log = get_logger().bind(module="service")


async def _run_event_detector() -> None:
    """Placeholder for Phase 4 event detector — runs alongside the trading loop."""
    while True:
        await asyncio.sleep(60)


async def run_service(testnet: bool = True) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL env var is required")

    db = NeonHttpClient(database_url)
    auth = resolve_bybit_auth()
    client = BybitClient(auth, testnet=testnet, db=db)

    loop_task = asyncio.create_task(_run_trading_loop(client, db, testnet))
    detector_task = asyncio.create_task(_run_event_detector())

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Shutdown signal received", signal=sig.name)
        loop_task.cancel()
        detector_task.cancel()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        await asyncio.gather(loop_task, detector_task)
    except asyncio.CancelledError:
        log.info("Service stopped")


async def _run_trading_loop(client: BybitClient, db: NeonHttpClient, testnet: bool) -> None:
    agent = AgentLoop(client, db, is_testnet=testnet)
    await agent.start()
