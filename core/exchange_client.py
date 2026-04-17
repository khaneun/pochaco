"""거래소 클라이언트 추상 기반 클래스

모든 거래소 구현체는 이 인터페이스를 준수해야 합니다.
응답 포맷은 하위 클래스가 내부적으로 정규화하여 통일된 형식으로 반환합니다.

정규화 포맷:
  get_ticker(symbol)       → {"status":"0000", "data": {ticker_fields}} |
                             {"status":"0000", "data": {symbol: {ticker_fields}, ...}} (ALL)
  get_balance()            → {"status":"0000", "data": {"available_krw": ..., ...}}
  market_buy/sell()        → {"status":"0000", "data": {...}} | {"status":"9999", ...}
  get_executed_orders()    → [{"uuid":..., "side":..., "avg_price":..., ...}]
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseExchangeClient(ABC):
    """거래소 클라이언트 공통 인터페이스"""

    # ── Public API ──────────────────────────────────────────────────── #

    @abstractmethod
    def get_ticker(self, symbol: str = "ALL") -> dict:
        """현재가 정보 조회.

        Args:
            symbol: 코인 심볼. "ALL" 이면 전체 코인 dict 반환.

        Returns:
            {"status": "0000", "data": ticker_data}
            - symbol="ALL" 시 data = {symbol: {closing_price, opening_price, ...}}
            - symbol=특정코인 시 data = {closing_price, opening_price, max_price,
              min_price, units_traded_24H, acc_trade_value_24H, sell_price, buy_price, ...}
        """

    @abstractmethod
    def get_orderbook(self, symbol: str) -> dict:
        """호가 정보 조회"""

    @abstractmethod
    def get_transaction_history(self, symbol: str, count: int = 20) -> dict:
        """최근 체결 내역 조회"""

    @abstractmethod
    def get_candlestick(self, symbol: str, interval: str = "1h") -> dict:
        """캔들스틱 데이터 조회.

        Returns:
            {"status": "0000", "data": [[ts, open, close, high, low, vol], ...]}
            오래된 것이 앞, 최신이 뒤.
        """

    @abstractmethod
    def get_all_symbols(self) -> list[str]:
        """거래 가능한 전체 코인 심볼 목록"""

    # ── Private API ─────────────────────────────────────────────────── #

    @abstractmethod
    def get_balance(self, currency: str = "ALL") -> dict:
        """잔고 조회.

        Returns:
            {"status": "0000", "data": {
                "available_krw": "...", "total_krw": "...", "in_use_krw": "...",
                "available_{coin}": "...", ...
            }}
        """

    @abstractmethod
    def get_orders(self, symbol: str, order_id: str = "", order_type: str = "") -> dict:
        """미체결 주문 조회.

        Returns:
            {"status": "0000", "data": [{order_id, type, order_currency, ...}, ...]}
        """

    @abstractmethod
    def get_executed_orders(self, symbol: str, limit: int = 20) -> list[dict]:
        """체결 완료 주문 조회.

        Returns:
            [{uuid, side, avg_price, executed_volume, executed_funds, created_at, trades}]
        """

    @abstractmethod
    def get_order_by_uuid(self, uuid: str) -> dict | None:
        """특정 주문 UUID로 체결 상세 조회"""

    @abstractmethod
    def market_buy(self, symbol: str, krw_amount: float) -> dict:
        """시장가 매수 (KRW 금액 기반)"""

    @abstractmethod
    def market_sell(self, symbol: str, units: float) -> dict:
        """시장가 매도 (코인 수량 기반)"""

    @abstractmethod
    def limit_buy(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매수"""

    @abstractmethod
    def limit_sell(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매도"""

    @abstractmethod
    def cancel_order(self, order_type: str, order_id: str, symbol: str) -> dict:
        """주문 취소"""

    @abstractmethod
    def cancel_all_orders(self, symbol: str) -> list[dict]:
        """미체결 주문 일괄 취소"""

    # ── 잔고 헬퍼 ───────────────────────────────────────────────────── #

    @abstractmethod
    def get_krw_balance(self) -> float:
        """보유 KRW 가용 잔고"""

    @abstractmethod
    def get_krw_balance_detail(self) -> dict:
        """KRW 잔고 상세 (available, total, in_use)"""

    @abstractmethod
    def get_coin_balance(self, symbol: str) -> float:
        """특정 코인 보유 수량(가용)"""

    @abstractmethod
    def get_current_price(self, symbol: str) -> float:
        """현재 시장가.

        Raises:
            RuntimeError: API 실패 또는 가격이 0원 이하인 경우
        """
