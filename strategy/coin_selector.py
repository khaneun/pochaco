"""종목 선정 전담 Agent — 변동성·거래량·모멘텀 기반 사전 필터링

문제: 24h 등락폭이 ±1% 수준인 코인에 2~3% 익절을 기대하는 것은 비현실적.
해결: AI에게 넘기기 전에 수학적으로 필터링 + 스코어링.

동작:
  1. 변동폭(고저차) < 목표 익절 × 1.5 → 제외 (변동성 부족)
  2. 거래대금 < 50억 → 제외 (유동성 부족)
  3. 하락 추세 → 제외 (다중 기준 복합 판정 — 아래 _check_downtrend 참고)
  4. 나머지를 [변동성 × 모멘텀 × 거래량] 가중 스코어로 정렬
  5. 상위 10개만 AI에게 전달 → AI는 검증된 후보풀에서만 선택

하락 추세 판정 기준 (하나라도 해당 시 제외):
  A. 24h 변동률 < -2.0%               — 강한 일봉 하락
  B. 최근 3h 변화율 < -1.5%           — 현재 단기 급락 중
  C. 최근 4캔들 중 3개 이상 하락       — 단, 24h < 1.0% 조건 병행 (강한 상승 조정은 허용)
  D. 24h < -1.0% 이고 최근 2h도 하락  — 중단기 동반 하락 확인
  E. 저가 근처(하위 15%) + 최근 2h 하락 — 바닥 다이빙
"""
import logging
from dataclasses import dataclass

from .market_analyzer import CoinSnapshot

logger = logging.getLogger(__name__)

# 필터링 기준
_MIN_VOLUME_KRW     = 5_000_000_000  # 50억원 — 유동성 하한
_TOP_CANDIDATES     = 20             # AI에 전달할 최대 후보 수 (8개 포트폴리오 구성용)

# 하락 추세 판정 임계값
_DOWN_24H_STRONG    = -2.0   # 24h 이 이하면 무조건 제외
_DOWN_24H_MILD      = -1.0   # 단기 확인과 병행 시 제외
_DOWN_3H_THRESHOLD  = -1.5   # 최근 3h 이 이하면 제외
_DOWN_2H_ANY        =  0.0   # 24h mild 하락 + 단기 이 이하면 제외
_DOWN_CONSEC_RATIO  =  3     # 연속 하락 캔들 수 (최근 4개 중)
_LOW_POSITION_PCT   = 15.0   # 저가 근처 기준 (%)


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
        cooldown_symbols: set[str] | None = None,
        min_candidates: int = 8,
    ) -> tuple[list[CoinSnapshot], list[CoinScore]]:
        """매매 가능한 코인만 필터링 + 스코어링

        포트폴리오 8개 구성을 위해 min_candidates 미만이면
        변동폭 조건만 완화한 2차 패스로 보충합니다.
        하락추세·거래대금 필터는 2차에서도 항상 유지됩니다.

        Args:
            snapshots: 전체 코인 스냅샷 목록
            target_tp: 현재 목표 익절% (StrategyOptimizer 기준)
            cooldown_symbols: 쿨다운 중인 심볼 집합 (재매수 금지)
            min_candidates: 최소 반환 개수 (부족 시 변동폭 조건 완화)

        Returns:
            (필터링된 스냅샷 목록, 스코어 목록) — AI 프롬프트에 함께 전달
        """
        cooldown_symbols = cooldown_symbols or set()

        # ── 1차 패스: 전체 필터 (변동폭 TP×1.5 포함) ──
        scored, rejected = self._run_filter_pass(
            snapshots, cooldown_symbols, target_tp, vol_multiplier=1.5,
        )
        scored.sort(key=lambda x: x[1].total_score, reverse=True)

        # ── 2차 패스: 1차 통과 코인이 min_candidates 미만이면 변동폭 완화 보충 ──
        if len(scored) < min_candidates:
            passed_symbols = {sc.symbol for _, sc in scored}
            # 2차: 변동폭 조건 TP×0.5 (하락추세·거래대금은 유지)
            extra, _ = self._run_filter_pass(
                [s for s in snapshots if s.symbol not in passed_symbols],
                cooldown_symbols, target_tp, vol_multiplier=0.5,
            )
            extra.sort(key=lambda x: x[1].total_score, reverse=True)
            need = min_candidates - len(scored)
            scored.extend(extra[:need])
            scored.sort(key=lambda x: x[1].total_score, reverse=True)
            if extra:
                logger.info(
                    f"[CoinSelector] 2차 패스(변동폭 완화): "
                    f"{len(extra[:need])}개 추가 보충"
                )

        # 상위 N개만 반환
        top = scored[:_TOP_CANDIDATES]
        result_snapshots = [s for s, _ in top]
        result_scores = [sc for _, sc in top]

        logger.info(
            f"[CoinSelector] {len(snapshots)}개 중 {len(result_snapshots)}개 선별 "
            f"(제외: {len(snapshots) - len(result_snapshots)}개, TP 기준: {target_tp}%)"
        )
        for sc in result_scores[:5]:
            logger.info(
                f"  #{result_scores.index(sc)+1} {sc.symbol}: "
                f"score={sc.total_score:.2f} 변동폭={sc.volatility_pct:.1f}% "
                f"모멘텀={sc.momentum:+.1f} 위치={sc.price_position:.0f}%"
            )

        return result_snapshots, result_scores

    def _run_filter_pass(
        self,
        snapshots: list[CoinSnapshot],
        cooldown_symbols: set[str],
        target_tp: float,
        vol_multiplier: float,
    ) -> tuple[list[tuple[CoinSnapshot, "CoinScore"]], int]:
        """단일 필터 패스 실행.

        Args:
            snapshots: 대상 스냅샷 목록
            cooldown_symbols: 쿨다운 심볼
            target_tp: 목표 익절%
            vol_multiplier: 변동폭 조건 배수 (target_tp × multiplier)

        Returns:
            (통과 코인 리스트, 제외된 개수)
        """
        scored: list[tuple[CoinSnapshot, CoinScore]] = []
        rejected = 0
        min_volatility = target_tp * vol_multiplier

        for s in snapshots:
            if s.symbol in cooldown_symbols:
                rejected += 1
                continue

            if s.high_price <= 0 or s.low_price <= 0:
                rejected += 1
                continue
            volatility_pct = (s.high_price - s.low_price) / s.low_price * 100

            # 변동폭 필터
            if volatility_pct < min_volatility:
                logger.debug(
                    f"  [제외] {s.symbol}: 변동폭 {volatility_pct:.1f}% < "
                    f"필요 {min_volatility:.1f}% (TP {target_tp}%×{vol_multiplier})"
                )
                rejected += 1
                continue

            # 거래대금 필터
            if s.volume_krw_24h < _MIN_VOLUME_KRW:
                logger.debug(
                    f"  [제외] {s.symbol}: 거래대금 {s.volume_krw_24h/1e8:.0f}억 < 50억"
                )
                rejected += 1
                continue

            # 하락 추세 필터 (항상 적용)
            is_down, down_reason = self._check_downtrend(s)
            if is_down:
                logger.debug(f"  [제외] {s.symbol}: {down_reason}")
                rejected += 1
                continue

            # 현재가 위치
            price_range = s.high_price - s.low_price
            price_position = (
                (s.current_price - s.low_price) / price_range * 100
                if price_range > 0 else 50.0
            )

            momentum = self._analyze_candle_momentum(s.candlestick_1h)

            vol_score   = min(volatility_pct / 2.0, 5.0)
            trend_score = min(max(s.change_pct_24h, 0) * 2, 5.0)
            mom_score   = max(momentum, 0) / 2.0
            v_score     = min(s.volume_krw_24h / 2e10, 3.0)
            pos_score   = 2.0 if 50 <= price_position <= 80 else (
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
                reason=self._make_reason(
                    volatility_pct, s.change_pct_24h, momentum, price_position
                ),
            )
            scored.append((s, score))

        return scored, rejected

    # ---------------------------------------------------------------- #
    #  하락 추세 판정 (다중 기준)                                          #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _check_downtrend(s: "CoinSnapshot") -> tuple[bool, str]:
        """1h 캔들 + 24h 변동률을 복합적으로 보는 하락 추세 판정.

        캔들 데이터가 없으면 24h 단일 기준으로 폴백합니다.

        Args:
            s: CoinSnapshot (candlestick_1h 포함)

        Returns:
            (하락추세 여부, 제외 이유)
        """
        # ── 1h 캔들에서 종가 추출 ──
        closes: list[float] = []
        for c in (s.candlestick_1h or []):
            try:
                v = float(c[2]) if len(c) > 2 else 0.0
                if v > 0:
                    closes.append(v)
            except (ValueError, TypeError):
                pass

        has_candles = len(closes) >= 4

        # ── 기준 A: 24h 강한 하락 (캔들 무관하게 즉시 제외) ──
        if s.change_pct_24h < _DOWN_24H_STRONG:
            return True, f"24h 강한 하락 {s.change_pct_24h:+.1f}%"

        # ── 기준 B: 단기(3h) 급락 ──
        if has_candles and len(closes) >= 4:
            ch3h = (closes[-1] - closes[-4]) / closes[-4] * 100
            if ch3h < _DOWN_3H_THRESHOLD:
                return True, f"단기(3h) {ch3h:+.1f}% 급락"

        # ── 기준 C: 연속 하락 캔들 구조 (24h 강한 상승 조정은 제외) ──
        if len(closes) >= 5 and s.change_pct_24h < 1.0:
            tail = closes[-5:]
            down_count = sum(
                1 for i in range(1, len(tail)) if tail[i] < tail[i - 1]
            )
            if down_count >= _DOWN_CONSEC_RATIO:
                return (
                    True,
                    f"연속 하락 캔들 {down_count}/4 (24h {s.change_pct_24h:+.1f}%)",
                )

        # ── 기준 D: 24h 완만 하락 + 단기도 하락 중 (동반 확인) ──
        if s.change_pct_24h < _DOWN_24H_MILD:
            if has_candles and len(closes) >= 3:
                ch2h = (closes[-1] - closes[-3]) / closes[-3] * 100
                if ch2h <= _DOWN_2H_ANY:
                    return (
                        True,
                        f"24h {s.change_pct_24h:+.1f}% + 단기(2h) {ch2h:+.1f}% 동반 하락",
                    )
            else:
                # 캔들 없음 → 24h 단일 기준으로 폴백
                return True, f"24h 하락 {s.change_pct_24h:+.1f}% (캔들 없음)"

        # ── 기준 E: 저가 근처 + 단기 하락 중 ──
        price_range = s.high_price - s.low_price
        if price_range > 0:
            price_pos = (s.current_price - s.low_price) / price_range * 100
            if price_pos < _LOW_POSITION_PCT:
                if has_candles and len(closes) >= 3:
                    ch2h = (closes[-1] - closes[-3]) / closes[-3] * 100
                    if ch2h < -0.3:
                        return (
                            True,
                            f"저가 근처({price_pos:.0f}%) + 단기(2h) {ch2h:+.1f}% 하락",
                        )
                elif s.change_pct_24h < 0:
                    return True, f"저가 근처({price_pos:.0f}%) + 24h 하락"

        return False, ""

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
