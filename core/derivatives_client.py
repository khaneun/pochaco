"""파생상품 시장 데이터 클라이언트

Binance Futures Public API(무료, 인증 불필요)를 주 소스로,
Bybit Public API를 보조 소스로 활용하여 파생 지표를 수집합니다.

수집 데이터:
- 펀딩비 (Funding Rate, 8h): 시장 롱/숏 편중 판단
- 미결제약정 (Open Interest): 시장 참여 강도 및 추세 지속성 판단

빗썸 현물 심볼을 USDT 선물 심볼로 자동 매핑합니다.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# ── 상수 ─────────────────────────────────────────────────────────── #
_BINANCE_BASE = "https://fapi.binance.com"
_BYBIT_BASE   = "https://api.bybit.com"
_TIMEOUT      = 5       # 요청 타임아웃(초)
_CACHE_TTL    = 60      # 캐시 유효 시간(초)

# 빗썸 심볼 → USDT 무기한 선물 심볼
_SYMBOL_MAP: dict[str, str] = {
    "BTC":    "BTCUSDT",
    "ETH":    "ETHUSDT",
    "XRP":    "XRPUSDT",
    "SOL":    "SOLUSDT",
    "DOGE":   "DOGEUSDT",
    "ADA":    "ADAUSDT",
    "AVAX":   "AVAXUSDT",
    "DOT":    "DOTUSDT",
    "LINK":   "LINKUSDT",
    "MATIC":  "MATICUSDT",
    "POL":    "POLUSDT",
    "TRX":    "TRXUSDT",
    "LTC":    "LTCUSDT",
    "BCH":    "BCHUSDT",
    "UNI":    "UNIUSDT",
    "ATOM":   "ATOMUSDT",
    "ETC":    "ETCUSDT",
    "NEAR":   "NEARUSDT",
    "SUI":    "SUIUSDT",
    "APT":    "APTUSDT",
    "OP":     "OPUSDT",
    "ARB":    "ARBUSDT",
    "PEPE":   "PEPEUSDT",
    "WIF":    "WIFUSDT",
    "BONK":   "BONKUSDT",
    "FLOKI":  "FLOKIUSDT",
    "FIL":    "FILUSDT",
    "HBAR":   "HBARUSDT",
    "ALGO":   "ALGOUSDT",
    "VET":    "VETUSDT",
    "XLM":    "XLMUSDT",
    "EOS":    "EOSUSDT",
    "SAND":   "SANDUSDT",
    "MANA":   "MANAUSDT",
    "CHZ":    "CHZUSDT",
    "SHIB":   "SHIBUSDT",
    "SEI":    "SEIUSDT",
    "TIA":    "TIAUSDT",
    "INJ":    "INJUSDT",
    "FTM":    "FTMUSDT",
    "KAS":    "KASUSDT",
    "RENDER": "RENDERUSDT",
    "MKR":    "MKRUSDT",
    "AAVE":   "AAVEUSDT",
    "GRT":    "GRTUSDT",
    "SNX":    "SNXUSDT",
    "CRV":    "CRVUSDT",
    "STX":    "STXUSDT",
    "BLUR":   "BLURUSDT",
    "PENDLE": "PENDLEUSDT",
    "1INCH":  "1INCHUSDT",
}

# OI 이력(1h 변화율) 조회 대상 — 주요 코인만 (API 부하 감소)
_OI_HIST_SYMBOLS: set[str] = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}


# ── 결과 데이터클래스 ─────────────────────────────────────────────── #

@dataclass
class DerivativesData:
    """코인 1개 파생상품 시장 데이터"""

    funding_rate: float = 0.0       # 8h 펀딩비 (%, 양수=롱우세 / 음수=숏우세)
    funding_signal: str = "중립"     # "극단롱과열" | "롱과열" | "중립" | "숏과열"
    open_interest_usd: float = 0.0  # 미결제약정 (USD 명목)
    oi_change_pct: float = 0.0      # 1h 전 대비 OI 변화율 (%)
    oi_trend: str = "횡보"           # "급증" | "증가" | "감소" | "급감" | "횡보"
    available: bool = False         # True = 정상 수집
    source: str = ""                # "binance" | "bybit" | ""
    summary: str = ""               # 텍스트 요약 (프롬프트용)


# ── 클라이언트 ────────────────────────────────────────────────────── #

class DerivativesClient:
    """Binance Futures + Bybit Public API 파생 데이터 클라이언트.

    주요 메서드:
        prefetch(symbols)         : 배치 사전 로드 (build_market_summary 전 호출 권장)
        get_derivatives(symbol)   : 코인 1개 파생 데이터 반환
        get_batch(symbols)        : 여러 코인 일괄 조회
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "pochaco/1.0",
        })
        self._lock = threading.Lock()

        # 전체 펀딩비 캐시 (Binance premiumIndex 배치 결과)
        # {futures_symbol: rate_pct}  e.g. {"BTCUSDT": 0.01}
        self._funding_cache: dict[str, float] = {}
        self._funding_ts: float = 0.0

        # OI 캐시: {futures_symbol: (timestamp, oi_usd, oi_change_pct)}
        self._oi_cache: dict[str, tuple[float, float, float]] = {}

        # 최종 결과 캐시: {bithumb_symbol: (timestamp, DerivativesData)}
        self._result_cache: dict[str, tuple[float, DerivativesData]] = {}

    # ── 공개 메서드 ────────────────────────────────────────────────── #

    def prefetch(self, bithumb_symbols: list[str]) -> None:
        """파생 데이터 사전 로드.

        1. Binance premiumIndex 배치 호출 (전체 펀딩비 1회 수집)
        2. 주요 코인(_OI_HIST_SYMBOLS) OI 이력 순차 수집

        Args:
            bithumb_symbols: 조회할 빗썸 심볼 목록
        """
        self._load_all_funding_rates()

        for sym in bithumb_symbols:
            upper = sym.upper()
            if upper in _OI_HIST_SYMBOLS:
                futures_sym = _SYMBOL_MAP.get(upper)
                if futures_sym:
                    self._load_oi_hist(futures_sym)

    def get_derivatives(self, bithumb_symbol: str) -> DerivativesData:
        """코인 1개 파생 데이터 반환 (캐시 우선).

        Args:
            bithumb_symbol: 빗썸 심볼 (대소문자 무관)

        Returns:
            DerivativesData (available=False 이면 미지원 심볼)
        """
        sym = bithumb_symbol.upper()

        # 결과 캐시 확인
        with self._lock:
            if sym in self._result_cache:
                ts, cached = self._result_cache[sym]
                if time.time() - ts < _CACHE_TTL:
                    return cached

        futures_sym = _SYMBOL_MAP.get(sym)
        if not futures_sym:
            return DerivativesData(available=False, summary="선물 미지원")

        # 펀딩비 (배치 캐시 우선)
        with self._lock:
            funding_rate = self._funding_cache.get(futures_sym)

        if funding_rate is None:
            # 캐시 미스 → Bybit 폴백
            result = self._fetch_bybit_single(futures_sym)
        else:
            oi_usd, oi_change_pct = self._get_oi_cached(futures_sym)
            result = self._build_result(funding_rate, oi_usd, oi_change_pct, "binance")

        with self._lock:
            self._result_cache[sym] = (time.time(), result)
        return result

    def get_batch(self, bithumb_symbols: list[str]) -> dict[str, DerivativesData]:
        """여러 코인 파생 데이터 일괄 조회.

        Args:
            bithumb_symbols: 빗썸 심볼 목록

        Returns:
            {대문자_심볼: DerivativesData}
        """
        self.prefetch(bithumb_symbols)
        result: dict[str, DerivativesData] = {}
        for sym in bithumb_symbols:
            try:
                result[sym.upper()] = self.get_derivatives(sym)
            except Exception as e:
                logger.warning(f"[DerivativesClient] {sym} 조회 실패: {e}")
                result[sym.upper()] = DerivativesData(available=False)
        return result

    # ── 내부 데이터 로드 ──────────────────────────────────────────── #

    def _load_all_funding_rates(self) -> None:
        """Binance /fapi/v1/premiumIndex 배치 호출로 전체 펀딩비 로드."""
        with self._lock:
            if time.time() - self._funding_ts < _CACHE_TTL and self._funding_cache:
                return

        try:
            resp = self._session.get(
                f"{_BINANCE_BASE}/fapi/v1/premiumIndex",
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            items = resp.json()

            rates: dict[str, float] = {}
            for item in items:
                if isinstance(item, dict) and "symbol" in item and "lastFundingRate" in item:
                    rates[item["symbol"]] = float(item["lastFundingRate"]) * 100.0

            with self._lock:
                self._funding_cache = rates
                self._funding_ts = time.time()

            logger.debug(f"[DerivativesClient] 펀딩비 배치 로드: {len(rates)}개 심볼")

        except Exception as e:
            logger.warning(f"[DerivativesClient] 펀딩비 배치 조회 실패: {e}")

    def _load_oi_hist(self, futures_sym: str) -> tuple[float, float]:
        """Binance OI 이력(1h 2건) 조회 → 변화율 계산.

        Args:
            futures_sym: 선물 심볼 (예: "BTCUSDT")

        Returns:
            (oi_usd, oi_change_pct)
        """
        with self._lock:
            if futures_sym in self._oi_cache:
                ts, oi, change = self._oi_cache[futures_sym]
                if time.time() - ts < _CACHE_TTL:
                    return oi, change

        try:
            # 현재 OI (코인 수량 기준)
            oi_resp = self._session.get(
                f"{_BINANCE_BASE}/fapi/v1/openInterest",
                params={"symbol": futures_sym},
                timeout=_TIMEOUT,
            )
            oi_resp.raise_for_status()
            oi_current = float(oi_resp.json().get("openInterest", 0))

            # OI 이력 (USD 기준, 1h 2건 — 현재·1h전)
            oi_change_pct = 0.0
            try:
                hist_resp = self._session.get(
                    f"{_BINANCE_BASE}/futures/data/openInterestHist",
                    params={"symbol": futures_sym, "period": "1h", "limit": 2},
                    timeout=_TIMEOUT,
                )
                hist_resp.raise_for_status()
                hist = hist_resp.json()
                if len(hist) >= 2:
                    prev = float(hist[0].get("sumOpenInterest", oi_current))
                    curr = float(hist[1].get("sumOpenInterest", oi_current))
                    oi_change_pct = (curr - prev) / prev * 100 if prev > 0 else 0.0
            except Exception:
                pass

            with self._lock:
                self._oi_cache[futures_sym] = (time.time(), oi_current, oi_change_pct)
            return oi_current, oi_change_pct

        except Exception as e:
            logger.debug(f"[DerivativesClient] {futures_sym} OI 조회 실패: {e}")
            return 0.0, 0.0

    def _get_oi_cached(self, futures_sym: str) -> tuple[float, float]:
        """OI 캐시 반환 (없으면 로드 시도)."""
        with self._lock:
            if futures_sym in self._oi_cache:
                ts, oi, change = self._oi_cache[futures_sym]
                if time.time() - ts < _CACHE_TTL:
                    return oi, change
        # 주요 심볼이면 즉시 로드
        bithumb_sym = next(
            (k for k, v in _SYMBOL_MAP.items() if v == futures_sym), None
        )
        if bithumb_sym and bithumb_sym in _OI_HIST_SYMBOLS:
            return self._load_oi_hist(futures_sym)
        return 0.0, 0.0

    def _fetch_bybit_single(self, futures_sym: str) -> DerivativesData:
        """Bybit 단일 심볼 조회 (Binance 실패 시 폴백).

        Args:
            futures_sym: 선물 심볼 (예: "BTCUSDT")

        Returns:
            DerivativesData
        """
        try:
            resp = self._session.get(
                f"{_BYBIT_BASE}/v5/market/tickers",
                params={"category": "linear", "symbol": futures_sym},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            items = resp.json().get("result", {}).get("list", [])
            if not items:
                return DerivativesData(available=False)

            item = items[0]
            funding_rate = float(item.get("fundingRate", 0)) * 100.0
            oi_usd = float(item.get("openInterest", 0))
            return self._build_result(funding_rate, oi_usd, 0.0, "bybit")

        except Exception as e:
            logger.debug(f"[DerivativesClient] Bybit {futures_sym} 실패: {e}")
            return DerivativesData(available=False)

    # ── 빌더 헬퍼 ────────────────────────────────────────────────── #

    @staticmethod
    def _build_result(
        funding_rate: float,
        oi_usd: float,
        oi_change_pct: float,
        source: str,
    ) -> DerivativesData:
        """펀딩비·OI 수치로 DerivativesData 생성."""

        # 펀딩비 신호 (8h 기준 %)
        if funding_rate > 0.10:
            funding_signal = "극단롱과열"
        elif funding_rate > 0.05:
            funding_signal = "롱과열"
        elif funding_rate < -0.03:
            funding_signal = "숏과열"
        else:
            funding_signal = "중립"

        # OI 변화 추세
        if oi_change_pct >= 3.0:
            oi_trend = "급증"
        elif oi_change_pct >= 1.0:
            oi_trend = "증가"
        elif oi_change_pct <= -3.0:
            oi_trend = "급감"
        elif oi_change_pct <= -1.0:
            oi_trend = "감소"
        else:
            oi_trend = "횡보"

        # 텍스트 요약
        parts = [f"펀딩비 {funding_rate:+.3f}%({funding_signal})"]
        if oi_usd > 0:
            oi_b = oi_usd / 1e9
            if oi_b >= 0.1:
                parts.append(f"OI {oi_b:.1f}B({oi_trend})")
        summary = " ".join(parts)

        return DerivativesData(
            funding_rate=round(funding_rate, 4),
            funding_signal=funding_signal,
            open_interest_usd=oi_usd,
            oi_change_pct=round(oi_change_pct, 2),
            oi_trend=oi_trend,
            available=True,
            source=source,
            summary=summary,
        )
