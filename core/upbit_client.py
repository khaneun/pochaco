"""업비트 REST API 클라이언트

Public API  : /v1/ticker, /v1/candles/*, /v1/orderbook 등
Private API : /v1/accounts, /v1/orders 등 (JWT HS256 인증)

응답 포맷: BithumbClient와 동일한 정규화 포맷으로 반환하여
상위 컴포넌트(MarketAnalyzer, TradingEngine 등)가 거래소에 무관하게 동작합니다.
"""
import hashlib
import logging
import time
import urllib.parse
import uuid as _uuid_mod

import jwt
import requests

from config import settings
from .exchange_client import BaseExchangeClient

logger = logging.getLogger(__name__)

# ── 업비트 캔들 interval → (타입, 단위) 매핑 ──────────────────────── #
_INTERVAL_MAP: dict[str, tuple[str, int | None]] = {
    "1m":  ("minutes", 1),
    "3m":  ("minutes", 3),
    "5m":  ("minutes", 5),
    "10m": ("minutes", 10),
    "30m": ("minutes", 30),
    "1h":  ("minutes", 60),
    "6h":  ("minutes", 240),   # 업비트 최대 분단위 = 240분
    "12h": ("days", None),
    "24h": ("days", None),
}


class UpbitClient(BaseExchangeClient):
    """업비트 REST API 래퍼 (BaseExchangeClient 구현체)"""

    BASE_URL = settings.UPBIT_BASE_URL

    def __init__(self):
        self._api_key = settings.UPBIT_ACCESS_KEY
        self._secret_key = settings.UPBIT_SECRET_KEY
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._markets_cache: list[str] | None = None  # KRW-XXX 목록 캐시

    # ------------------------------------------------------------------ #
    #  인증 헬퍼 (JWT HS256 — 빗썸 v2와 동일 방식)                         #
    # ------------------------------------------------------------------ #
    def _jwt_header(self, params: dict | None = None) -> dict:
        """JWT 인증 헤더 생성 (HS256)"""
        payload: dict = {
            "access_key": self._api_key,
            "nonce": str(_uuid_mod.uuid4()),
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
    #  내부 헬퍼                                                            #
    # ------------------------------------------------------------------ #
    def _get_krw_markets(self) -> list[str]:
        """KRW 마켓 목록 반환 (세션 내 캐시)"""
        if self._markets_cache is None:
            resp = self._session.get(
                f"{self.BASE_URL}/v1/market/all",
                params={"isDetails": "false"},
                timeout=10,
            )
            resp.raise_for_status()
            self._markets_cache = [
                m["market"] for m in resp.json() if m["market"].startswith("KRW-")
            ]
        return self._markets_cache

    @staticmethod
    def _norm_ticker(t: dict) -> dict:
        """업비트 ticker 항목 → 빗썸 호환 포맷으로 변환"""
        trade_price = t.get("trade_price", 0)
        return {
            "closing_price": str(trade_price),
            "opening_price": str(t.get("opening_price", trade_price)),
            "max_price": str(t.get("high_price", trade_price)),
            "min_price": str(t.get("low_price", trade_price)),
            "units_traded_24H": str(t.get("acc_trade_volume_24h", 0)),
            "acc_trade_value_24H": str(t.get("acc_trade_price_24h", 0)),
            # 업비트 ticker에 호가 없음 → 현재가로 대체 (허용 오차 무시)
            "sell_price": str(trade_price),
            "buy_price": str(trade_price),
        }

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #
    def get_ticker(self, symbol: str = "ALL") -> dict:
        """현재가 정보 조회. symbol='ALL' 이면 전체 KRW 코인"""
        if symbol == "ALL":
            markets = self._get_krw_markets()
            ticker_data: dict = {}
            for i in range(0, len(markets), 100):
                batch = markets[i:i + 100]
                resp = self._session.get(
                    f"{self.BASE_URL}/v1/ticker",
                    params={"markets": ",".join(batch)},
                    timeout=15,
                )
                resp.raise_for_status()
                for t in resp.json():
                    sym = t["market"].replace("KRW-", "")
                    ticker_data[sym] = self._norm_ticker(t)
            return {"status": "0000", "data": ticker_data}
        else:
            resp = self._session.get(
                f"{self.BASE_URL}/v1/ticker",
                params={"markets": f"KRW-{symbol}"},
                timeout=10,
            )
            resp.raise_for_status()
            tickers = resp.json()
            if not tickers:
                return {"status": "9999", "data": {}}
            return {"status": "0000", "data": self._norm_ticker(tickers[0])}

    def get_orderbook(self, symbol: str) -> dict:
        """호가 정보 조회"""
        resp = self._session.get(
            f"{self.BASE_URL}/v1/orderbook",
            params={"markets": f"KRW-{symbol}"},
            timeout=10,
        )
        resp.raise_for_status()
        return {"status": "0000", "data": resp.json()}

    def get_transaction_history(self, symbol: str, count: int = 20) -> dict:
        """최근 체결 내역 조회"""
        resp = self._session.get(
            f"{self.BASE_URL}/v1/trades/ticks",
            params={"market": f"KRW-{symbol}", "count": count},
            timeout=10,
        )
        resp.raise_for_status()
        return {"status": "0000", "data": resp.json()}

    def get_candlestick(self, symbol: str, interval: str = "1h") -> dict:
        """캔들스틱 데이터 조회.

        업비트 캔들을 빗썸 호환 포맷 [ts, open, close, high, low, vol]으로 변환합니다.
        업비트는 최신순으로 반환하므로 역정렬하여 오래된 것이 앞에 오게 합니다.
        """
        unit_type, unit = _INTERVAL_MAP.get(interval, ("minutes", 60))
        if unit_type == "minutes":
            url = f"{self.BASE_URL}/v1/candles/minutes/{unit}"
        else:
            url = f"{self.BASE_URL}/v1/candles/days"

        resp = self._session.get(
            url,
            params={"market": f"KRW-{symbol}", "count": 50},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        raw.reverse()  # 업비트: 최신순 → 오래된순

        candles = [
            [
                c.get("timestamp", 0),
                c.get("opening_price", 0),
                c.get("trade_price", 0),           # close
                c.get("high_price", 0),
                c.get("low_price", 0),
                c.get("candle_acc_trade_volume", 0),
            ]
            for c in raw
        ]
        return {"status": "0000", "data": candles}

    def get_all_symbols(self) -> list[str]:
        """업비트 KRW 마켓 코인 심볼 목록"""
        return [m.replace("KRW-", "") for m in self._get_krw_markets()]

    # ------------------------------------------------------------------ #
    #  Private API (업비트 엔드포인트 — 빗썸 v2와 동일 구조)               #
    # ------------------------------------------------------------------ #
    def get_balance(self, currency: str = "ALL") -> dict:
        """잔고 조회 — 빗썸 호환 포맷으로 반환"""
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
        """미체결 주문 조회 — 빗썸 호환 포맷 반환"""
        params: dict = {"market": f"KRW-{symbol}", "state": "wait", "limit": 100}
        orders = self._v2_get("/v1/orders", params)
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
        """체결 완료 주문 조회 (state=done)"""
        params: dict = {"market": f"KRW-{symbol}", "state": "done", "limit": min(limit, 100)}
        raw = self._v2_get("/v1/orders", params)
        if not isinstance(raw, list):
            return []
        result = []
        for o in raw:
            avg_price_str = o.get("avg_price") or o.get("price") or "0"
            exec_vol_str = o.get("executed_volume") or o.get("volume") or "0"
            avg_price = float(avg_price_str) if avg_price_str else 0.0
            exec_vol = float(exec_vol_str) if exec_vol_str else 0.0
            exec_funds = avg_price * exec_vol
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
        """특정 주문 UUID로 체결 상세 조회"""
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
        """시장가 매수 (KRW 금액 기반)"""
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
            logger.error(
                f"[매수 실패] {e.response.status_code if e.response is not None else ''}: {body_text}"
            )
            return {"status": "9999", "message": body_text}

    def market_sell(self, symbol: str, units: float) -> dict:
        """시장가 매도 (코인 수량 기반)"""
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
        """주문 취소"""
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
        """보유 KRW 가용 잔고"""
        data = self.get_balance("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {data}")
        available = float(data["data"].get("available_krw", 0))
        total = float(data["data"].get("total_krw", 0))
        in_use = float(data["data"].get("in_use_krw", 0))
        logger.info(f"[잔고] available={available:,.0f} total={total:,.0f} in_use={in_use:,.0f}")
        return available

    def get_krw_balance_detail(self) -> dict:
        """KRW 잔고 상세 (available, total, in_use)"""
        data = self.get_balance("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {data}")
        return {
            "available": float(data["data"].get("available_krw", 0)),
            "total": float(data["data"].get("total_krw", 0)),
            "in_use": float(data["data"].get("in_use_krw", 0)),
        }

    def get_coin_balance(self, symbol: str) -> float:
        """특정 코인 보유 수량(가용)"""
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
        price = float(data["data"]["closing_price"])
        if price <= 0:
            raise RuntimeError(f"비정상 시세 (0원): {symbol}")
        return price
