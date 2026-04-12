"""Agent Coordinator — 8개 전문가 Agent 오케스트레이션 (v4.1 — 합의 기반)

TradingEngine은 이 클래스만 의존하며, 포트폴리오 단위로
코인 선정·전략 조정·성과 평가를 수행합니다.

투자 판단은 자산 운용가(보수적) vs 투자 전문가(공격적) 양측 의견을
시장 분석가·포트폴리오 평가가·특성 분석가 의견과 종합하여 결정합니다.
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
    InvestmentStrategist, InvestmentOpinion,
    BuyStrategist,
    SellStrategist,
    PortfolioEvaluator,
    MetaEvaluator, AgentFeedback,
    CoinProfileAnalyst,
)
from .agents.meta_evaluator import AGENT_ROLES

_KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)

# ── 합의 메커니즘 가중치 ──
# 자산 운용가(보수) : 투자 전문가(공격) : 시장 분석가(시장 권장 비율)
_W_ASSET_MANAGER = 0.35
_W_INVESTMENT_STRATEGIST = 0.40
_W_MARKET_ANALYST = 0.25


class InvestmentHoldError(Exception):
    """투자 보류 결정 시 발생하는 예외.

    일반 오류(RuntimeError)와 구분하여, TradingEngine이
    '중요 알람' 발송 + 지정 대기 후 조용히 재시도하도록 처리합니다.
    """


class AgentCoordinator:
    """8개 전문가 Agent를 오케스트레이션하는 코디네이터 (합의 기반)"""

    def __init__(
        self,
        market_analyst: MarketAnalyst,
        asset_manager: AssetManager,
        investment_strategist: InvestmentStrategist,
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
            "investment_strategist": investment_strategist,
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
    #  합의 메커니즘: 자산 운용가 vs 투자 전문가 의견 종합                       #
    # ------------------------------------------------------------------ #
    def _synthesize_investment_decision(
        self,
        allocation: AllocationDecision,
        opinion: InvestmentOpinion,
        condition: MarketCondition,
    ) -> tuple[bool, float, str]:
        """양측 의견 + 시장 분석을 종합하여 최종 투자 결정

        Returns:
            (should_invest, final_ratio, consensus_reason)
        """
        am_invest = allocation.should_invest
        is_invest = opinion.should_invest

        # ── 투자 여부 결정 ──
        if not am_invest and not is_invest:
            # 양측 모두 반대 → 보류 확정
            return False, 0.0, (
                f"[합의: 보류] 운용가: {allocation.reason} / "
                f"전문가: {opinion.reason}"
            )

        if am_invest and is_invest:
            # 양측 모두 찬성 → 가중 평균 비율
            final_ratio = (
                allocation.invest_ratio * _W_ASSET_MANAGER
                + opinion.invest_ratio * _W_INVESTMENT_STRATEGIST
                + condition.recommended_exposure * _W_MARKET_ANALYST
            )
            final_ratio = max(0.3, min(0.95, final_ratio))
            return True, round(final_ratio, 2), (
                f"[합의: 투자] 운용가 {allocation.invest_ratio:.0%} + "
                f"전문가 {opinion.invest_ratio:.0%} + "
                f"시장 {condition.recommended_exposure:.0%} "
                f"→ {final_ratio:.0%}"
            )

        if is_invest and not am_invest:
            # 투자 전문가만 찬성 → 기회 수준이 높으면 소극 투자
            if opinion.opportunity_score >= 0.7:
                # 높은 확신 → 투자하되 보수적 비율
                conservative_ratio = min(
                    opinion.invest_ratio * 0.6,
                    0.6,
                )
                conservative_ratio = max(0.3, conservative_ratio)
                return True, round(conservative_ratio, 2), (
                    f"[합의: 소극 투자] 운용가 반대, "
                    f"전문가 기회지수 {opinion.opportunity_score:.1f}로 소극 투자 "
                    f"({conservative_ratio:.0%})"
                )
            else:
                return False, 0.0, (
                    f"[합의: 보류] 운용가 반대 + "
                    f"전문가 기회지수 낮음({opinion.opportunity_score:.1f})"
                )

        # am_invest and not is_invest
        # 운용가만 찬성, 투자 전문가 반대 → 보수적 투자
        conservative_ratio = min(allocation.invest_ratio * 0.7, 0.6)
        conservative_ratio = max(0.3, conservative_ratio)
        return True, round(conservative_ratio, 2), (
            f"[합의: 보수 투자] 전문가 반대, "
            f"운용가 비율 축소 ({conservative_ratio:.0%})"
        )

    # ------------------------------------------------------------------ #
    #  포트폴리오 선정 (8개 코인) — 합의 기반                                 #
    # ------------------------------------------------------------------ #
    def select_portfolio(
        self,
        snapshots: list[CoinSnapshot],
        eval_stats: dict | None = None,
        coin_scores: list[CoinScore] | None = None,
        krw_balance: float = 0.0,
    ) -> PortfolioDecision:
        """시장 분석 → 운용가/전문가 합의 → 8개 코인 포트폴리오 선정"""

        # 1) 시장 분석가
        logger.info("[Coordinator] 1단계: 시장 분석가 분석 중...")
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

        # 후보 코인 요약 (투자 전문가 참고용)
        coin_scores_summary = ""
        if coin_scores:
            top_5 = sorted(coin_scores, key=lambda c: c.total_score, reverse=True)[:5]
            lines = [f"  {c.symbol}: 점수={c.total_score:.1f}" for c in top_5]
            coin_scores_summary = "\n".join(lines)

        # 2) 자산 운용가 (보수적)
        logger.info("[Coordinator] 2단계: 자산 운용가 배분 결정 중...")
        alloc_result = self._agents["asset_manager"].execute({
            "market_condition": condition,
            "eval_stats": eval_stats,
            "krw_balance": krw_balance,
        })
        allocation = alloc_result.get("allocation", AllocationDecision(
            should_invest=True, invest_ratio=0.85, reason="기본 배분",
        ))
        self._log_decision(
            "asset_manager", "allocation",
            f"시장={condition.sentiment} 리스크={condition.risk_level}",
            f"투자={'Y' if allocation.should_invest else 'N'} "
            f"비율={allocation.invest_ratio:.0%} | {allocation.reason}",
        )

        # 3) 투자 전문가 (공격적)
        logger.info("[Coordinator] 2단계: 투자 전문가 기회 판단 중...")
        invest_result = self._agents["investment_strategist"].execute({
            "market_condition": condition,
            "eval_stats": eval_stats,
            "krw_balance": krw_balance,
            "coin_scores_summary": coin_scores_summary,
        })
        opinion = invest_result.get("opinion", InvestmentOpinion(
            should_invest=True, invest_ratio=0.80,
            aggression=0.5, opportunity_score=0.5, reason="기본 의견",
        ))
        self._log_decision(
            "investment_strategist", "opportunity_assessment",
            f"시장={condition.sentiment} 리스크={condition.risk_level}",
            f"투자={'Y' if opinion.should_invest else 'N'} "
            f"비율={opinion.invest_ratio:.0%} 기회={opinion.opportunity_score:.1f} "
            f"공격={opinion.aggression:.1f} | {opinion.reason}",
        )

        # 4) 합의 도출
        should_invest, final_ratio, consensus_reason = (
            self._synthesize_investment_decision(allocation, opinion, condition)
        )

        logger.info(
            f"[Coordinator] 합의 결과: 투자={'Y' if should_invest else 'N'} "
            f"비율={final_ratio:.0%} | {consensus_reason}"
        )
        self.last_invest_ratio = final_ratio

        if not should_invest:
            raise InvestmentHoldError(consensus_reason)

        # AllocationDecision에 최종 비율 반영 (매수 전문가에 전달)
        final_allocation = AllocationDecision(
            should_invest=True,
            invest_ratio=final_ratio,
            reason=consensus_reason,
        )

        # 5) 특성 분석가 — 후보 코인별 프로파일 수집
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

        # 6) 매수 전문가 — 8개 코인 포트폴리오 선정
        logger.info("[Coordinator] 3단계: 매수 전문가 포트폴리오 구성 중...")
        buy_result = self._agents["buy_strategist"].execute({
            "snapshots": snapshots,
            "market_condition": condition,
            "allocation": final_allocation,
            "eval_stats": eval_stats,
            "coin_scores": coin_scores,
            "coin_profiles": coin_profiles,
            "investment_opinion": opinion,
        })
        decision = buy_result.get("portfolio_decision")
        if decision is None:
            raise RuntimeError("[매수 전문가] 포트폴리오 구성 실패")

        symbols = [c.symbol for c in decision.coins]
        self._log_decision(
            "buy_strategist", "portfolio_select",
            f"후보 {len(snapshots)}개 / 합의비율={final_ratio:.0%}",
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
