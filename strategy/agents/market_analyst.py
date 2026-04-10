"""시장 흐름 분석 전문가

CoinSnapshot 목록을 받아 시장 전반의 분위기, 리스크 수준, 투자 적합성을 판단합니다.
BTC/ETH 주도 코인의 흐름, 알트코인 동조 여부, 전체 거래대금 추세, 변동성 수준을 종합 분석합니다.
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
            "당신의 판단이 부정확하면 포트폴리오 전체가 손실을 봅니다.\n\n"
            "【분석 프레임워크 — 반드시 이 순서로 판단】\n"
            "1. BTC/ETH 흐름 확인 → 대장주가 하락 중이면 알트코인도 위험\n"
            "2. 상위 10개 코인의 24h 변동률 분포 → 절반 이상 음수면 bearish\n"
            "3. 전체 거래대금 수준 → 거래대금 급감은 방향성 상실 신호\n"
            "4. 변동폭(고저차) 분포 → 변동폭이 전반적으로 작으면 관망 권고\n"
            "5. 현재가 위치 분포 → 대부분 저가 근처(20% 이하)면 약세장\n\n"
            "【절대 원칙】\n"
            "- BTC 24h 변동률이 -3% 이하이면 risk_level=high, recommended_exposure=0.3 이하 필수\n"
            "- 상위 10개 코인 중 7개 이상 하락이면 sentiment=bearish 필수\n"
            "- 확신이 없으면 반드시 보수적으로 판단하세요. 과도한 낙관은 금물입니다.\n"
            "- 시장 요약은 매수 전문가가 참고하므로, 핵심 리스크 요인을 반드시 언급하세요."
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

            task_prompt = (
                f"아래는 빗썸 상위 코인 실시간 데이터입니다.\n"
                f"{market_text}\n\n"
                f"시장 전반의 상태를 분석하세요:\n"
                f"1. 전반적 심리 (bullish/bearish/neutral/volatile)\n"
                f"2. 리스크 수준 (low/medium/high)\n"
                f"3. 시장 강도 (0.0~1.0)\n"
                f"4. 권장 투자 비율 (0.0~1.0, 1.0=적극 투자, 0.0=관망)\n"
                f"5. 시장 요약 (한국어, 100자 이내)\n\n"
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
                summary=data.get("summary", "분석 완료")[:100],
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
        """스냅샷 목록을 시장 분석용 텍스트로 변환"""
        lines = []
        for s in snapshots:
            vol_pct = (
                (s.high_price - s.low_price) / s.low_price * 100
                if s.low_price > 0 else 0
            )
            price_range = s.high_price - s.low_price
            pos_pct = (
                (s.current_price - s.low_price) / price_range * 100
                if price_range > 0 else 50
            )
            lines.append(
                f"- {s.symbol}: 현재가={s.current_price:,.0f}원, "
                f"24h변동={s.change_pct_24h:+.2f}%, "
                f"거래대금={s.volume_krw_24h / 1e8:.1f}억원, "
                f"변동폭={vol_pct:.1f}%, 현재가위치={pos_pct:.0f}%"
            )
        return "\n".join(lines)

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
