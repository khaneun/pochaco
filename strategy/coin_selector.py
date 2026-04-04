"""종목 선정 전담 Agent — 변동성·거래량·모멘텀 기반 사전 필터링

문제: 24h 등락폭이 ±1% 수준인 코인에 2~3% 익절을 기대하는 것은 비현실적.
해결: AI에게 넘기기 전에 수학적으로 필터링 + 스코어링.

동작:
  1. 변동폭(고저차) < 목표 익절 × 1.5 → 제외 (변동성 부족)
  2. 거래대금 < 50억 → 제외 (유동성 부족)
  3. 하락 추세(24h 변동 < -1% 또는 현재가가 저가 근처) → 제외
  4. 나머지를 [변동성 × 모멘텀 × 거래량] 가중 스코어로 정렬
  5. 상위 10개만 AI에게 전달 → AI는 검증된 후보풀에서만 선택
"""
import logging
from dataclasses import dataclass

from .market_analyzer import CoinSnapshot

logger = logging.getLogger(__name__)

# 기본 필터링 기준
_MIN_VOLUME_KRW = 5_000_000_000    # 50억원
_MIN_CHANGE_PCT = -1.0              # 24h 변동률 하한 (이하면 하락 추세로 간주)
_TOP_CANDIDATES = 10                # AI에 전달할 최대 후보 수


@dataclass
class CoinScore:
    """코인 사전 분석 결과"""
    symbol: str
    total_score: float
    volatility_pct: float      # 24h 고저 변동폭 %
    price_position: float      # 0=저가, 100=고가 (현재가 위치)
    momentum: float            # 캔들 기반 모멘텀 (-10 ~ +10)
    volume_score: float        # 거래량 점수
    reason: str                # 선별/제외 이유


class CoinSelector:
    """AI 종목 선정 전 사전 필터링 및 스코어링 Agent

    MarketAnalyzer가 수집한 CoinSnapshot 목록을 받아서
    변동성·거래량·모멘텀을 분석하고, 매매 가능한 상위 후보만 반환합니다.
    """

    def filter_and_rank(
        self,
        snapshots: list[CoinSnapshot],
        target_tp: float = 2.0,
    ) -> tuple[list[CoinSnapshot], list[CoinScore]]:
        """매매 가능한 코인만 필터링 + 스코어링

        Args:
            snapshots: 전체 코인 스냅샷 목록
            target_tp: 현재 목표 익절% (StrategyOptimizer 기준)

        Returns:
            (필터링된 스냅샷 목록, 스코어 목록) — AI 프롬프트에 함께 전달
        """
        scored: list[tuple[CoinSnapshot, CoinScore]] = []
        rejected = 0

        for s in snapshots:
            # ── 변동폭 계산 ──
            if s.high_price <= 0 or s.low_price <= 0:
                rejected += 1
                continue
            volatility_pct = (s.high_price - s.low_price) / s.low_price * 100

            # ── 필터 1: 변동폭 부족 ──
            min_volatility = target_tp * 1.5
            if volatility_pct < min_volatility:
                logger.debug(
                    f"  [제외] {s.symbol}: 변동폭 {volatility_pct:.1f}% < "
                    f"필요 {min_volatility:.1f}% (TP {target_tp}%×1.5)"
                )
                rejected += 1
                continue

            # ── 필터 2: 거래대금 부족 ──
            if s.volume_krw_24h < _MIN_VOLUME_KRW:
                logger.debug(
                    f"  [제외] {s.symbol}: 거래대금 {s.volume_krw_24h/1e8:.0f}억 < 50억"
                )
                rejected += 1
                continue

            # ── 필터 3: 하락 추세 ──
            if s.change_pct_24h < _MIN_CHANGE_PCT:
                logger.debug(
                    f"  [제외] {s.symbol}: 24h 변동 {s.change_pct_24h:+.1f}% (하락 추세)"
                )
                rejected += 1
                continue

            # ── 현재가 위치 (0=저가, 100=고가) ──
            price_range = s.high_price - s.low_price
            price_position = (
                (s.current_price - s.low_price) / price_range * 100
                if price_range > 0 else 50.0
            )

            # 현재가가 저가 근처면 (하위 20%) → 하락 후 바닥일 수 있으니 감점
            if price_position < 20 and s.change_pct_24h < 0:
                logger.debug(
                    f"  [제외] {s.symbol}: 저가 근처({price_position:.0f}%) + 하락 중"
                )
                rejected += 1
                continue

            # ── 캔들 모멘텀 분석 ──
            momentum = self._analyze_candle_momentum(s.candlestick_1h)

            # ── 스코어링 ──
            # 변동폭: 클수록 좋음 (기회 많음)
            vol_score = min(volatility_pct / 2.0, 5.0)  # 0~5점
            # 상승 추세: 양수일수록 좋음
            trend_score = min(max(s.change_pct_24h, 0) * 2, 5.0)  # 0~5점
            # 모멘텀: 최근 상승 추세
            mom_score = max(momentum, 0) / 2.0  # 0~5점
            # 거래량: 충분하면 가산
            v_score = min(s.volume_krw_24h / 2e10, 3.0)  # 0~3점
            # 현재가 위치: 고가 근처이되 너무 꼭대기가 아닌 60~80% 최적
            pos_score = 2.0 if 50 <= price_position <= 80 else (
                1.0 if 30 <= price_position <= 90 else 0.0
            )

            total = (
                vol_score * 0.25
                + trend_score * 0.25
                + mom_score * 0.25
                + v_score * 0.15
                + pos_score * 0.10
            )

            score = CoinScore(
                symbol=s.symbol,
                total_score=round(total, 2),
                volatility_pct=round(volatility_pct, 2),
                price_position=round(price_position, 1),
                momentum=round(momentum, 2),
                volume_score=round(v_score, 2),
                reason=self._make_reason(volatility_pct, s.change_pct_24h, momentum, price_position),
            )
            scored.append((s, score))

        # 스코어 상위 정렬
        scored.sort(key=lambda x: x[1].total_score, reverse=True)

        # 상위 N개만 반환
        top = scored[:_TOP_CANDIDATES]
        result_snapshots = [s for s, _ in top]
        result_scores = [sc for _, sc in top]

        logger.info(
            f"[CoinSelector] {len(snapshots)}개 중 {len(result_snapshots)}개 선별 "
            f"(제외: {rejected}개, TP 기준: {target_tp}%)"
        )
        for sc in result_scores[:5]:
            logger.info(
                f"  #{result_scores.index(sc)+1} {sc.symbol}: "
                f"score={sc.total_score:.2f} 변동폭={sc.volatility_pct:.1f}% "
                f"모멘텀={sc.momentum:+.1f} 위치={sc.price_position:.0f}%"
            )

        return result_snapshots, result_scores

    # ---------------------------------------------------------------- #
    #  캔들 모멘텀 분석                                                   #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _analyze_candle_momentum(candles: list) -> float:
        """최근 1시간 캔들 기반 모멘텀 분석

        빗썸 캔들 형식: [timestamp, open, close, high, low, volume]
        반환: -10.0 ~ +10.0 (양수 = 상승 모멘텀)
        """
        if not candles or len(candles) < 3:
            return 0.0

        try:
            # 최근 6개 캔들의 종가 추출
            recent = candles[-6:] if len(candles) >= 6 else candles[-3:]
            closes = []
            for c in recent:
                # 빗썸 형식: [timestamp, open, close, high, low, volume]
                close_val = float(c[2]) if len(c) > 2 else 0
                if close_val > 0:
                    closes.append(close_val)

            if len(closes) < 2:
                return 0.0

            # 전체 추세: 첫 종가 → 마지막 종가 변화율
            overall = (closes[-1] - closes[0]) / closes[0] * 100

            # 최근 3개 캔들의 연속 상승/하락 카운트
            streak = 0
            for i in range(1, min(4, len(closes))):
                if closes[-i] > closes[-i-1] if i < len(closes) else False:
                    streak += 1
                elif closes[-i] < closes[-i-1] if i < len(closes) else False:
                    streak -= 1

            # 모멘텀 = 추세 + 연속성 가중
            momentum = overall * 3.0 + streak * 1.5
            return max(-10.0, min(10.0, momentum))

        except (IndexError, ValueError, TypeError):
            return 0.0

    @staticmethod
    def _make_reason(volatility: float, change: float, momentum: float, position: float) -> str:
        """코인 선별 이유 생성"""
        parts = []
        if volatility > 8:
            parts.append("고변동성")
        elif volatility > 4:
            parts.append("적정 변동성")

        if change > 3:
            parts.append("강한 상승세")
        elif change > 1:
            parts.append("상승 추세")
        elif change > 0:
            parts.append("약간 상승")

        if momentum > 3:
            parts.append("상승 모멘텀 강함")
        elif momentum > 0:
            parts.append("모멘텀 양호")

        if 50 <= position <= 80:
            parts.append("매수 적정 위치")
        elif position > 85:
            parts.append("고가 근접 주의")

        return ", ".join(parts) if parts else "일반"
