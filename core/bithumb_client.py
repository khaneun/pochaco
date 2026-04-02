"""빗썸 REST API 클라이언트"""
import base64
import hashlib
import hmac
import time
import urllib.parse
import logging
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)


class BithumbClient:
    """빗썸 Public/Private REST API 래퍼"""

    BASE_URL = settings.BITHUMB_BASE_URL

    def __init__(self):
        self._api_key = settings.BITHUMB_API_KEY
        self._secret_key = settings.BITHUMB_SECRET_KEY
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------ #
    #  인증 헬퍼                                                           #
    # ------------------------------------------------------------------ #
    def _sign(self, endpoint: str, params: dict) -> dict:
        """HMAC-SHA512 서명 생성 후 헤더 반환

        빗썸 인증 규격:
          hmac_data = endpoint + "\0" + urlencode({...params, endpoint}) + "\0" + nonce
          Api-Sign  = Base64(HMAC-SHA512(secret_key, hmac_data))
        nonce는 밀리초(13자리), endpoint는 query string 마지막에 위치해야 합니다.
        """
        nonce = str(int(time.time() * 1_000))
        sign_params = {**params, "endpoint": endpoint}  # endpoint는 마지막

        query = urllib.parse.urlencode(sign_params)
        hmac_data = f"{endpoint}\0{query}\0{nonce}"
        raw_sig = hmac.new(
            self._secret_key.encode("utf-8"),
            hmac_data.encode("utf-8"),
            digestmod=hashlib.sha512,
        ).hexdigest()
        signature = base64.b64encode(raw_sig.encode("utf-8")).decode("utf-8")

        return {
            "Api-Key": self._api_key,
            "Api-Sign": signature,
            "Api-Nonce": nonce,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #
    def get_ticker(self, symbol: str = "ALL") -> dict:
        """현재가 정보 조회. symbol='ALL' 이면 전체 코인"""
        url = f"{self.BASE_URL}/public/ticker/{symbol}_KRW"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, symbol: str) -> dict:
        """호가 정보 조회"""
        url = f"{self.BASE_URL}/public/orderbook/{symbol}_KRW"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_transaction_history(self, symbol: str, count: int = 20) -> dict:
        """최근 체결 내역 조회"""
        url = f"{self.BASE_URL}/public/transaction_history/{symbol}_KRW"
        resp = self._session.get(url, params={"count": count}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_candlestick(self, symbol: str, interval: str = "1h") -> dict:
        """캔들스틱 데이터 조회. interval: 1m, 3m, 5m, 10m, 30m, 1h, 6h, 12h, 24h"""
        url = f"{self.BASE_URL}/public/candlestick/{symbol}_KRW/{interval}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_all_symbols(self) -> list[str]:
        """빗썸에서 거래 가능한 전체 코인 심볼 목록 반환"""
        data = self.get_ticker("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"ticker 조회 실패: {data}")
        return [k for k in data["data"].keys() if k != "date"]

    # ------------------------------------------------------------------ #
    #  Private API                                                         #
    # ------------------------------------------------------------------ #
    def _private_post(self, endpoint: str, params: dict) -> dict:
        """Private API POST 요청 공통 처리"""
        # POST body에도 endpoint 포함 (빗썸 API 규격)
        post_params = {**params, "endpoint": endpoint}
        headers = self._sign(endpoint, params)
        resp = self._session.post(
            self.BASE_URL + endpoint, data=post_params, headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def get_balance(self, currency: str = "ALL") -> dict:
        """잔고 조회"""
        return self._private_post("/info/balance", {"currency": currency})

    def get_orders(self, symbol: str, order_id: str = "", order_type: str = "") -> dict:
        """미체결 주문 조회"""
        params = {
            "order_currency": symbol,
            "payment_currency": "KRW",
        }
        if order_id:
            params["order_id"] = order_id
        if order_type:
            params["type"] = order_type
        return self._private_post("/info/orders", params)

    def get_user_transactions(self, symbol: str, offset: int = 0, count: int = 20) -> dict:
        """거래 내역 조회"""
        return self._private_post("/info/user_transactions", {
            "order_currency": symbol,
            "payment_currency": "KRW",
            "offset": offset,
            "count": count,
        })

    def market_buy(self, symbol: str, krw_amount: float) -> dict:
        """시장가 매수 (빗썸 market_buy: units 파라미터에 KRW 금액 전달)"""
        params = {
            "order_currency": symbol,
            "payment_currency": "KRW",
            "units": krw_amount,
        }
        logger.info(f"[매수] {symbol} {krw_amount:,.0f} KRW")
        result = self._private_post("/trade/market_buy", params)
        logger.info(f"[매수 결과] {result}")
        return result

    def market_sell(self, symbol: str, units: float) -> dict:
        """시장가 매도 (코인 수량 지정)"""
        params = {
            "order_currency": symbol,
            "payment_currency": "KRW",
            "units": units,
        }
        logger.info(f"[매도] {symbol} {units} 개")
        result = self._private_post("/trade/market_sell", params)
        if result.get("status") == "0000":
            logger.info(f"[매도 체결] {result}")
        else:
            logger.warning(f"[매도 실패] {result}")
        return result

    def limit_buy(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매수"""
        return self._private_post("/trade/place", {
            "order_currency": symbol,
            "payment_currency": "KRW",
            "type": "bid",
            "price": price,
            "units": units,
        })

    def limit_sell(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매도"""
        return self._private_post("/trade/place", {
            "order_currency": symbol,
            "payment_currency": "KRW",
            "type": "ask",
            "price": price,
            "units": units,
        })

    def cancel_order(self, order_type: str, order_id: str, symbol: str) -> dict:
        """주문 취소. order_type: 'bid'(매수) or 'ask'(매도)"""
        return self._private_post("/trade/cancel", {
            "type": order_type,
            "order_id": order_id,
            "order_currency": symbol,
            "payment_currency": "KRW",
        })

    def cancel_all_orders(self, symbol: str) -> list[dict]:
        """미체결 주문 일괄 취소"""
        try:
            orders_data = self.get_orders(symbol)
        except Exception as e:
            logger.warning(f"[미체결 조회 실패] {symbol}: {e}")
            return []

        results = []
        if orders_data.get("status") != "0000":
            return results

        orders = orders_data.get("data", [])
        if not orders or isinstance(orders, str):
            return results

        for order in orders:
            order_id = order.get("order_id", "")
            order_type = order.get("type", "")
            if not order_id:
                continue
            try:
                r = self.cancel_order(order_type, order_id, symbol)
                results.append(r)
                logger.info(f"[주문 취소] {symbol} {order_type} {order_id}: {r.get('status')}")
            except Exception as e:
                logger.error(f"[주문 취소 실패] {symbol} {order_id}: {e}")
        return results

    # ------------------------------------------------------------------ #
    #  잔고 헬퍼                                                            #
    # ------------------------------------------------------------------ #
    def get_krw_balance(self) -> float:
        """보유 KRW 가용 잔고 반환"""
        data = self.get_balance("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {data}")
        available = float(data["data"].get("available_krw", 0))
        total = float(data["data"].get("total_krw", 0))
        in_use = float(data["data"].get("in_use_krw", 0))
        logger.info(f"[잔고] available={available:,.0f} total={total:,.0f} in_use={in_use:,.0f}")
        return available

    def get_krw_balance_detail(self) -> dict:
        """KRW 잔고 상세 (available, total, in_use) 반환"""
        data = self.get_balance("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {data}")
        return {
            "available": float(data["data"].get("available_krw", 0)),
            "total": float(data["data"].get("total_krw", 0)),
            "in_use": float(data["data"].get("in_use_krw", 0)),
        }

    def get_coin_balance(self, symbol: str) -> float:
        """특정 코인 보유 수량(가용) 반환"""
        data = self.get_balance("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {data}")
        key = f"available_{symbol.lower()}"
        return float(data["data"].get(key, 0))

    def get_current_price(self, symbol: str) -> float:
        """현재 시장가(closing_price) 반환"""
        data = self.get_ticker(symbol)
        if data.get("status") != "0000":
            raise RuntimeError(f"시세 조회 실패: {data}")
        return float(data["data"]["closing_price"])
