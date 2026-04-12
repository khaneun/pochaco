"""빗썸 REST API 클라이언트

Public API  : v1 엔드포인트 유지 (/public/ticker 등, 인증 불필요)
Private API : v2 JWT 인증 방식 (/v1/accounts, /v1/orders 등)
"""
import hashlib
import logging
import time
import urllib.parse
import uuid

import jwt
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
    #  인증 헬퍼 (API v2 JWT)                                              #
    # ------------------------------------------------------------------ #
    def _jwt_header(self, params: dict | None = None) -> dict:
        """JWT 인증 헤더 생성 (HS256)

        params가 있으면 SHA512 query_hash를 payload에 포함합니다.
        """
        payload: dict = {
            "access_key": self._api_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": round(time.time() * 1000),
        }
        if params:
            query_string = urllib.parse.urlencode(params).encode()
            query_hash = hashlib.sha512(query_string).hexdigest()
            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        token = jwt.encode(payload, self._secret_key, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    def _v2_get(self, path: str, params: dict | None = None) -> any:
        headers = self._jwt_header(params)
        resp = self._session.get(
            self.BASE_URL + path, params=params, headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def _v2_post(self, path: str, body: dict) -> any:
        headers = {**self._jwt_header(body), "Content-Type": "application/json"}
        resp = self._session.post(
            self.BASE_URL + path, json=body, headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def _v2_delete(self, path: str, params: dict) -> any:
        headers = self._jwt_header(params)
        resp = self._session.delete(
            self.BASE_URL + path, params=params, headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  Public API (v1, 인증 불필요)                                        #
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
    #  Private API (v2 JWT)                                               #
    # ------------------------------------------------------------------ #
    def get_balance(self, currency: str = "ALL") -> dict:
        """잔고 조회 — trading_engine 호환 포맷(v1 스타일)으로 반환

        v2 응답: [{"currency":"KRW","balance":"94825","locked":"0",...}, ...]
        반환:    {"status":"0000","data":{"available_krw":"94825","total_krw":"94825","in_use_krw":"0",...}}
        """
        accounts = self._v2_get("/v1/accounts")
        data: dict = {}
        for acc in accounts:
            cur = acc["currency"].lower()
            bal = float(acc.get("balance", 0))
            locked = float(acc.get("locked", 0))
            data[f"available_{cur}"] = str(bal)
            data[f"total_{cur}"] = str(bal + locked)
            data[f"in_use_{cur}"] = str(locked)
        return {"status": "0000", "data": data}

    def get_orders(self, symbol: str, order_id: str = "", order_type: str = "") -> dict:
        """미체결 주문 조회 — v1 호환 포맷 반환"""
        params: dict = {"market": f"KRW-{symbol}", "state": "wait", "limit": 100}
        orders = self._v2_get("/v1/orders", params)
        # v1 호환 포맷으로 변환
        normalized = [
            {
                "order_id": o.get("uuid", ""),
                "type": "bid" if o.get("side") == "bid" else "ask",
                "order_currency": symbol,
                "payment_currency": "KRW",
                "units": o.get("volume", "0"),
                "price": o.get("price", "0"),
            }
            for o in (orders if isinstance(orders, list) else [])
        ]
        return {"status": "0000", "data": normalized}

    def get_executed_orders(self, symbol: str, limit: int = 20) -> list[dict]:
        """체결 완료 주문 조회 (state=done)

        Args:
            symbol: 코인 심볼 (예: "HEMI")
            limit: 최대 조회 건수 (최대 100)

        Returns:
            체결 주문 목록. 각 항목:
              - uuid: 주문 ID
              - side: "bid" | "ask"
              - avg_price: 평균 체결가 (float)
              - executed_volume: 체결 수량 (float)
              - executed_funds: 체결 금액 합계 (float)
              - created_at: 주문 생성 시각 (ISO 문자열)
              - trades: 개별 체결 목록
        """
        params: dict = {"market": f"KRW-{symbol}", "state": "done", "limit": min(limit, 100)}
        raw = self._v2_get("/v1/orders", params)
        if not isinstance(raw, list):
            return []
        result = []
        for o in raw:
            avg_price_str = o.get("avg_price") or o.get("price") or "0"
            exec_vol_str = o.get("executed_volume") or o.get("volume") or "0"
            # executed_funds = avg_price * exec_vol (일부 API는 직접 제공)
            avg_price = float(avg_price_str) if avg_price_str else 0.0
            exec_vol = float(exec_vol_str) if exec_vol_str else 0.0
            exec_funds = avg_price * exec_vol

            # trades 배열에서 실제 체결 금액(funds) 합산
            trades = o.get("trades", [])
            if trades:
                exec_funds = sum(float(t.get("funds", 0)) for t in trades)
                if exec_funds > 0 and exec_vol > 0:
                    avg_price = exec_funds / exec_vol

            result.append({
                "uuid": o.get("uuid", ""),
                "side": o.get("side", ""),
                "avg_price": avg_price,
                "executed_volume": exec_vol,
                "executed_funds": exec_funds,
                "created_at": o.get("created_at", ""),
                "trades": trades,
            })
        return result

    def get_order_by_uuid(self, uuid: str) -> dict | None:
        """특정 주문 UUID로 체결 상세 조회

        Args:
            uuid: 빗썸 v2 주문 UUID

        Returns:
            주문 상세 딕셔너리 또는 None (조회 실패 시)
        """
        try:
            raw = self._v2_get("/v1/order", {"uuid": uuid})
            if not isinstance(raw, dict):
                return None
            avg_price_str = raw.get("avg_price") or raw.get("price") or "0"
            exec_vol_str = raw.get("executed_volume") or raw.get("volume") or "0"
            avg_price = float(avg_price_str) if avg_price_str else 0.0
            exec_vol = float(exec_vol_str) if exec_vol_str else 0.0
            exec_funds = avg_price * exec_vol
            trades = raw.get("trades", [])
            if trades:
                exec_funds = sum(float(t.get("funds", 0)) for t in trades)
                if exec_funds > 0 and exec_vol > 0:
                    avg_price = exec_funds / exec_vol
            return {
                "uuid": raw.get("uuid", uuid),
                "side": raw.get("side", ""),
                "state": raw.get("state", ""),
                "avg_price": avg_price,
                "executed_volume": exec_vol,
                "executed_funds": exec_funds,
                "created_at": raw.get("created_at", ""),
                "trades": trades,
            }
        except Exception as e:
            logger.warning(f"[주문 조회 실패] uuid={uuid}: {e}")
            return None

    def market_buy(self, symbol: str, krw_amount: float) -> dict:
        """시장가 매수 (v2: ord_type=price → KRW 금액으로 즉시 체결)"""
        body = {
            "market": f"KRW-{symbol}",
            "side": "bid",
            "ord_type": "price",
            "price": str(krw_amount),
        }
        logger.info(f"[매수] {symbol} {krw_amount:,.0f} KRW (ord_type=price)")
        try:
            result = self._v2_post("/v1/orders", body)
            logger.info(f"[매수 결과] {result}")
            return {"status": "0000", "data": result}
        except requests.HTTPError as e:
            body_text = e.response.text if e.response is not None else str(e)
            logger.error(f"[매수 실패] {e.response.status_code if e.response is not None else ''}: {body_text}")
            return {"status": "9999", "message": body_text}

    def market_sell(self, symbol: str, units: float) -> dict:
        """시장가 매도 (v2: ord_type=market → 코인 수량으로 즉시 체결)"""
        body = {
            "market": f"KRW-{symbol}",
            "side": "ask",
            "ord_type": "market",
            "volume": str(units),
        }
        logger.info(f"[매도] {symbol} {units}개 (ord_type=market)")
        try:
            result = self._v2_post("/v1/orders", body)
            logger.info(f"[매도 결과] {result}")
            return {"status": "0000", "data": result}
        except requests.HTTPError as e:
            body_text = e.response.text if e.response is not None else str(e)
            logger.warning(f"[매도 실패] {body_text}")
            return {"status": "9999", "message": body_text}

    def limit_buy(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매수"""
        body = {
            "market": f"KRW-{symbol}",
            "side": "bid",
            "ord_type": "limit",
            "price": str(price),
            "volume": str(units),
        }
        try:
            return {"status": "0000", "data": self._v2_post("/v1/orders", body)}
        except requests.HTTPError as e:
            return {"status": "9999", "message": e.response.text if e.response else str(e)}

    def limit_sell(self, symbol: str, price: float, units: float) -> dict:
        """지정가 매도"""
        body = {
            "market": f"KRW-{symbol}",
            "side": "ask",
            "ord_type": "limit",
            "price": str(price),
            "volume": str(units),
        }
        try:
            return {"status": "0000", "data": self._v2_post("/v1/orders", body)}
        except requests.HTTPError as e:
            return {"status": "9999", "message": e.response.text if e.response else str(e)}

    def cancel_order(self, order_type: str, order_id: str, symbol: str) -> dict:
        """주문 취소. order_id = v2 uuid"""
        try:
            result = self._v2_delete("/v1/order", {"uuid": order_id})
            return {"status": "0000", "data": result}
        except requests.HTTPError as e:
            return {"status": "9999", "message": e.response.text if e.response else str(e)}

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
        """현재 시장가(closing_price) 반환

        Returns:
            0 초과의 실제 가격

        Raises:
            RuntimeError: API 실패 또는 가격이 0원 이하인 경우
        """
        data = self.get_ticker(symbol)
        if data.get("status") != "0000":
            raise RuntimeError(f"시세 조회 실패: {data}")
        price = float(data["data"]["closing_price"])
        if price <= 0:
            raise RuntimeError(f"비정상 시세 (0원): {symbol}")
        return price
