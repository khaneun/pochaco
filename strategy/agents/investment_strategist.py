"""투자 전문가 — 수익 극대화를 위한 공격적 투자 판단

자산 운용가의 보수적 관점에 대적하여, 불확실성 속에서도
기회를 포착하고 적극적 투자를 주장하는 역할을 합니다.
"""
import logging
from dataclasses import dataclass

from .base_agent import BaseSpecialistAgent
from .market_analyst import MarketCondition

logger = logging.getLogger(__name__)


@dataclass
class InvestmentOpinion:
    """투자 전문가의 투자 의견"""
    should_invest: bool       # 투자 실행 여부
    invest_ratio: float       # 투자 비율 (0.3~0.95)
    aggression: float         # 공격성 (0.0~1.0, 높을수록 적극적)
    opportunity_score: float  # 현재 기회 수준 (0.0~1.0)
    reason: str


class InvestmentStrategist(BaseSpecialistAgent):
    """수익 극대화를 위한 공격적 투자 판단 전문가 Agent"""

    ROLE_NAME = "investment_strategist"
    DISPLAY_NAME = "투자 전문가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 암호화폐 시장에서 수익 극대화를 추구하는 공격적 투자 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "시장 분석 결과와 매매 성과를 기반으로, 지금이 투자 기회인지 판단합니다.\n"
            "자산 운용가가 리스크 관리와 자본 보전을 중시한다면,\n"
            "당신은 불확실성 속 기회 포착과 수익 극대화를 담당합니다.\n"
            "두 전문가의 의견이 종합되어 최종 투자 결정이 내려집니다.\n\n"
            "【투자 철학 — 핵심 원칙】\n"
            "1. ★변동성은 리스크가 아니라 기회★: 하락장에서도 반등 종목이 있고,\n"
            "   공포 속에 매수한 포지션이 가장 큰 수익을 가져옴\n"
            "2. ★시장 타이밍보다 포트폴리오 분산★: 8개 코인 분산이므로\n"
            "   개별 종목 리스크는 이미 분산됨 → 시장 전체 방향만 중요\n"
            "3. ★기회비용 인식★: 투자를 보류하는 것도 비용.\n"
            "   대기 중인 자금은 수익을 만들지 못함\n"
            "4. ★단기 손실 감내★: 분할 매도(-1%/-1.5%)와 손절(-2%)이\n"
            "   시스템에 내장되어 있으므로, 최대 손실은 제한됨\n\n"
            "【의사결정 기준】\n"
            "- 시장이 bearish여도 변동성이 높으면 → 투자 기회 (invest=true)\n"
            "- 연속 손절 중이라도 → 전략이 잘못된 것이지 시장이 나쁜 것 아닐 수 있음\n"
            "- 승률이 낮더라도 평균 수익률이 +면 → 계속 투자 (기대값 양수)\n"
            "- 거래량이 급증하면 → 방향 무관, 기회 (공격적 투자 권장)\n\n"
            "【자산 운용가와의 균형】\n"
            "- 당신이 invest=true, 운용가가 invest=false → 시스템이 종합 판단\n"
            "- 둘 다 invest=false → 보류 확정\n"
            "- 둘 다 invest=true → 두 비율의 가중 평균으로 결정\n"
            "- invest_ratio: 당신은 0.5~0.95 범위에서 적극적으로 제안하되\n"
            "  기회 수준(opportunity_score)으로 확신도를 표현하세요"
        )

    def execute(self, context: dict) -> dict:
        """투자 기회 판단 및 공격적 투자 비율 제안

        Args:
            context: {
                "market_condition": MarketCondition,
                "eval_stats": dict,
                "krw_balance": float,
                "coin_scores_summary": str,  # 상위 후보 코인 요약
            }

        Returns:
            {"opinion": InvestmentOpinion}
        """
        market_condition: MarketCondition = context.get("market_condition")
        eval_stats: dict = context.get("eval_stats", {})
        krw_balance: float = context.get("krw_balance", 0)
        coin_scores_summary: str = context.get("coin_scores_summary", "")

        if not market_condition:
            logger.warning("[InvestmentStrategist] 시장 상태 없음 — 기본 의견")
            return {"opinion": self._default_opinion()}

        try:
            market_text = (
                f"시장 심리: {market_condition.sentiment}\n"
                f"리스크 수준: {market_condition.risk_level}\n"
                f"시장 강도: {market_condition.strength}\n"
                f"시장 요약: {market_condition.summary}"
            )

            stats_text = "매매 이력 없음 (첫 투자 — 적극적 시작 권장)"
            if eval_stats and eval_stats.get("count", 0) > 0:
                # 기대값 계산
                avg_pnl = eval_stats.get("avg_pnl_pct", 0)
                win_rate = eval_stats.get("win_rate", 0)
                expected_value = "양수 (계속 투자 유리)" if avg_pnl > 0 else "음수 (전략 점검 필요)"
                stats_text = (
                    f"최근 {eval_stats['count']}건 매매:\n"
                    f"- 승률: {win_rate:.0%}\n"
                    f"- 평균 수익률: {avg_pnl:+.2f}%\n"
                    f"- 기대값: {expected_value}\n"
                    f"- 승리: {eval_stats['win_count']}건, 패배: {eval_stats['loss_count']}건"
                )

            coin_section = ""
            if coin_scores_summary:
                coin_section = f"\n상위 후보 코인 현황:\n{coin_scores_summary}\n"

            task_prompt = (
                f"현재 자산 상태:\n"
                f"- 가용 KRW: {krw_balance:,.0f}원\n\n"
                f"시장 분석 결과:\n{market_text}\n\n"
                f"최근 매매 성과:\n{stats_text}\n"
                f"{coin_section}\n"
                f"위 정보를 기반으로 지금이 투자 기회인지 판단하세요.\n"
                f"자산 운용가는 리스크를 중시하지만, 당신은 기회를 중시합니다.\n"
                f"기회 수준(opportunity_score)으로 확신도를 0.0~1.0으로 표현하세요.\n"
                f"aggression은 얼마나 공격적으로 투자할지 0.0~1.0으로 표현합니다.\n\n"
                f"JSON으로만 응답:\n"
                f'{{"should_invest": true, "invest_ratio": 0.85, '
                f'"aggression": 0.7, "opportunity_score": 0.8, '
                f'"reason": "..."}}'
            )

            raw = self._call_llm(task_prompt, max_tokens=300)
            logger.info(f"[InvestmentStrategist] LLM 응답: {raw}")

            data = self._parse_json(raw)

            should_invest = bool(data.get("should_invest", True))
            invest_ratio = float(data.get("invest_ratio", 0.85))
            aggression = float(data.get("aggression", 0.5))
            opportunity_score = float(data.get("opportunity_score", 0.5))
            reason = data.get("reason", "")

            # 안전장치
            invest_ratio = max(0.3, min(0.95, invest_ratio))
            aggression = max(0.0, min(1.0, aggression))
            opportunity_score = max(0.0, min(1.0, opportunity_score))

            opinion = InvestmentOpinion(
                should_invest=should_invest,
                invest_ratio=round(invest_ratio, 2),
                aggression=round(aggression, 2),
                opportunity_score=round(opportunity_score, 2),
                reason=reason,
            )

            logger.info(
                f"[InvestmentStrategist] 의견: "
                f"투자={'예' if opinion.should_invest else '아니오'} / "
                f"비율={opinion.invest_ratio:.0%} / "
                f"공격성={opinion.aggression:.1f} / "
                f"기회={opinion.opportunity_score:.1f} / "
                f"{opinion.reason}"
            )
            return {"opinion": opinion}

        except Exception as e:
            logger.error(f"[InvestmentStrategist] 분석 실패: {e}")
            return {"opinion": self._default_opinion()}

    @staticmethod
    def _default_opinion() -> InvestmentOpinion:
        return InvestmentOpinion(
            should_invest=True,
            invest_ratio=0.80,
            aggression=0.6,
            opportunity_score=0.5,
            reason="분석 실패 — 기본 적극 투자",
        )
