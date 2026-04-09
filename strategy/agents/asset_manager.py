"""자산 운용 전문가

시장 상태, 최근 매매 성과, 잔고 상황을 종합해 투자 비율을 결정합니다.
연속 손절 시 투자 비율을 줄여 리스크를 관리하고,
시장이 좋을 때는 과감하게, 불확실할 때는 보수적으로 접근합니다.
"""
import logging
from dataclasses import dataclass

from .base_agent import BaseSpecialistAgent
from .market_analyst import MarketCondition

logger = logging.getLogger(__name__)


@dataclass
class AllocationDecision:
    """투자 배분 결정 결과"""
    should_invest: bool       # 지금 투자할지
    invest_ratio: float       # 가용 KRW 중 투자 비율 (0.3~0.95)
    reason: str


class AssetManager(BaseSpecialistAgent):
    """시장 상태와 계좌 상황을 보고 투자 비율을 결정하는 전문가 Agent"""

    ROLE_NAME = "asset_manager"
    DISPLAY_NAME = "자산 운용가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 가상화폐와 현금 자산을 운용하는 자산 전문가입니다.\n"
            "시장 상태, 최근 매매 성과, 잔고 상황을 종합해 투자 비율을 결정합니다.\n"
            "연속 손절이 발생했다면 투자 비율을 줄여 리스크를 관리합니다.\n"
            "시장이 좋을 때는 과감하게, 불확실할 때는 보수적으로 접근합니다."
        )

    def execute(self, context: dict) -> dict:
        """투자 비율을 결정하여 AllocationDecision을 반환

        Args:
            context: {
                "market_condition": MarketCondition,
                "eval_stats": dict,     # 과거 매매 통계
                "krw_balance": float,   # 현재 가용 KRW
            }

        Returns:
            {"allocation": AllocationDecision}
        """
        market_condition: MarketCondition = context.get("market_condition")
        eval_stats: dict = context.get("eval_stats", {})
        krw_balance: float = context.get("krw_balance", 0)

        if not market_condition:
            logger.warning("[AssetManager] 시장 상태 정보 없음 — 기본값 반환")
            return {"allocation": self._default_allocation()}

        try:
            # 시장 상태 텍스트
            market_text = (
                f"시장 심리: {market_condition.sentiment}\n"
                f"리스크 수준: {market_condition.risk_level}\n"
                f"시장 강도: {market_condition.strength}\n"
                f"권장 투자 비율: {market_condition.recommended_exposure}\n"
                f"시장 요약: {market_condition.summary}"
            )

            # 매매 성과 텍스트
            stats_text = "매매 이력 없음"
            if eval_stats and eval_stats.get("count", 0) > 0:
                stats_text = (
                    f"최근 {eval_stats['count']}건 매매:\n"
                    f"- 승률: {eval_stats['win_rate']:.0%}\n"
                    f"- 평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%\n"
                    f"- 승리: {eval_stats['win_count']}건, 패배: {eval_stats['loss_count']}건"
                )

            task_prompt = (
                f"현재 자산 상태:\n"
                f"- 가용 KRW: {krw_balance:,.0f}원\n\n"
                f"시장 분석 결과:\n{market_text}\n\n"
                f"최근 매매 성과:\n{stats_text}\n\n"
                f"위 정보를 종합하여 지금 투자할지, 투자한다면 가용 자금의 몇 %를 "
                f"사용할지 결정하세요.\n"
                f"invest_ratio는 0.3~0.95 범위에서 설정하세요.\n\n"
                f"JSON으로만 응답:\n"
                f'{{"should_invest": true, "invest_ratio": 0.8, "reason": "..."}}'
            )

            raw = self._call_llm(task_prompt, max_tokens=256)
            logger.info(f"[AssetManager] LLM 응답: {raw}")

            data = self._parse_json(raw)

            should_invest = bool(data.get("should_invest", True))
            invest_ratio = float(data.get("invest_ratio", 0.85))
            reason = data.get("reason", "")

            # 안전장치: invest_ratio 0.3~0.95 clamp
            invest_ratio = max(0.3, min(0.95, invest_ratio))

            allocation = AllocationDecision(
                should_invest=should_invest,
                invest_ratio=round(invest_ratio, 2),
                reason=reason,
            )

            logger.info(
                f"[AssetManager] 결정: 투자={'예' if allocation.should_invest else '아니오'} / "
                f"비율={allocation.invest_ratio:.0%} / {allocation.reason}"
            )
            return {"allocation": allocation}

        except Exception as e:
            logger.error(f"[AssetManager] 분석 실패: {e}")
            return {"allocation": self._default_allocation()}

    @staticmethod
    def _default_allocation() -> AllocationDecision:
        """에러 시 기본 AllocationDecision"""
        return AllocationDecision(
            should_invest=True,
            invest_ratio=0.85,
            reason="분석 실패 — 기본 비율",
        )
