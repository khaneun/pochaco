"""빗썸 WebSocket 실시간 시세 클라이언트"""
import asyncio
import json
import logging
from typing import Callable

import websockets

from config import settings

logger = logging.getLogger(__name__)


class BithumbWebSocket:
    """빗썸 실시간 시세 WebSocket 클라이언트

    사용 예시:
        ws = BithumbWebSocket(on_tick=my_callback)
        asyncio.run(ws.subscribe(["BTC", "ETH"]))
    """

    WS_URL = settings.BITHUMB_WS_URL

    def __init__(self, on_tick: Callable[[dict], None] | None = None):
        self._on_tick = on_tick
        self._running = False
        self._latest: dict[str, dict] = {}  # symbol → latest tick

    def get_latest(self, symbol: str) -> dict | None:
        return self._latest.get(symbol)

    async def subscribe(self, symbols: list[str]) -> None:
        """지정된 코인들의 실시간 체결가를 구독"""
        self._running = True
        subscribe_msg = {
            "type": "ticker",
            "symbols": [f"{s}_KRW" for s in symbols],
            "tickTypes": ["MID"],
        }

        while self._running:
            try:
                async with websockets.connect(self.WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"WebSocket 연결됨: {symbols}")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "ticker":
                                content = msg.get("content", {})
                                symbol = content.get("symbol", "").replace("_KRW", "")
                                self._latest[symbol] = content
                                if self._on_tick:
                                    self._on_tick(content)
                        except (json.JSONDecodeError, KeyError):
                            pass
            except Exception as e:
                if self._running:
                    logger.warning(f"WebSocket 연결 끊김, 재연결 시도: {e}")
                    await asyncio.sleep(3)
                else:
                    break

    def stop(self) -> None:
        self._running = False
