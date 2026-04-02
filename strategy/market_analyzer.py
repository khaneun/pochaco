"""시장 데이터 수집 및 가공"""
import logging
from dataclasses import dataclass

from core import BithumbClient

logger = logging.getLogger(__name__)


@dataclass
class CoinSnapshot:
    symbol: str
    current_price: float
    open_price: float
    high_price: float
    low_price: float
    volume_24h: float           # 24h 거래량 (코인 수량)
    volume_krw_24h: float       # 24h 거래대금 (KRW)
    change_pct_24h: float       # 24h 등락률(%)
    ask_price: float            # 최우선 매도호가
    bid_price: float            # 최우선 매수호가
    candlestick_1h: list        # 최근 24개 1시간 캔들 데이터


class MarketAnalyzer:
    """전체 코인 시장 데이터 수집"""

    def __init__(self, client: BithumbClient):
        self._client = client

    def get_all_tickers(self) -> dict[str, dict]:
        """전체 코인 ticker 데이터 반환"""
        data = self._client.get_ticker("ALL")
        if data.get("status") != "0000":
            raise RuntimeError(f"전체 ticker 조회 실패: {data}")
        tickers = {k: v for k, v in data["data"].items() if k != "date"}
        return tickers

    def get_top_volume_coins(self, top_n: int = 30) -> list[str]:
        """거래대금 상위 N개 코인 심볼 반환"""
        tickers = self.get_all_tickers()
        ranked = sorted(
            tickers.items(),
            key=lambda x: float(x[1].get("acc_trade_value_24H", 0)),
            reverse=True,
        )
        return [symbol for symbol, _ in ranked[:top_n]]

    def get_coin_snapshot(self, symbol: str) -> CoinSnapshot:
        """특정 코인의 상세 스냅샷 수집"""
        ticker = self._client.get_ticker(symbol)
        if ticker.get("status") != "0000":
            raise RuntimeError(f"{symbol} ticker 조회 실패: {ticker}")

        d = ticker["data"]
        current_price = float(d["closing_price"])
        open_price = float(d.get("opening_price", current_price))

        # 캔들스틱 (1시간, 최근 24개)
        try:
            candle_resp = self._client.get_candlestick(symbol, "1h")
            candles = candle_resp.get("data", [])[-24:]
        except Exception:
            candles = []

        return CoinSnapshot(
            symbol=symbol,
            current_price=current_price,
            open_price=open_price,
            high_price=float(d.get("max_price", current_price)),
            low_price=float(d.get("min_price", current_price)),
            volume_24h=float(d.get("units_traded_24H", 0)),
            volume_krw_24h=float(d.get("acc_trade_value_24H", 0)),
            change_pct_24h=(current_price - open_price) / open_price * 100
            if open_price > 0
            else 0.0,
            ask_price=float(d.get("sell_price", current_price)),
            bid_price=float(d.get("buy_price", current_price)),
            candlestick_1h=candles,
        )

    def build_market_summary(self, top_n: int = 20) -> list[CoinSnapshot]:
        """상위 N개 코인 스냅샷 목록 반환 (Agent 분석용)"""
        symbols = self.get_top_volume_coins(top_n)
        snapshots = []
        for symbol in symbols:
            try:
                snapshots.append(self.get_coin_snapshot(symbol))
            except Exception as e:
                logger.warning(f"{symbol} 스냅샷 수집 실패: {e}")
        return snapshots
