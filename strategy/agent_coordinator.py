"""Agent Coordinator — 6개 전문가 Agent 오케스트레이션

TradingEngine은 이 클래스만 의존하며, 기존 TradingAgent와
동일한 메서드 시그니처(select_coin, should_adjust_strategy, evaluate_trade)를
제공해 최소 변경으로 전환 가능.
"""
import logging
from datetime import datetime

from database import TradeRepository
from .ai_agent import AgentDecision, TradeEvaluation
from .market_analyzer import CoinSnapshot
from .coin_selector import CoinScore
from .agents import (
    MarketAnalyst, MarketCondition,
    AssetManager, AllocationDecision,
    BuyStrategist,
    SellStrategist,
    PortfolioEvaluator,
    MetaEvaluator, AgentFeedback,
)
from .agents.meta_evaluator import AGENT_ROLES

logger = logging.getLogger(__name__)


class AgentCoordinator:
    """6개 전문가 Agent를 오케스트레이션하는 코디네이터"""

    def __init__(
        self,
        market_analyst: MarketAnalyst,
        asset_manager: AssetManager,
        buy_strategist: BuyStrategist,
        sell_strategist: SellStrategist,
        portfolio_evaluator: PortfolioEvaluator,
        meta_evaluator: MetaEvaluator,
        repo: TradeRepository,
    ):
        self._agents: dict = {
            "market_analyst": market_analyst,
            "asset_manager": asset_manager,
            "buy_strategist": buy_strategist,
            "sell_strategist": sell_strategist,
            "portfolio_evaluator": portfolio_evaluator,
        }
        self._meta = meta_evaluator
        self._repo = repo

        # 마지막 투자 비율 (TradingEngine에서 참조)
        self.last_invest_ratio: float = 0.95
        # 마지막 시장 분석 결과
        self.last_market_condition: MarketCondition | None = None

    # ------------------------------------------------------------------ #
    #  기본 속성                                                            #
    # ------------------------------------------------------------------ #
    @property
    def provider_name(self) -> str:
        return self._agents["buy_strategist"]._llm.provider_name

    def get_agent_scores(self) -> dict[str, float]:
        """대시보드용 — 각 Agent 현재 점수 반환"""
        return {role: agent.score for role, agent in self._agents.items()}

    def get_all_agents(self) -> dict:
        """전체 Agent dict 반환"""
        return self._agents

    # ------------------------------------------------------------------ #
    #  DB에서 피드백 복원 (재시작 시)                                         #
    # ------------------------------------------------------------------ #
    def restore_feedbacks_from_db(self) -> None:
        """DB에 저장된 최신 피드백을 각 Agent에 로드"""
        try:
            latest = self._repo.get_latest_agent_scores()
            for score_record in latest:
                agent = self._agents.get(score_record.agent_role)
                if agent:
                    feedback_text = (
                        f"점수: {score_record.score}/100\n"
                        f"강점: {score_record.strengths}\n"
                        f"약점: {score_record.weaknesses}\n"
                        f"지시: {score_record.directive}"
                    )
                    agent.update_feedback(feedback_text, score_record.score)
                    logger.info(
                        f"[피드백 복원] {score_record.agent_role}: "
                        f"{score_record.score:.0f}점"
                    )
        except Exception as e:
            logger.warning(f"[피드백 복원 실패] {e}")

    # ------------------------------------------------------------------ #
    #  의사결정 로그 저장 헬퍼                                                #
    # ------------------------------------------------------------------ #
    def _log_decision(
        self, agent_role: str, decision_type: str,
        input_summary: str, output_summary: str,
        position_id: int | None = None,
    ) -> None:
        try:
            self._repo.save_decision_log(
                agent_role=agent_role,
                decision_type=decision_type,
                input_summary=input_summary[:500],
                output_summary=output_summary[:500],
                position_id=position_id,
            )
        except Exception as e:
            logger.warning(f"[의사결정 로그 저장 실패] {e}")

    # ------------------------------------------------------------------ #
    #  코인 선정 (기존 TradingAgent.select_coin 대체)                       #
    # ------------------------------------------------------------------ #
    def select_coin(
        self,
        snapshots: list[CoinSnapshot],
        eval_stats: dict | None = None,
        coin_scores: list[CoinScore] | None = None,
    ) -> AgentDecision:
        """시장 분석 → 자산 배분 → 코인 선정 파이프라인"""

        # 1) 시장 분석가
        logger.info("[Coordinator] 시장 분석가 분석 중...")
        market_result = self._agents["market_analyst"].execute({
            "snapshots": snapshots,
        })
        condition = market_result.get("condition", MarketCondition(
            sentiment="neutral", risk_level="medium",
            strength=0.5, recommended_exposure=0.7, summary="분석 실패",
        ))
        self.last_market_condition = condition
        self._log_decision(
            "market_analyst", "market_analysis",
            f"코인 {len(snapshots)}개 분석",
            f"{condition.sentiment} / 리스크={condition.risk_level} / 강도={condition.strength:.1f}",
        )

        # 2) 자산 운용가
        logger.info("[Coordinator] 자산 운용가 배분 결정 중...")
        alloc_result = self._agents["asset_manager"].execute({
            "market_condition": condition,
            "eval_stats": eval_stats,
        })
        allocation = alloc_result.get("allocation", AllocationDecision(
            should_invest=True, invest_ratio=0.85, reason="기본 배분",
        ))
        self.last_invest_ratio = allocation.invest_ratio
        self._log_decision(
            "asset_manager", "allocation",
            f"시장={condition.sentiment} 리스크={condition.risk_level}",
            f"투자={'Y' if allocation.should_invest else 'N'} 비율={allocation.invest_ratio:.0%} | {allocation.reason}",
        )

        if not allocation.should_invest:
            raise RuntimeError(
                f"[자산 운용가 판단] 투자 보류: {allocation.reason}"
            )

        # 3) 매수 전문가
        logger.info("[Coordinator] 매수 전문가 코인 선정 중...")
        buy_result = self._agents["buy_strategist"].execute({
            "snapshots": snapshots,
            "market_condition": condition,
            "allocation": allocation,
            "eval_stats": eval_stats,
            "coin_scores": coin_scores,
        })
        decision = buy_result.get("decision")
        if decision is None:
            raise RuntimeError("[매수 전문가] 코인 선정 실패")

        self._log_decision(
            "buy_strategist", "coin_select",
            f"후보 {len(snapshots)}개 / 시장={condition.sentiment}",
            f"{decision.symbol} TP=+{decision.take_profit_pct}% "
            f"SL1={decision.stop_loss_1st_pct}% SL2={decision.stop_loss_pct}%",
        )

        return decision

    # ------------------------------------------------------------------ #
    #  전략 동적 조정 (기존 TradingAgent.should_adjust_strategy 대체)       #
    # ------------------------------------------------------------------ #
    def should_adjust_strategy(
        self,
        symbol: str,
        buy_price: float,
        current_price: float,
        current_pnl_pct: float,
        holding_minutes: int,
        original_tp: float,
        original_sl: float,
        original_sl_1st: float | None = None,
        sl1_executed: bool = False,
    ) -> dict:
        """매도 전문가에게 TP/SL 조정 질의"""
        result = self._agents["sell_strategist"].execute({
            "symbol": symbol,
            "buy_price": buy_price,
            "current_price": current_price,
            "current_pnl_pct": current_pnl_pct,
            "holding_minutes": holding_minutes,
            "original_tp": original_tp,
            "original_sl": original_sl,
            "original_sl_1st": original_sl_1st,
            "sl1_executed": sl1_executed,
        })
        adjust_result = result.get("adjust_result", {
            "adjust": False,
            "new_take_profit_pct": original_tp,
            "new_stop_loss_1st_pct": original_sl_1st,
            "new_stop_loss_pct": original_sl,
            "reason": "폴백",
        })

        if adjust_result.get("adjust"):
            self._log_decision(
                "sell_strategist", "exit_adjust",
                f"{symbol} PnL={current_pnl_pct:+.2f}% {holding_minutes}분",
                f"TP={adjust_result.get('new_take_profit_pct')}% "
                f"SL={adjust_result.get('new_stop_loss_pct')}% "
                f"| {adjust_result.get('reason', '')}",
            )

        return adjust_result

    # ------------------------------------------------------------------ #
    #  매매 후 성과 평가 (기존 TradingAgent.evaluate_trade 대체)            #
    # ------------------------------------------------------------------ #
    def evaluate_trade(
        self,
        symbol: str,
        buy_price: float,
        sell_price: float,
        pnl_pct: float,
        held_minutes: float,
        exit_type: str,
        original_tp: float,
        original_sl: float,
        agent_reason: str,
        original_sl_1st: float | None = None,
        partial_sl_executed: bool = False,
        eval_stats: dict | None = None,
    ) -> TradeEvaluation:
        """포트폴리오 평가가에게 성과 평가 요청"""
        result = self._agents["portfolio_evaluator"].execute({
            "symbol": symbol,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "pnl_pct": pnl_pct,
            "held_minutes": held_minutes,
            "exit_type": exit_type,
            "original_tp": original_tp,
            "original_sl": original_sl,
            "agent_reason": agent_reason,
            "original_sl_1st": original_sl_1st,
            "partial_sl_executed": partial_sl_executed,
            "eval_stats": eval_stats,
        })
        evaluation = result.get("evaluation")
        if evaluation is None:
            evaluation = TradeEvaluation(
                evaluation="평가 실패 — 기존 전략 유지",
                suggested_tp_pct=round(original_tp, 2),
                suggested_sl_1st_pct=round(original_sl_1st or -1.0, 2),
                suggested_sl_pct=round(original_sl, 2),
                lesson="",
            )

        self._log_decision(
            "portfolio_evaluator", "evaluate",
            f"{symbol} {pnl_pct:+.2f}% {exit_type}",
            f"제안 TP=+{evaluation.suggested_tp_pct}% "
            f"SL={evaluation.suggested_sl_pct}% | {evaluation.lesson}",
        )

        return evaluation

    # ------------------------------------------------------------------ #
    #  총괄 평가 (6시간 주기, 스케줄러에서 호출)                               #
    # ------------------------------------------------------------------ #
    def run_meta_evaluation(self) -> list[AgentFeedback]:
        """전체 전문가 평가 실행 → 피드백 주입 + DB 저장"""
        logger.info("[Coordinator] 총괄 평가 시작...")

        # 최근 6시간 의사결정 기록
        decision_logs = self._repo.get_recent_decision_logs(hours=6)
        # 최근 매매 결과
        recent_evals = self._repo.get_recent_evaluations(limit=10)
        # 현재 점수
        current_scores = self.get_agent_scores()

        # 매매 결과 요약
        trade_results = [
            {
                "symbol": ev.symbol,
                "pnl_pct": ev.pnl_pct,
                "exit_type": ev.exit_type,
                "held_minutes": ev.held_minutes,
            }
            for ev in recent_evals
        ]

        # MetaEvaluator 실행
        result = self._meta.execute({
            "decision_logs": decision_logs,
            "trade_results": trade_results,
            "current_scores": current_scores,
        })
        feedbacks = result.get("feedbacks", [])

        if not feedbacks:
            logger.warning("[총괄 평가] 피드백 없음")
            return []

        # 평가 기간 라벨
        eval_period = datetime.now().strftime("%Y-%m-%d_%H")

        # 각 Agent에 피드백 주입 + DB 저장
        score_records = []
        for fb in feedbacks:
            agent = self._agents.get(fb.agent_role)
            if agent:
                feedback_text = (
                    f"점수: {fb.score}/100\n"
                    f"강점: {fb.strengths}\n"
                    f"약점: {fb.weaknesses}\n"
                    f"지시: {fb.directive}"
                )
                previous_score = agent.score
                agent.update_feedback(feedback_text, fb.score)
                logger.info(
                    f"[총괄 평가] {fb.agent_role}: "
                    f"{previous_score:.0f} → {fb.score:.0f}점 "
                    f"({fb.priority})"
                )

                score_records.append({
                    "agent_role": fb.agent_role,
                    "score": fb.score,
                    "previous_score": previous_score,
                    "strengths": fb.strengths,
                    "weaknesses": fb.weaknesses,
                    "directive": fb.directive,
                    "priority": fb.priority,
                    "eval_period": eval_period,
                })

        if score_records:
            try:
                self._repo.save_agent_scores(score_records)
            except Exception as e:
                logger.error(f"[총괄 평가 DB 저장 오류] {e}")

        return feedbacks
