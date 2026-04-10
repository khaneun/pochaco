"""Agent Coordinator — 7개 전문가 Agent 오케스트레이션 (v4.0 — 포트폴리오 기반)

TradingEngine은 이 클래스만 의존하며, 포트폴리오 단위로
코인 선정·전략 조정·성과 평가를 수행합니다.
"""
import logging
from datetime import datetime, timezone, timedelta

from database import TradeRepository
from .ai_agent import PortfolioDecision, TradeEvaluation
from .market_analyzer import CoinSnapshot
from .coin_selector import CoinScore
from .agents import (
    MarketAnalyst, MarketCondition,
    AssetManager, AllocationDecision,
    BuyStrategist,
    SellStrategist,
    PortfolioEvaluator,
    MetaEvaluator, AgentFeedback,
    CoinProfileAnalyst,
)
from .agents.meta_evaluator import AGENT_ROLES

_KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)


class AgentCoordinator:
    """7개 전문가 Agent를 오케스트레이션하는 코디네이터 (포트폴리오 기반)"""

    def __init__(
        self,
        market_analyst: MarketAnalyst,
        asset_manager: AssetManager,
        buy_strategist: BuyStrategist,
        sell_strategist: SellStrategist,
        portfolio_evaluator: PortfolioEvaluator,
        meta_evaluator: MetaEvaluator,
        repo: TradeRepository,
        coin_profile_analyst: CoinProfileAnalyst | None = None,
    ):
        self._agents: dict = {
            "market_analyst": market_analyst,
            "asset_manager": asset_manager,
            "buy_strategist": buy_strategist,
            "sell_strategist": sell_strategist,
            "portfolio_evaluator": portfolio_evaluator,
        }
        if coin_profile_analyst:
            self._agents["coin_profile_analyst"] = coin_profile_analyst
        self._meta = meta_evaluator
        self._coin_analyst: CoinProfileAnalyst | None = coin_profile_analyst
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
        return {role: agent.score for role, agent in self._agents.items()}

    def get_all_agents(self) -> dict:
        return self._agents

    def get_agent_prompt(self, role: str) -> dict | None:
        agent = self._agents.get(role)
        if not agent:
            return None
        return {
            "role": role,
            "base_prompt": agent.base_prompt,
            "feedback_prompt": agent.feedback_prompt,
        }

    def get_coin_profile(self, symbol: str) -> str | None:
        if not self._coin_analyst:
            return None
        return self._coin_analyst.get_profile(symbol)

    def list_coin_profiles(self) -> list[str]:
        if not self._coin_analyst:
            return []
        return self._coin_analyst.list_profiles()

    def chat_with_agent(self, role: str, message: str, history: list[dict]) -> str:
        agent = self._agents.get(role)
        if not agent:
            return f"에이전트 '{role}'을 찾을 수 없습니다."
        try:
            return agent.chat(message, history)
        except Exception as e:
            logger.error(f"[대화 오류] {role}: {e}")
            return f"대화 처리 중 오류 발생: {e}"

    def update_agent_prompt(self, role: str, new_prompt: str) -> bool:
        agent = self._agents.get(role)
        if not agent:
            return False
        agent.update_base_prompt(new_prompt)
        logger.info(f"[프롬프트 업데이트] {role}: {len(new_prompt)}자")
        return True

    # ------------------------------------------------------------------ #
    #  DB에서 피드백 복원 (재시작 시)                                         #
    # ------------------------------------------------------------------ #
    def restore_feedbacks_from_db(self) -> None:
        """DB에 저장된 최근 피드백을 각 Agent에 누적 로드 (최대 3회분)"""
        try:
            for role, agent in self._agents.items():
                history = self._repo.get_agent_score_history(role, limit=3)
                if not history:
                    continue

                # 오래된 것부터 순서대로 적용하여 누적 히스토리 구축
                for score_record in reversed(history):
                    feedback_text = (
                        f"점수: {score_record.score}/100\n"
                        f"강점: {score_record.strengths}\n"
                        f"약점: {score_record.weaknesses}\n"
                        f"지시: {score_record.directive}"
                    )
                    agent.update_feedback(feedback_text, score_record.score)

                latest = history[0]
                logger.info(
                    f"[피드백 복원] {role}: "
                    f"{latest.score:.0f}점 ({len(history)}회분 누적)"
                )
        except Exception as e:
            logger.warning(f"[피드백 복원 실패] {e}")

    # ------------------------------------------------------------------ #
    #  의사결정 로그 저장 헬퍼                                                #
    # ------------------------------------------------------------------ #
    def _log_decision(
        self, agent_role: str, decision_type: str,
        input_summary: str, output_summary: str,
        portfolio_id: int | None = None,
    ) -> None:
        try:
            self._repo.save_decision_log(
                agent_role=agent_role,
                decision_type=decision_type,
                input_summary=input_summary[:500],
                output_summary=output_summary[:500],
                portfolio_id=portfolio_id,
            )
        except Exception as e:
            logger.warning(f"[의사결정 로그 저장 실패] {e}")

    # ------------------------------------------------------------------ #
    #  포트폴리오 선정 (8개 코인)                                            #
    # ------------------------------------------------------------------ #
    def select_portfolio(
        self,
        snapshots: list[CoinSnapshot],
        eval_stats: dict | None = None,
        coin_scores: list[CoinScore] | None = None,
    ) -> PortfolioDecision:
        """시장 분석 → 자산 배분 → 8개 코인 포트폴리오 선정 파이프라인"""

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
            f"투자={'Y' if allocation.should_invest else 'N'} "
            f"비율={allocation.invest_ratio:.0%} | {allocation.reason}",
        )

        if not allocation.should_invest:
            raise RuntimeError(
                f"[자산 운용가 판단] 투자 보류: {allocation.reason}"
            )

        # 3) 특성 분석가 — 후보 코인별 프로파일 수집
        coin_profiles: dict[str, str] = {}
        if self._coin_analyst:
            for s in snapshots:
                profile = self._coin_analyst.get_profile(s.symbol)
                if profile:
                    coin_profiles[s.symbol] = profile
            if coin_profiles:
                logger.info(
                    f"[Coordinator] 특성 분석가 프로파일 로드: "
                    f"{list(coin_profiles.keys())}"
                )

        # 4) 매수 전문가 — 8개 코인 포트폴리오 선정
        logger.info("[Coordinator] 매수 전문가 포트폴리오 구성 중...")
        buy_result = self._agents["buy_strategist"].execute({
            "snapshots": snapshots,
            "market_condition": condition,
            "allocation": allocation,
            "eval_stats": eval_stats,
            "coin_scores": coin_scores,
            "coin_profiles": coin_profiles,
        })
        decision = buy_result.get("portfolio_decision")
        if decision is None:
            raise RuntimeError("[매수 전문가] 포트폴리오 구성 실패")

        symbols = [c.symbol for c in decision.coins]
        self._log_decision(
            "buy_strategist", "portfolio_select",
            f"후보 {len(snapshots)}개 / 시장={condition.sentiment}",
            f"{','.join(symbols)} TP=+{decision.take_profit_pct}% "
            f"SL={decision.stop_loss_pct}%",
        )

        return decision

    # ------------------------------------------------------------------ #
    #  전략 동적 조정 (포트폴리오 레벨)                                       #
    # ------------------------------------------------------------------ #
    def should_adjust_strategy(
        self,
        portfolio_name: str,
        combined_pnl_pct: float,
        holding_minutes: int,
        original_tp: float,
        original_sl: float,
        coin_details: list[dict],
        tier1_sold: bool = False,
        tier2_sold: bool = False,
    ) -> dict:
        """매도 전문가에게 포트폴리오 TP/SL 조정 질의"""
        result = self._agents["sell_strategist"].execute({
            "portfolio_name": portfolio_name,
            "combined_pnl_pct": combined_pnl_pct,
            "holding_minutes": holding_minutes,
            "original_tp": original_tp,
            "original_sl": original_sl,
            "coin_details": coin_details,
            "tier1_sold": tier1_sold,
            "tier2_sold": tier2_sold,
        })
        adjust_result = result.get("adjust_result", {
            "adjust": False,
            "new_take_profit_pct": original_tp,
            "new_stop_loss_pct": original_sl,
            "reason": "폴백",
        })

        if adjust_result.get("adjust"):
            self._log_decision(
                "sell_strategist", "exit_adjust",
                f"{portfolio_name} PnL={combined_pnl_pct:+.2f}% {holding_minutes}분",
                f"TP={adjust_result.get('new_take_profit_pct')}% "
                f"SL={adjust_result.get('new_stop_loss_pct')}% "
                f"| {adjust_result.get('reason', '')}",
            )

        return adjust_result

    # ------------------------------------------------------------------ #
    #  매매 후 성과 평가 (포트폴리오 단위)                                     #
    # ------------------------------------------------------------------ #
    def evaluate_trade(
        self,
        portfolio_name: str,
        total_buy_krw: float,
        total_sell_krw: float,
        combined_pnl_pct: float,
        held_minutes: float,
        exit_type: str,
        original_tp: float,
        original_sl: float,
        coin_results: list[dict],
        portfolio_reason: str = "",
        eval_stats: dict | None = None,
    ) -> TradeEvaluation:
        """포트폴리오 평가가에게 성과 평가 요청"""
        result = self._agents["portfolio_evaluator"].execute({
            "portfolio_name": portfolio_name,
            "total_buy_krw": total_buy_krw,
            "total_sell_krw": total_sell_krw,
            "combined_pnl_pct": combined_pnl_pct,
            "held_minutes": held_minutes,
            "exit_type": exit_type,
            "original_tp": original_tp,
            "original_sl": original_sl,
            "coin_results": coin_results,
            "portfolio_reason": portfolio_reason,
            "eval_stats": eval_stats,
        })
        evaluation = result.get("evaluation")
        if evaluation is None:
            evaluation = TradeEvaluation(
                evaluation="평가 실패 — 기존 전략 유지",
                suggested_tp_pct=round(original_tp, 2),
                suggested_sl_pct=round(original_sl, 2),
                lesson="",
            )

        self._log_decision(
            "portfolio_evaluator", "evaluate",
            f"{portfolio_name} {combined_pnl_pct:+.2f}% {exit_type}",
            f"제안 TP=+{evaluation.suggested_tp_pct}% "
            f"SL={evaluation.suggested_sl_pct}% | {evaluation.lesson}",
        )

        # 특성 분석가 — 포트폴리오 내 코인들 프로파일 업데이트
        if self._coin_analyst:
            for cr in coin_results:
                try:
                    self._coin_analyst.execute({
                        "symbol": cr.get("symbol", ""),
                        "buy_price": cr.get("buy_price", 0),
                        "sell_price": cr.get("sell_price", 0),
                        "pnl_pct": cr.get("pnl_pct", 0),
                        "held_minutes": held_minutes,
                        "exit_type": exit_type,
                        "agent_reason": cr.get("reason", ""),
                        "original_tp": original_tp,
                        "original_sl": original_sl,
                        "evaluation": evaluation.evaluation,
                        "lesson": evaluation.lesson,
                        "trade_time": datetime.now(tz=_KST).strftime("%Y-%m-%d %H:%M"),
                    })
                except Exception as e:
                    logger.warning(f"[특성 분석가] {cr.get('symbol', '')} 업데이트 오류: {e}")

        return evaluation

    # ------------------------------------------------------------------ #
    #  총괄 평가 (6시간 주기, 스케줄러에서 호출)                               #
    # ------------------------------------------------------------------ #
    def run_meta_evaluation(self) -> list[AgentFeedback]:
        """전체 전문가 평가 실행 → 피드백 주입 + DB 저장"""
        logger.info("[Coordinator] 총괄 평가 시작...")

        decision_logs = self._repo.get_recent_decision_logs(hours=6)
        recent_evals = self._repo.get_recent_evaluations(limit=10)
        current_scores = self.get_agent_scores()

        trade_results = [
            {
                "portfolio_name": ev.portfolio_name,
                "pnl_pct": ev.pnl_pct,
                "exit_type": ev.exit_type,
                "held_minutes": ev.held_minutes,
            }
            for ev in recent_evals
        ]

        result = self._meta.execute({
            "decision_logs": decision_logs,
            "trade_results": trade_results,
            "current_scores": current_scores,
        })
        feedbacks = result.get("feedbacks", [])

        if not feedbacks:
            logger.warning("[총괄 평가] 피드백 없음")
            return []

        eval_period = datetime.now().strftime("%Y-%m-%d_%H")

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
