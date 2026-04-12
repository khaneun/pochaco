"""기술적 분석 지표 계산 모듈

빗썸 캔들스틱 데이터([timestamp, open, close, high, low, volume])를 기반으로
RSI, MACD, 이동평균선, 볼린저 밴드, OBV 등 핵심 기술 지표를 산출합니다.

온체인·파생 데이터(Exchange Flow, Funding Rate, OI 등)는 빗썸 API 미제공으로
사용 가능한 가격·거래량 지표를 최대한 활용합니다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ── 캔들 인덱스 (빗썸 형식) ──────────────────────────────────────── #
_TS, _OPEN, _CLOSE, _HIGH, _LOW, _VOL = 0, 1, 2, 3, 4, 5


# ── 지표 결과 데이터클래스 ───────────────────────────────────────── #

@dataclass
class TechnicalIndicators:
    """코인 1개에 대한 기술적 분석 결과"""

    # RSI (14기간)
    rsi_14: float = 50.0            # 0~100
    rsi_signal: str = "중립"         # "과매수" | "과매도" | "중립"

    # MACD (12, 26, 9)
    macd_line: float = 0.0
    macd_signal_line: float = 0.0
    macd_histogram: float = 0.0
    macd_trend: str = "중립"         # "상승" | "하락" | "골든크로스" | "데드크로스"

    # 이동평균선
    sma_5: float = 0.0
    sma_10: float = 0.0
    sma_20: float = 0.0
    ema_12: float = 0.0
    ema_26: float = 0.0
    ma_alignment: str = "혼조"       # "정배열" | "역배열" | "혼조"

    # 볼린저 밴드 (20, 2σ)
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_position: float = 0.5        # 0~1 (밴드 내 현재가 위치)
    bb_width_pct: float = 0.0       # 밴드폭 %

    # OBV & 거래량
    obv_trend: str = "횡보"          # "상승" | "하락" | "횡보"
    volume_trend: str = "일정"       # "급증" | "증가" | "감소" | "일정"
    price_volume_divergence: bool = False  # True = 가격↑ 거래량↓ (가짜 상승)

    # 종합 신호
    overall_signal: str = "중립"      # "강한매수" | "매수" | "중립" | "매도" | "강한매도"
    signal_strength: float = 50.0    # 0~100 (높을수록 매수 유리)

    # 텍스트 요약 (프롬프트용)
    summary: str = ""


# ── 헬퍼: EMA 계산 ──────────────────────────────────────────────── #

def _ema(values: list[float], period: int) -> list[float]:
    """지수이동평균(EMA) 계산. 첫 period개는 SMA로 시드."""
    if len(values) < period:
        return [sum(values) / len(values)] * len(values) if values else []

    k = 2.0 / (period + 1)
    result = [0.0] * len(values)
    # 시드: 첫 period개의 SMA
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    # 시드 이전 값은 SMA로 채움
    seed = result[period - 1]
    for i in range(period - 1):
        result[i] = seed
    return result


def _sma(values: list[float], period: int) -> float:
    """단순이동평균 — 마지막 period개 평균"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-period:]) / period


# ── RSI 계산 ─────────────────────────────────────────────────────── #

def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Relative Strength Index (Wilder 방식)"""
    if len(closes) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    # 초기 평균
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── MACD 계산 ────────────────────────────────────────────────────── #

def _calc_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """MACD Line, Signal Line, Histogram 반환"""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line_arr = [f - s for f, s in zip(ema_fast, ema_slow)]

    # Signal line = MACD의 EMA(9)
    signal_arr = _ema(macd_line_arr[slow - 1:], signal)

    if not signal_arr:
        return macd_line_arr[-1], 0.0, macd_line_arr[-1]

    m = macd_line_arr[-1]
    s = signal_arr[-1]
    return m, s, m - s


# ── 볼린저 밴드 ──────────────────────────────────────────────────── #

def _calc_bollinger(
    closes: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[float, float, float]:
    """상단, 중앙(SMA), 하단 밴드 반환"""
    if len(closes) < period:
        mid = sum(closes) / len(closes) if closes else 0
        return mid, mid, mid

    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return mid + num_std * std, mid, mid - num_std * std


# ── OBV 계산 ─────────────────────────────────────────────────────── #

def _calc_obv(closes: list[float], volumes: list[float]) -> list[float]:
    """On-Balance Volume 계산"""
    if not closes or not volumes:
        return []
    obv = [0.0]
    for i in range(1, min(len(closes), len(volumes))):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def _obv_trend(obv: list[float], lookback: int = 5) -> str:
    """최근 lookback 기간 OBV 추세 판정"""
    if len(obv) < lookback + 1:
        return "횡보"
    recent = obv[-lookback:]
    diff = recent[-1] - recent[0]
    avg_abs = sum(abs(obv[i] - obv[i - 1]) for i in range(1, len(obv))) / max(len(obv) - 1, 1)
    if avg_abs == 0:
        return "횡보"
    ratio = diff / (avg_abs * lookback)
    if ratio > 0.3:
        return "상승"
    elif ratio < -0.3:
        return "하락"
    return "횡보"


# ── 거래량 추세 ──────────────────────────────────────────────────── #

def _volume_trend(volumes: list[float], lookback: int = 6) -> str:
    """최근 거래량 추세 판정"""
    if len(volumes) < lookback + 2:
        return "일정"
    recent = volumes[-lookback:]
    earlier = volumes[-(lookback * 2):-lookback] if len(volumes) >= lookback * 2 else volumes[:lookback]
    if not earlier:
        return "일정"
    avg_recent = sum(recent) / len(recent)
    avg_earlier = sum(earlier) / len(earlier)
    if avg_earlier == 0:
        return "일정"
    ratio = avg_recent / avg_earlier
    if ratio > 2.0:
        return "급증"
    elif ratio > 1.3:
        return "증가"
    elif ratio < 0.7:
        return "감소"
    return "일정"


# ── 가격-거래량 다이버전스 (가짜 상승 감지) ──────────────────────── #

def _detect_divergence(
    closes: list[float],
    volumes: list[float],
    lookback: int = 6,
) -> bool:
    """가격 상승 + 거래량 감소 = 가짜 상승(bearish divergence) 감지"""
    if len(closes) < lookback + 1 or len(volumes) < lookback + 1:
        return False
    price_change = (closes[-1] - closes[-lookback]) / closes[-lookback] * 100
    vol_recent = sum(volumes[-lookback:]) / lookback
    vol_prior = (
        sum(volumes[-(lookback * 2):-lookback]) / lookback
        if len(volumes) >= lookback * 2
        else sum(volumes[:lookback]) / max(lookback, 1)
    )
    # 가격 1% 이상 상승했는데 거래량이 20% 이상 줄었으면 다이버전스
    return price_change > 1.0 and vol_prior > 0 and (vol_recent / vol_prior) < 0.8


# ── MA 정배열/역배열 판정 ────────────────────────────────────────── #

def _ma_alignment(sma5: float, sma10: float, sma20: float) -> str:
    """이동평균 배열 상태 판정"""
    if sma5 > sma10 > sma20 and sma5 > 0:
        return "정배열"
    elif sma5 < sma10 < sma20 and sma5 > 0:
        return "역배열"
    return "혼조"


# ── 종합 신호 산출 ───────────────────────────────────────────────── #

def _overall_signal(ti: TechnicalIndicators) -> tuple[str, float]:
    """개별 지표를 종합하여 매수/매도 신호 및 강도 산출

    Returns:
        (signal_text, strength 0~100)
    """
    score = 50.0  # 중립 기본

    # RSI (가중치 25%)
    if ti.rsi_14 < 30:
        score += 15       # 과매도 → 매수
    elif ti.rsi_14 < 40:
        score += 8
    elif ti.rsi_14 > 70:
        score -= 15       # 과매수 → 매도
    elif ti.rsi_14 > 60:
        score -= 5

    # MACD (가중치 20%)
    if ti.macd_histogram > 0 and ti.macd_trend in ("상승", "골든크로스"):
        score += 10
    elif ti.macd_histogram < 0 and ti.macd_trend in ("하락", "데드크로스"):
        score -= 10

    # MA 배열 (가중치 15%)
    if ti.ma_alignment == "정배열":
        score += 8
    elif ti.ma_alignment == "역배열":
        score -= 8

    # 볼린저밴드 (가중치 15%)
    if ti.bb_position < 0.2:
        score += 8       # 하단 근접 → 매수
    elif ti.bb_position > 0.8:
        score -= 8       # 상단 근접 → 매도

    # OBV (가중치 15%)
    if ti.obv_trend == "상승":
        score += 7
    elif ti.obv_trend == "하락":
        score -= 7

    # 가짜 상승 다이버전스 (강한 감점)
    if ti.price_volume_divergence:
        score -= 12

    # 거래량 급증 (추세 확인용)
    if ti.volume_trend == "급증":
        score += 5
    elif ti.volume_trend == "감소":
        score -= 3

    score = max(0.0, min(100.0, score))

    if score >= 75:
        signal = "강한매수"
    elif score >= 60:
        signal = "매수"
    elif score <= 25:
        signal = "강한매도"
    elif score <= 40:
        signal = "매도"
    else:
        signal = "중립"

    return signal, round(score, 1)


# ── 텍스트 요약 생성 ─────────────────────────────────────────────── #

def _build_summary(ti: TechnicalIndicators, current_price: float) -> str:
    """프롬프트에 삽입할 기술 지표 요약 (1줄)"""
    parts = [
        f"RSI={ti.rsi_14:.0f}({ti.rsi_signal})",
        f"MACD={ti.macd_trend}",
        f"MA={ti.ma_alignment}",
        f"BB위치={ti.bb_position:.0%}",
        f"OBV={ti.obv_trend}",
    ]
    if ti.price_volume_divergence:
        parts.append("⚠️거래량다이버전스")
    parts.append(f"종합={ti.overall_signal}({ti.signal_strength:.0f})")
    return " | ".join(parts)


# ── 메인 계산 함수 ───────────────────────────────────────────────── #

def compute_indicators(
    candles: list,
    current_price: float = 0.0,
) -> TechnicalIndicators:
    """빗썸 캔들스틱 데이터로부터 기술적 지표를 계산

    Args:
        candles: 빗썸 캔들 리스트 [[ts, open, close, high, low, vol], ...]
                 오래된 것이 앞, 최신이 뒤
        current_price: 현재가 (0이면 마지막 캔들 종가 사용)

    Returns:
        TechnicalIndicators 데이터클래스
    """
    if not candles or len(candles) < 5:
        return TechnicalIndicators()

    # 데이터 추출
    closes: list[float] = []
    volumes: list[float] = []
    for c in candles:
        try:
            cl = float(c[_CLOSE]) if len(c) > _CLOSE else 0.0
            vo = float(c[_VOL]) if len(c) > _VOL else 0.0
            if cl > 0:
                closes.append(cl)
                volumes.append(vo)
        except (ValueError, TypeError, IndexError):
            continue

    if len(closes) < 5:
        return TechnicalIndicators()

    if current_price <= 0:
        current_price = closes[-1]

    ti = TechnicalIndicators()

    # ── RSI ──
    ti.rsi_14 = round(_calc_rsi(closes, 14), 1)
    if ti.rsi_14 >= 70:
        ti.rsi_signal = "과매수"
    elif ti.rsi_14 <= 30:
        ti.rsi_signal = "과매도"
    else:
        ti.rsi_signal = "중립"

    # ── MACD ──
    m, s, h = _calc_macd(closes, 12, 26, 9)
    ti.macd_line = round(m, 2)
    ti.macd_signal_line = round(s, 2)
    ti.macd_histogram = round(h, 2)

    # MACD 추세 판정 (최근 2개 히스토그램 비교)
    if len(closes) >= 28:
        closes_prev = closes[:-1]
        m_prev, s_prev, h_prev = _calc_macd(closes_prev, 12, 26, 9)
        if h_prev <= 0 < h:
            ti.macd_trend = "골든크로스"
        elif h_prev >= 0 > h:
            ti.macd_trend = "데드크로스"
        elif h > 0:
            ti.macd_trend = "상승"
        else:
            ti.macd_trend = "하락"
    else:
        ti.macd_trend = "상승" if h > 0 else "하락" if h < 0 else "중립"

    # ── 이동평균선 ──
    ti.sma_5 = round(_sma(closes, 5), 2)
    ti.sma_10 = round(_sma(closes, 10), 2)
    ti.sma_20 = round(_sma(closes, 20), 2)

    ema12_arr = _ema(closes, 12)
    ema26_arr = _ema(closes, 26)
    ti.ema_12 = round(ema12_arr[-1], 2) if ema12_arr else 0.0
    ti.ema_26 = round(ema26_arr[-1], 2) if ema26_arr else 0.0

    ti.ma_alignment = _ma_alignment(ti.sma_5, ti.sma_10, ti.sma_20)

    # ── 볼린저 밴드 ──
    upper, mid, lower = _calc_bollinger(closes, 20, 2.0)
    ti.bb_upper = round(upper, 2)
    ti.bb_middle = round(mid, 2)
    ti.bb_lower = round(lower, 2)
    band_width = upper - lower
    if band_width > 0:
        ti.bb_position = round(
            max(0.0, min(1.0, (current_price - lower) / band_width)),
            3,
        )
        ti.bb_width_pct = round(band_width / mid * 100, 2) if mid > 0 else 0.0
    else:
        ti.bb_position = 0.5
        ti.bb_width_pct = 0.0

    # ── OBV ──
    obv = _calc_obv(closes, volumes)
    ti.obv_trend = _obv_trend(obv, lookback=5)

    # ── 거래량 추세 ──
    ti.volume_trend = _volume_trend(volumes, lookback=6)

    # ── 가격-거래량 다이버전스 ──
    ti.price_volume_divergence = _detect_divergence(closes, volumes, lookback=6)

    # ── 종합 신호 ──
    ti.overall_signal, ti.signal_strength = _overall_signal(ti)

    # ── 텍스트 요약 ──
    ti.summary = _build_summary(ti, current_price)

    return ti
