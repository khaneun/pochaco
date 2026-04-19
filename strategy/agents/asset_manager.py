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


@dataclass
class PyramidDecision:
    """피라미딩 추가 매수 결정"""
    should_pyramid: bool      # 추가 매수 여부
    threshold_pct: float      # 실행 최소 수익률 (0.5~3.0%)
    add_ratio: float          # 원래 투입금 대비 추가 비율 (0.10~0.50)
    reason: str


class AssetManager(BaseSpecialistAgent):
    """시장 상태와 계좌 상황을 보고 투자 비율을 결정하는 전문가 Agent"""

    ROLE_NAME = "asset_manager"
    DISPLAY_NAME = "자산 운용가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 8개 코인 분산 포트폴리오의 자금 배분 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "시장 분석가의 진단 결과와 최근 매매 성과를 종합하여,\n"
            "지금 투자할지 여부와 가용 자금 중 얼마를 투입할지 결정합니다.\n"
            "투자 비율이 곧 8개 코인에 분배될 총 자금을 결정하므로 신중해야 합니다.\n\n"
            "【의사결정 기준 — 반드시 준수】\n"
            "1. 시장 분석가의 risk_level이 high이면 → invest_ratio 0.5 이하 또는 투자 보류\n"
            "2. 연속 2건 이상 손절 → invest_ratio 최소 0.1 감소 (자본 보전 우선)\n"
            "3. 연속 3건 이상 손절 → should_invest=false 강력 권고\n"
            "4. 승률 60% 이상이고 시장 bullish → invest_ratio 0.85~0.95 (적극)\n"
            "5. 최근 성과가 좋아도 시장이 bearish이면 → 시장 우선 (0.5~0.7)\n\n"
            "【절대 원칙】\n"
            "- invest_ratio 범위: 0.30 ~ 0.95 (30%~95%)\n"
            "- 시장 분석가가 high risk를 경고했는데 0.8 이상 배분하면 실패\n"
            "- 투자 보류 판단을 두려워하지 마세요. 기회는 또 옵니다.\n"
            "- reason에는 핵심 수치(승률, 시장심리 등)를 반드시 포함하세요."
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

    def decide_pyramid(self, context: dict) -> PyramidDecision:
        """피라미딩 추가 매수 여부·조건 결정

        Args:
            context: {
                "current_pnl_pct": float,   # 현재 포트폴리오 수익률
                "peak_pnl_pct": float,      # 보유 기간 최고 수익률
                "total_buy_krw": float,     # 현재 포트폴리오 총 투입금
                "take_profit_pct": float,   # 익절 목표%
                "krw_available": float,     # 가용 KRW
                "coin_count": int,          # 보유 코인 수
                "held_minutes": float,      # 보유 시간(분)
            }

        Returns:
            PyramidDecision
        """
        current_pnl = context.get("current_pnl_pct", 0.0)
        peak_pnl = context.get("peak_pnl_pct", 0.0)
        total_buy = context.get("total_buy_krw", 0.0)
        tp = context.get("take_profit_pct", 5.0)
        krw_avail = context.get("krw_available", 0.0)
        coin_count = context.get("coin_count", 1)
        held_min = context.get("held_minutes", 0.0)

        # 가용 KRW가 원래 투입금의 10% 미만이면 무조건 보류
        if krw_avail < total_buy * 0.10:
            return PyramidDecision(
                should_pyramid=False,
                threshold_pct=999.0,
                add_ratio=0.0,
                reason=f"가용 KRW 부족 ({krw_avail:,.0f}원)",
            )

        try:
            task_prompt = (
                f"현재 포트폴리오 상태:\n"
                f"- 현재 수익률: {current_pnl:+.2f}%\n"
                f"- 보유 기간 최고 수익률: {peak_pnl:+.2f}%\n"
                f"- 익절 목표: +{tp}%\n"
                f"- 총 투입금: {total_buy:,.0f}원\n"
                f"- 가용 KRW: {krw_avail:,.0f}원 (원래 투입금의 {krw_avail/total_buy*100:.0f}%)\n"
                f"- 보유 코인 수: {coin_count}개\n"
                f"- 보유 시간: {held_min:.0f}분\n\n"
                f"【피라미딩 의사결정】\n"
                f"포트폴리오가 플러스 국면일 때 상승 모멘텀에 편승해 추가 매수할지 결정합니다.\n"
                f"이 결정은 포트폴리오 생애 동안 단 1회만 실행됩니다.\n\n"
                f"【판단 기준】\n"
                f"- 수익이 안정적으로 유지되고 익절 목표의 30~60% 달성 시 적극 고려\n"
                f"- 수익이 고점 대비 크게 빠졌다면 모멘텀이 약하므로 보류\n"
                f"- threshold_pct: 추가 매수 실행에 필요한 최소 수익률 (0.5~3.0%)\n"
                f"- add_ratio: 원래 투입금 대비 추가 투입 비율 (0.10~0.50)\n"
                f"  · 상승 확신 강하면 0.3~0.5, 보통이면 0.1~0.2\n"
                f"  · 가용 KRW 제약을 반드시 고려\n\n"
                f"JSON으로만 응답:\n"
                f'{{"should_pyramid": true, "threshold_pct": 2.0, "add_ratio": 0.25, "reason": "..."}}'
            )

            raw = self._call_llm(task_prompt, max_tokens=256)
            logger.info(f"[AssetManager] 피라미딩 응답: {raw}")
            data = self._parse_json(raw)

            should = bool(data.get("should_pyramid", False))
            threshold = float(data.get("threshold_pct", 2.0))
            add_ratio = float(data.get("add_ratio", 0.25))
            reason = data.get("reason", "")

            threshold = max(0.5, min(3.0, threshold))
            add_ratio = max(0.10, min(0.50, add_ratio))

            decision = PyramidDecision(
                should_pyramid=should,
                threshold_pct=round(threshold, 2),
                add_ratio=round(add_ratio, 2),
                reason=reason,
            )
            logger.info(
                f"[AssetManager] 피라미딩: {'예' if should else '아니오'} / "
                f"최소수익={threshold:.1f}% / 추가비율={add_ratio:.0%} / {reason}"
            )
            return decision

        except Exception as e:
            logger.error(f"[AssetManager] 피라미딩 판단 실패: {e}")
            return PyramidDecision(
                should_pyramid=False,
                threshold_pct=999.0,
                add_ratio=0.0,
                reason=f"판단 실패: {e}",
            )

    @staticmethod
    def _default_allocation() -> AllocationDecision:
        """에러 시 기본 AllocationDecision"""
        return AllocationDecision(
            should_invest=True,
            invest_ratio=0.85,
            reason="분석 실패 — 기본 비율",
        )
