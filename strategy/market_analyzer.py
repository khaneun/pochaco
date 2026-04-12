"""시장 데이터 수집 및 가공

빗썸 API 기반으로 가격·거래량·캔들스틱 데이터를 수집하고
기술적 분석 지표(RSI, MACD, MA, 볼린저밴드, OBV)를 산출합니다.
Binance Futures / Bybit Public API로 펀딩비·미결제약정 파생 데이터도 보완합니다.
"""
import logging
from dataclasses import dataclass, field

from core import BithumbClient
from core.derivatives_client import DerivativesClient, DerivativesData
from .technical_analyzer import TechnicalIndicators, compute_indicators

logger = logging.getLogger(__name__)

# 기술 지표 계산에 필요한 캔들 수 (MACD 26+9=35, 여유 포함)
_CANDLE_COUNT = 50


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
    candlestick_1h: list        # 최근 1시간 캔들 데이터 (최대 50개)
    technical: TechnicalIndicators = field(default_factory=TechnicalIndicators)
    derivatives: DerivativesData = field(default_factory=DerivativesData)


class MarketAnalyzer:
    """전체 코인 시장 데이터 수집"""

    def __init__(self, client: BithumbClient, derivatives: DerivativesClient | None = None):
        self._client = client
        self._derivatives = derivatives

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

    def get_coin_snapshot(
        self,
        symbol: str,
        derivatives: DerivativesData | None = None,
    ) -> CoinSnapshot:
        """특정 코인의 상세 스냅샷 수집.

        Args:
            symbol: 빗썸 심볼
            derivatives: 사전 로드된 파생 데이터 (없으면 기본값 사용)
        """
        ticker = self._client.get_ticker(symbol)
        if ticker.get("status") != "0000":
            raise RuntimeError(f"{symbol} ticker 조회 실패: {ticker}")

        d = ticker["data"]
        current_price = float(d["closing_price"])
        open_price = float(d.get("opening_price", current_price))

        # 캔들스틱 (1시간, 기술 지표 산출용 최대 50개)
        try:
            candle_resp = self._client.get_candlestick(symbol, "1h")
            candles = candle_resp.get("data", [])[-_CANDLE_COUNT:]
        except Exception:
            candles = []

        # 기술적 분석 지표 계산
        technical = compute_indicators(candles, current_price)

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
            technical=technical,
            derivatives=derivatives or DerivativesData(),
        )

    def build_market_summary(self, top_n: int = 20) -> list[CoinSnapshot]:
        """상위 N개 코인 스냅샷 목록 반환 (Agent 분석용).

        파생 데이터 클라이언트가 주입된 경우 배치 사전 로드 후 통합합니다.
        """
        symbols = self.get_top_volume_coins(top_n)

        # 파생 데이터 배치 로드 (Binance premiumIndex 1회 + 주요 OI 수집)
        deriv_map: dict[str, DerivativesData] = {}
        if self._derivatives:
            try:
                deriv_map = self._derivatives.get_batch(symbols)
                avail = sum(1 for d in deriv_map.values() if d.available)
                logger.info(f"[MarketAnalyzer] 파생 데이터 로드: {avail}/{len(symbols)}개 심볼")
            except Exception as e:
                logger.warning(f"[MarketAnalyzer] 파생 데이터 배치 조회 실패: {e}")

        snapshots = []
        for symbol in symbols:
            try:
                deriv = deriv_map.get(symbol.upper())
                snapshots.append(self.get_coin_snapshot(symbol, deriv))
            except Exception as e:
                logger.warning(f"{symbol} 스냅샷 수집 실패: {e}")
        return snapshots
