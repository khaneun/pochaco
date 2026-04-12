"""시장 흐름 분석 전문가

기술적 분석 지표(RSI, MACD, MA, 볼린저밴드, OBV)를 기반으로
시장 전반의 상태를 진단합니다. 과매수 종목 진입을 사전에 차단하고
가격-거래량 다이버전스(가짜 상승)를 경고합니다.
"""
import logging
from dataclasses import dataclass

from .base_agent import BaseSpecialistAgent

logger = logging.getLogger(__name__)


@dataclass
class MarketCondition:
    """시장 상태 분석 결과"""
    sentiment: str        # bullish | bearish | neutral | volatile
    risk_level: str       # low | medium | high
    strength: float       # 0.0~1.0 시장 강도
    recommended_exposure: float  # 0.0~1.0 권장 투자 비율
    summary: str          # 시장 요약 텍스트 (한국어)


class MarketAnalyst(BaseSpecialistAgent):
    """시장 전반의 흐름을 분석하는 전문가 Agent"""

    ROLE_NAME = "market_analyst"
    DISPLAY_NAME = "시장 분석가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 빗썸 거래소 전문 시장 분석가입니다.\n\n"
            "【핵심 임무】\n"
            "8개 코인 분산 포트폴리오의 매수 타이밍을 판단하기 위해 시장 전반의 상태를 진단합니다.\n"
            "당신의 판단이 부정확하면 포트폴리오 전체가 손실을 보고, 당신의 점수가 하락합니다.\n\n"
            "【★ 최우선 경고: 급등 종목 함정 ★】\n"
            "과거 반복적으로 '이미 오른 코인'에 진입하여 조정 구간에서 손절당했습니다.\n"
            "다음 신호가 있는 코인은 반드시 위험으로 표시하세요:\n"
            "- RSI > 65: 과매수 접근 중, 진입 위험\n"
            "- RSI > 70: 과매수 확정, 조정 임박\n"
            "- 가격 상승 + 거래량 감소 (다이버전스): 가짜 상승, 하락 전환 가능성\n"
            "- MACD 데드크로스 또는 히스토그램 감소: 상승 모멘텀 소진\n"
            "- 볼린저밴드 상단(>80%) 위치: 밴드 회귀 가능성 높음\n"
            "- 이동평균 역배열: 하락 추세 진행 중\n\n"
            "【★ 파생시장 경고 신호 ★】\n"
            "선물 시장 펀딩비·미결제약정은 군중 심리와 포지션 과열을 선행 감지합니다.\n"
            "- 펀딩비 > +0.05%/8h: 롱 과열 → 현물 조정 임박 가능성. 신규 진입 자제\n"
            "- 펀딩비 > +0.10%/8h: 극단 롱 과열 → risk_level=high 강제\n"
            "- 펀딩비 < -0.03%/8h: 숏 과열 → 숏 스퀴즈 가능. 현물 반등 신호 가능\n"
            "- OI 급감(> -3%) + 가격 하락: 강제청산 진행 중, 추가 하락 위험\n"
            "- OI 급증(> +3%) + 가격 상승: 신규 자금 유입, 추세 지속 가능성 높음\n"
            "- OI 급증 + 가격 변동 없음: 포지션 축적 중, 곧 큰 방향 결정 예상\n\n"
            "【✅ 익절 성공 패턴 — 이 패턴들이 실제로 상승장을 잡았습니다】\n"
            "아래 조합이 겹칠수록 상승 확률이 높습니다. 프롬프트에 반드시 반영하세요:\n"
            "① RSI 35~55 구간 + 상향 반전 중: 과매도에서 회복 초기, 조정 없이 상승하는 구간\n"
            "② MACD 골든크로스 직후: 히스토그램이 음→양 전환 직후 진입 시 성공률 최고\n"
            "③ OBV 상승 + 가격 횡보·소폭 하락: 거래량이 실질 매수세를 확인 → 가격 폭발 전 징후\n"
            "④ BB 중간선 하단에서 반등 + 볼린저 밴드 수축(폭 좁음): 에너지 응축 후 방향 돌파 패턴\n"
            "⑤ MA 정배열 진행 중(5>10>20): 단기MA가 장기MA 위로 정렬 완성 직후\n"
            "⑥ 거래량 급증 + 가격 저항선 돌파: 거래량 동반 돌파는 가짜 상승이 아님\n"
            "⑦ 펀딩비 중립(-0.03%~+0.05%) + OI 증가: 과열 없이 신규 자금 유입 → 건강한 상승\n"
            "⑧ 24h 변동률 +1~+4% + 고점 근처가 아닌 경우: 과매수 전 상승 모멘텀 포착 구간\n"
            "▶ 위 패턴 중 3가지 이상 충족 시 → bullish 판단, 진입 긍정 고려\n\n"
            "【분석 프레임워크 — 반드시 이 순서로 판단】\n"
            "1. BTC/ETH 기술 지표 확인 → RSI·MACD·MA 상태가 시장 방향 결정\n"
            "2. BTC 펀딩비 → 선물 시장 과열 여부 선제 확인\n"
            "3. 상위 코인의 기술 지표 분포 → RSI 과매수 비율, MACD 상승/하락 비율\n"
            "4. OBV(On-Balance Volume) 추세 → 실질 매수세/매도세 확인\n"
            "   - 가격 상승 + OBV 상승 = 강한 상승 (진짜)\n"
            "   - 가격 상승 + OBV 하락 = 가짜 상승 (주의!)\n"
            "5. 거래량 추세 → 급증/감소 패턴이 추세 전환 선행 지표\n"
            "6. 볼린저밴드 폭 → 밴드 수축은 큰 변동 예고, 확장은 추세 진행 중\n\n"
            "【절대 원칙】\n"
            "- BTC RSI > 70이거나 MACD 하락이면 risk_level=high, recommended_exposure≤0.3\n"
            "- BTC 24h 변동률이 -3% 이하이면 risk_level=high, recommended_exposure≤0.3\n"
            "- BTC 펀딩비 > +0.10%이면 risk_level=high, recommended_exposure≤0.3\n"
            "- 상위 코인 중 롱과열(>0.05%) 비율이 50% 이상이면 → 시장 전체 과열, 진입 자제\n"
            "- 상위 10개 코인 중 RSI 과매수(>70)가 5개 이상이면 → 과열 시장, 진입 자제\n"
            "- 상위 10개 코인 중 7개 이상 하락이면 sentiment=bearish\n"
            "- OBV 하락 추세인 코인이 대다수이면 → 실질 매도세 우위, 보수적 판단\n"
            "- 확신이 없으면 반드시 보수적으로 판단. 과도한 낙관은 금물\n"
            "- 시장 요약에 핵심 기술 지표 수치와 펀딩비를 반드시 포함하세요"
        )

    def execute(self, context: dict) -> dict:
        """시장 상태를 분석하여 MarketCondition을 반환

        Args:
            context: {"snapshots": list[CoinSnapshot]}

        Returns:
            {"condition": MarketCondition}
        """
        snapshots = context.get("snapshots", [])
        if not snapshots:
            logger.warning("[MarketAnalyst] 스냅샷이 비어있음 — 기본값 반환")
            return {"condition": self._default_condition()}

        try:
            market_text = self._snapshots_to_market_text(snapshots)

            # 기술 지표 통계 요약
            tech_stats = self._build_tech_stats(snapshots)

            task_prompt = (
                f"아래는 빗썸 상위 코인 실시간 데이터와 기술적·파생 분석 지표입니다.\n\n"
                f"【기술 지표 + 파생 통계】\n{tech_stats}\n\n"
                f"【개별 코인 데이터】\n{market_text}\n\n"
                f"위 지표를 기반으로 시장 전반의 상태를 분석하세요.\n"
                f"★ RSI 과매수 코인 비율, MACD 상승/하락 비율, OBV 추세를 반드시 고려하세요.\n"
                f"★ 가격-거래량 다이버전스가 있는 코인이 많으면 가짜 상승 시장입니다.\n"
                f"★ BTC 펀딩비가 +0.10% 초과이면 극단 롱과열 — 즉시 risk_level=high로 판단하세요.\n"
                f"★ 롱과열 코인이 전체의 50% 이상이면 시장 전체가 과열 상태입니다.\n\n"
                f"1. 전반적 심리 (bullish/bearish/neutral/volatile)\n"
                f"2. 리스크 수준 (low/medium/high)\n"
                f"3. 시장 강도 (0.0~1.0)\n"
                f"4. 권장 투자 비율 (0.0~1.0, 1.0=적극 투자, 0.0=관망)\n"
                f"5. 시장 요약 (한국어, 150자 이내, 핵심 기술 지표 + 펀딩비 수치 포함)\n\n"
                f'JSON으로만 응답:\n'
                f'{{"sentiment": "...", "risk_level": "...", "strength": 0.7, '
                f'"recommended_exposure": 0.8, "summary": "..."}}'
            )

            raw = self._call_llm(task_prompt, max_tokens=300)
            logger.info(f"[MarketAnalyst] LLM 응답: {raw}")

            data = self._parse_json(raw)

            # 값 검증 및 보정
            sentiment = data.get("sentiment", "neutral")
            if sentiment not in ("bullish", "bearish", "neutral", "volatile"):
                sentiment = "neutral"

            risk_level = data.get("risk_level", "medium")
            if risk_level not in ("low", "medium", "high"):
                risk_level = "medium"

            strength = max(0.0, min(1.0, float(data.get("strength", 0.5))))
            recommended_exposure = max(0.0, min(1.0, float(data.get("recommended_exposure", 0.7))))

            condition = MarketCondition(
                sentiment=sentiment,
                risk_level=risk_level,
                strength=round(strength, 2),
                recommended_exposure=round(recommended_exposure, 2),
                summary=data.get("summary", "분석 완료")[:150],
            )

            logger.info(
                f"[MarketAnalyst] 분석 결과: {condition.sentiment} / "
                f"리스크={condition.risk_level} / 강도={condition.strength} / "
                f"권장비율={condition.recommended_exposure}"
            )
            return {"condition": condition}

        except Exception as e:
            logger.error(f"[MarketAnalyst] 분석 실패: {e}")
            return {"condition": self._default_condition()}

    @staticmethod
    def _snapshots_to_market_text(snapshots) -> str:
        """스냅샷 목록을 시장 분석용 텍스트로 변환 (기술 지표 + 파생 데이터 포함)"""
        lines = []
        for s in snapshots:
            ti = s.technical
            deriv_part = ""
            if s.derivatives.available:
                deriv_part = f" | 파생: {s.derivatives.summary}"
            lines.append(
                f"- {s.symbol}: {s.current_price:,.0f}원 "
                f"24h={s.change_pct_24h:+.1f}% "
                f"거래대금={s.volume_krw_24h / 1e8:.0f}억 "
                f"| {ti.summary}"
                f"{deriv_part}"
            )
        return "\n".join(lines)

    @staticmethod
    def _build_tech_stats(snapshots) -> str:
        """전체 코인의 기술 지표 + 파생 데이터 통계 요약"""
        if not snapshots:
            return "데이터 없음"

        n = len(snapshots)
        rsi_values = [s.technical.rsi_14 for s in snapshots]
        rsi_overbought = sum(1 for r in rsi_values if r >= 70)
        rsi_oversold = sum(1 for r in rsi_values if r <= 30)
        rsi_avg = sum(rsi_values) / n

        macd_up = sum(1 for s in snapshots if s.technical.macd_trend in ("상승", "골든크로스"))
        macd_dn = sum(1 for s in snapshots if s.technical.macd_trend in ("하락", "데드크로스"))

        obv_up = sum(1 for s in snapshots if s.technical.obv_trend == "상승")
        obv_dn = sum(1 for s in snapshots if s.technical.obv_trend == "하락")

        divergence_count = sum(1 for s in snapshots if s.technical.price_volume_divergence)

        ma_bull = sum(1 for s in snapshots if s.technical.ma_alignment == "정배열")
        ma_bear = sum(1 for s in snapshots if s.technical.ma_alignment == "역배열")

        # BTC 기술 + 파생 지표
        btc = next((s for s in snapshots if s.symbol == "BTC"), None)
        btc_line = ""
        if btc:
            bti = btc.technical
            btc_deriv = ""
            if btc.derivatives.available:
                btc_deriv = (
                    f" | 펀딩비={btc.derivatives.funding_rate:+.3f}%"
                    f"({btc.derivatives.funding_signal})"
                    f" OI={btc.derivatives.oi_trend}"
                )
            btc_line = (
                f"BTC: RSI={bti.rsi_14:.0f} MACD={bti.macd_trend} "
                f"MA={bti.ma_alignment} OBV={bti.obv_trend} "
                f"24h={btc.change_pct_24h:+.1f}%{btc_deriv}\n"
            )

        # 파생 데이터 통계 (이용 가능한 코인 기준)
        deriv_stats = ""
        deriv_snaps = [s for s in snapshots if s.derivatives.available]
        if deriv_snaps:
            nd = len(deriv_snaps)
            high_funding = sum(
                1 for s in deriv_snaps if s.derivatives.funding_rate > 0.05
            )
            extreme_funding = sum(
                1 for s in deriv_snaps if s.derivatives.funding_rate > 0.10
            )
            neg_funding = sum(
                1 for s in deriv_snaps if s.derivatives.funding_rate < -0.03
            )
            avg_funding = sum(
                s.derivatives.funding_rate for s in deriv_snaps
            ) / nd
            oi_surge = sum(
                1 for s in deriv_snaps if s.derivatives.oi_trend in ("급증", "증가")
            )
            deriv_stats = (
                f"\n【파생시장 통계 ({nd}개 심볼)】\n"
                f"펀딩비 평균: {avg_funding:+.3f}% | "
                f"롱과열(>0.05%): {high_funding}/{nd}개 | "
                f"극단롱과열(>0.10%): {extreme_funding}/{nd}개 | "
                f"숏과열(<-0.03%): {neg_funding}/{nd}개\n"
                f"OI 증가(신규자금 유입): {oi_surge}/{nd}개"
            )

        return (
            f"{btc_line}"
            f"RSI 평균: {rsi_avg:.0f} | 과매수(≥70): {rsi_overbought}/{n}개 | "
            f"과매도(≤30): {rsi_oversold}/{n}개\n"
            f"MACD 상승: {macd_up}/{n}개 | 하락: {macd_dn}/{n}개\n"
            f"OBV 상승(실질매수세): {obv_up}/{n}개 | 하락(매도세): {obv_dn}/{n}개\n"
            f"MA 정배열(상승추세): {ma_bull}/{n}개 | 역배열(하락추세): {ma_bear}/{n}개\n"
            f"가격-거래량 다이버전스(가짜상승): {divergence_count}/{n}개"
            f"{deriv_stats}"
        )

    @staticmethod
    def _default_condition() -> MarketCondition:
        """에러 시 기본 MarketCondition"""
        return MarketCondition(
            sentiment="neutral",
            risk_level="medium",
            strength=0.5,
            recommended_exposure=0.7,
            summary="분석 실패",
        )
