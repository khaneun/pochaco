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
    AssetManager, AllocationDecision, PyramidDecision,
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

# ── 서킷 브레이커 자동 해제 (시간 기반) ──
# 매매가 멈춰 통계가 갱신되지 않으면 보류가 영원히 풀리지 않는 데드락 방지.
# 마지막 evaluation으로부터 N시간 경과 시 단 1회 보수적 진입 허용.
_CIRCUIT_BREAKER_RESET_HOURS = 6.0
# 시간 해제 후 강제 진입 시 적용할 보수적 비율 (자본 보호)
_FORCED_RETRY_RATIO = 0.3


class InsufficientCandidatesError(Exception):
    """매수 후보 코인 부족 시 발생하는 예외.

    쿨다운·필터 등으로 유효한 후보가 3개 미만일 때 발생하며,
    TradingEngine이 조용히 대기 후 재시도하도록 처리합니다.
    """


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

        # 시작 시 반성문 요약 복원
        self._restore_reflection_summaries()

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

    def _restore_reflection_summaries(self) -> None:
        """시작 시 DB에서 반성문 요약을 각 Agent에 복원"""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        for role, agent in self._agents.items():
            try:
                summary = self._repo.get_agent_reflection_prompt_summary(role, limit=3)
                if summary:
                    agent.update_reflection_summary(summary)
                    _log.info(f"[반성문 복원] {role}: {len(summary)}자")
            except Exception as e:
                _log.debug(f"[반성문 복원 실패] {role}: {e}")

    def get_agent_prompt(self, role: str) -> dict | None:
        agent = self._agents.get(role)
        if not agent:
            return None
        return {
            "role": role,
            "base_prompt": agent.base_prompt,
            "feedback_prompt": agent.feedback_prompt,
            "reflection_summary": agent.reflection_summary,
        }

    def get_coin_profile(self, symbol: str) -> str | None:
        if not self._coin_analyst:
            return None
        return self._coin_analyst.get_profile(symbol)

    def list_coin_profiles(self) -> list[str]:
        if not self._coin_analyst:
            return []
        return self._coin_analyst.list_profiles()

    # ------------------------------------------------------------------ #
    #  블랙리스트 / 쿨다운 (대시보드용)                                       #
    # ------------------------------------------------------------------ #
    def get_blacklist(self) -> list[dict]:
        """현재 블랙리스트 스냅샷 (대시보드 전용).

        Returns:
            [{symbol, days_left, reason, expires_at_kst}, ...]
        """
        from . import cooldown as _cd
        import time as _time
        snap = _cd.get_blacklist_snapshot()
        now = _time.time()
        items: list[dict] = []
        for symbol, (expiry, reason) in snap.items():
            if expiry <= now:
                continue
            days_left = (expiry - now) / 86400.0
            expires_dt = datetime.fromtimestamp(expiry, tz=_KST)
            items.append({
                "symbol": symbol,
                "days_left": round(days_left, 1),
                "reason": reason,
                "expires_at_kst": expires_dt.strftime("%Y-%m-%d %H:%M"),
            })
        items.sort(key=lambda x: -x["days_left"])
        return items

    def remove_from_blacklist(self, symbol: str) -> bool:
        """블랙리스트에서 즉시 해제 (대시보드 수동 조작).

        Args:
            symbol: 코인 심볼 (대문자)

        Returns:
            제거 성공 여부 (해당 심볼이 없었으면 False)
        """
        from . import cooldown as _cd
        sym = (symbol or "").upper()
        with _cd._lock:  # type: ignore[attr-defined]
            existed = sym in _cd._blacklist  # type: ignore[attr-defined]
            if existed:
                _cd._blacklist.pop(sym, None)  # type: ignore[attr-defined]
        if existed:
            logger.info(f"[블랙리스트 즉시 해제] {sym} (대시보드 수동 조작)")
        return existed

    def reevaluate_coin(self, symbol: str) -> dict:
        """특정 코인을 시장 분석가·포트폴리오 평가가에게 재평가 의뢰.

        해당 코인의 프로파일·블랙리스트 사유를 입력으로 하여
        '지금 해제해도 좋은지' 판단을 받는다.

        Args:
            symbol: 코인 심볼

        Returns:
            {
              "symbol": str,
              "verdict": "release" | "keep" | "uncertain",
              "confidence": 0~1,
              "reason": str,
              "market_view": str,        # 시장 분석가 의견
              "profile_view": str,       # 포트폴리오 평가가 의견
            }
        """
        from . import cooldown as _cd
        sym = (symbol or "").upper()

        # 블랙리스트 사유 조회
        snap = _cd.get_blacklist_snapshot()
        bl_reason = snap.get(sym, (0.0, ""))[1]

        # 코인 프로파일
        profile = self._coin_analyst.get_profile(sym) if self._coin_analyst else None
        profile_text = profile or "(프로파일 없음 — 매매 이력 부족)"

        # 시장 분석가에게 단일 코인 의견 질의
        market_summary = ""
        try:
            market = self._agents.get("market_analyst")
            if market:
                cond = self.last_market_condition
                if cond:
                    market_summary = (
                        f"현재 시장: {cond.sentiment} / 리스크 {cond.risk_level} / "
                        f"권장노출 {cond.recommended_exposure:.0%} / "
                        f"{cond.summary}"
                    )
                else:
                    market_summary = "(직전 시장 분석 데이터 없음)"
        except Exception as e:
            market_summary = f"(시장 데이터 조회 실패: {e})"

        # 포트폴리오 평가가에게 LLM 질의 (해제 가능성 판단)
        try:
            evaluator = self._agents.get("portfolio_evaluator")
            if not evaluator:
                return {
                    "symbol": sym,
                    "verdict": "uncertain",
                    "confidence": 0.0,
                    "reason": "포트폴리오 평가가 없음",
                    "market_view": market_summary,
                    "profile_view": profile_text,
                }

            task_prompt = (
                f"다음 코인의 블랙리스트 해제 여부를 판단하세요.\n\n"
                f"【코인】 {sym}\n"
                f"【블랙리스트 사유】 {bl_reason or '(없음)'}\n\n"
                f"【시장 상황】\n{market_summary}\n\n"
                f"【과거 특성 프로파일】\n{profile_text}\n\n"
                f"위 정보를 종합하여 지금 이 코인을 다시 매매 후보로 풀어줘도 될지 판단하세요.\n"
                f"JSON으로만 응답 (코드블록 없이):\n"
                f'{{"verdict": "release" | "keep" | "uncertain", '
                f'"confidence": 0.0~1.0, '
                f'"reason": "30~120자 한국어 사유"}}'
            )
            raw = evaluator._call_llm(task_prompt, max_tokens=400)  # type: ignore[attr-defined]
            data = evaluator._parse_json(raw)  # type: ignore[attr-defined]
            verdict = data.get("verdict", "uncertain")
            if verdict not in ("release", "keep", "uncertain"):
                verdict = "uncertain"
            return {
                "symbol": sym,
                "verdict": verdict,
                "confidence": float(data.get("confidence", 0.5) or 0.5),
                "reason": data.get("reason", ""),
                "market_view": market_summary,
                "profile_view": profile_text,
            }
        except Exception as e:
            logger.warning(f"[코인 재평가] {sym} 실패: {e}")
            return {
                "symbol": sym,
                "verdict": "uncertain",
                "confidence": 0.0,
                "reason": f"평가 실패: {e}",
                "market_view": market_summary,
                "profile_view": profile_text,
            }

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
        eval_stats: dict | None = None,
    ) -> tuple[bool, float, str]:
        """양측 의견 + 시장 분석을 종합하여 최종 투자 결정

        반복 손실/약세장에서 LLM이 무리하게 매수하는 사고를 막기 위해
        하기 안전장치를 적용한다 (코드 레벨, LLM 무시 우선):
          ① 데이터 서킷 브레이커: 최근 5건 중 4건 이상 손실이면 즉시 보류
          ② 시장 차단: recommended_exposure ≤ 0.3 또는 risk=high+bearish 보류
          ③ 운용가 강력 보류 우선: AssetManager가 invest_ratio≤0.4로 보류 시
             InvestmentStrategist 의견 무시
          ④ opportunity_score 임계 0.7 → 0.85 (단독 진입 더 어렵게)

        Returns:
            (should_invest, final_ratio, consensus_reason)
        """
        am_invest = allocation.should_invest
        is_invest = opinion.should_invest

        # ── 시간 기반 자동 해제 판단 (데드락 방지) ──
        # 마지막 evaluation으로부터 _CIRCUIT_BREAKER_RESET_HOURS 이상 경과 시
        # 서킷 브레이커/운용가 강력 보류를 1회 우회하여 보수적 진입을 허용한다.
        # (매매가 발생해야만 통계가 갱신되어 보류가 자연 해제될 수 있기 때문)
        hours_idle = (eval_stats or {}).get("hours_since_last_eval")
        breaker_reset = (
            hours_idle is not None and hours_idle >= _CIRCUIT_BREAKER_RESET_HOURS
        )

        # ── ① 데이터 서킷 브레이커 — LLM 무시, 최근 손실 비율 기반 강제 보류 ──
        if eval_stats and eval_stats.get("count", 0) >= 5:
            recent = eval_stats.get("recent_trades", [])[:5]
            losses = sum(1 for t in recent if t.get("pnl_pct", 0) < 0)
            avg_pnl = eval_stats.get("avg_pnl_pct", 0)
            if losses >= 4:
                if breaker_reset:
                    logger.warning(
                        f"[서킷 브레이커 시간 해제] {hours_idle:.1f}h 경과 — "
                        f"손실 {losses}/5건이지만 1회 보수적 진입 허용"
                    )
                else:
                    return False, 0.0, (
                        f"[합의: 보류 — 서킷 브레이커] 최근 5건 중 {losses}건 손실 "
                        f"(평균 {avg_pnl:+.2f}%) → "
                        f"{_CIRCUIT_BREAKER_RESET_HOURS:.0f}시간 이상 휴식 후 재시도 "
                        f"(현재 {hours_idle:.1f}h)" if hours_idle is not None
                        else f"[합의: 보류 — 서킷 브레이커] 최근 5건 중 {losses}건 손실 "
                             f"(평균 {avg_pnl:+.2f}%) → 6시간 이상 휴식 후 재시도"
                    )
            elif losses >= 3 and avg_pnl <= -1.5:
                if breaker_reset:
                    logger.warning(
                        f"[서킷 브레이커 시간 해제] {hours_idle:.1f}h 경과 — "
                        f"평균 {avg_pnl:+.2f}%지만 1회 보수적 진입 허용"
                    )
                else:
                    return False, 0.0, (
                        f"[합의: 보류 — 서킷 브레이커] 최근 5건 중 {losses}건 손실 + "
                        f"평균 {avg_pnl:+.2f}% (-1.5% 이하) → 약세 누적, 진입 차단"
                    )

        # ── ② 시장 차단 — 시장 분석가의 강력 약세 신호 우선 (시간 해제 무시) ──
        if condition.recommended_exposure <= 0.3:
            return False, 0.0, (
                f"[합의: 보류 — 시장 약세] 시장 권장비율 {condition.recommended_exposure:.0%} "
                f"≤ 30% / {condition.summary[:80]}"
            )
        if condition.risk_level == "high" and condition.sentiment == "bearish":
            return False, 0.0, (
                f"[합의: 보류 — 시장 위험] high risk + bearish / {condition.summary[:80]}"
            )

        # ── ②-2 시장 분석가 저점수 + 최근 2건 손절 → 1시간 보류 (v4.4) ──
        # 시장 판단이 흔들리는데 연속 손절이면 추가 진입 금지
        market_analyst = self._agents.get("market_analyst")
        market_score = market_analyst.score if market_analyst else 50.0
        if market_score < 30.0 and eval_stats and eval_stats.get("count", 0) >= 2:
            recent_2 = eval_stats.get("recent_trades", [])[:2]
            if len(recent_2) >= 2 and all(t.get("pnl_pct", 0) < 0 for t in recent_2):
                return False, 0.0, (
                    f"[합의: 보류 — 시장분석가 저점수+연속손실] "
                    f"market_analyst {market_score:.0f}점/100, 직전 2건 손절 "
                    f"({recent_2[0].get('pnl_pct',0):+.2f}%, {recent_2[1].get('pnl_pct',0):+.2f}%) "
                    f"→ 1시간 후 재평가"
                )

        # ── ③ 운용가 강력 보류 → InvestmentStrategist 의견 무시 ──
        # AssetManager invest_ratio≤0.4은 "강력 보류" 신호 (승률 낮고 연속 손실 등)
        if not am_invest and allocation.invest_ratio <= 0.4:
            if breaker_reset:
                logger.warning(
                    f"[운용가 강력 보류 시간 해제] {hours_idle:.1f}h 경과 — "
                    f"운용가 비율 {allocation.invest_ratio:.0%}이지만 1회 보수적 진입 허용"
                )
                return True, _FORCED_RETRY_RATIO, (
                    f"[합의: 강제 재시도] {hours_idle:.1f}h 경과로 데드락 해제 → "
                    f"보수 비율 {_FORCED_RETRY_RATIO:.0%} 1회 진입 / "
                    f"운용가 사유: {allocation.reason[:80]}"
                )
            return False, 0.0, (
                f"[합의: 보류 — 운용가 강력 보류] 비율 {allocation.invest_ratio:.0%} ≤ 40% / "
                f"{allocation.reason[:120]}"
            )

        # 서킷 브레이커가 시간 해제로 통과한 경우 — 보수적 강제 진입
        if breaker_reset and not am_invest and not is_invest:
            return True, _FORCED_RETRY_RATIO, (
                f"[합의: 강제 재시도] {hours_idle:.1f}h 경과로 데드락 해제 → "
                f"보수 비율 {_FORCED_RETRY_RATIO:.0%} 1회 진입"
            )

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
            # ④ 전문가만 찬성 → 기회지수 0.85 이상이고 시장 risk=low일 때만 소극 투자
            if opinion.opportunity_score >= 0.85 and condition.risk_level == "low":
                conservative_ratio = min(opinion.invest_ratio * 0.5, 0.5)
                conservative_ratio = max(0.3, conservative_ratio)
                return True, round(conservative_ratio, 2), (
                    f"[합의: 소극 투자] 운용가 반대, 전문가 기회지수 "
                    f"{opinion.opportunity_score:.1f}≥0.85 + low risk → {conservative_ratio:.0%}"
                )
            return False, 0.0, (
                f"[합의: 보류] 운용가 반대 + 전문가 기회지수 "
                f"{opinion.opportunity_score:.1f} (임계 0.85 미달) "
                f"또는 시장 리스크 {condition.risk_level}"
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

        # 4) 합의 도출 (eval_stats 전달 — 데이터 서킷 브레이커용)
        should_invest, final_ratio, consensus_reason = (
            self._synthesize_investment_decision(allocation, opinion, condition, eval_stats)
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

        # 5) 특성 분석가 — 후보 코인별 프로파일 수집 + 적극 조언
        coin_profiles: dict[str, str] = {}
        coin_profile_advisory: str = ""
        if self._coin_analyst:
            candidate_symbols = [s.symbol for s in snapshots]
            for s in snapshots:
                profile = self._coin_analyst.get_profile(s.symbol)
                if profile:
                    coin_profiles[s.symbol] = profile
            if coin_profiles:
                logger.info(
                    f"[Coordinator] 특성 분석가 프로파일 로드: "
                    f"{list(coin_profiles.keys())}"
                )
                logger.info("[Coordinator] 특성 분석가 조언 요청 중...")
                coin_profile_advisory = self._coin_analyst.consult(candidate_symbols)
                if coin_profile_advisory:
                    logger.info(f"[Coordinator] 특성 분석가 조언:\n{coin_profile_advisory}")

        # 6) 매수 전문가 — 8개 코인 포트폴리오 선정
        logger.info("[Coordinator] 3단계: 매수 전문가 포트폴리오 구성 중...")
        buy_result = self._agents["buy_strategist"].execute({
            "snapshots": snapshots,
            "market_condition": condition,
            "allocation": final_allocation,
            "eval_stats": eval_stats,
            "coin_scores": coin_scores,
            "coin_profiles": coin_profiles,
            "coin_profile_advisory": coin_profile_advisory,
            "investment_opinion": opinion,
        })
        decision = buy_result.get("portfolio_decision")
        if decision is None:
            raise InsufficientCandidatesError("[매수 전문가] 유효 후보 코인 부족 — 이번 사이클 스킵")

        symbols = [c.symbol for c in decision.coins]
        self._log_decision(
            "buy_strategist", "portfolio_select",
            f"후보 {len(snapshots)}개 / 합의비율={final_ratio:.0%}",
            f"{','.join(symbols)} TP=+{decision.take_profit_pct}% "
            f"SL={decision.stop_loss_pct}%",
        )

        return decision

    # ------------------------------------------------------------------ #
    #  피라미딩 추가 매수 판단                                                #
    # ------------------------------------------------------------------ #
    def decide_pyramid(self, context: dict) -> PyramidDecision:
        """자산 운용가에게 피라미딩 추가 매수 여부 질의"""
        decision = self._agents["asset_manager"].decide_pyramid(context)
        if decision.should_pyramid:
            self._log_decision(
                "asset_manager", "pyramid",
                f"pnl={context.get('current_pnl_pct', 0):+.2f}% "
                f"avail={context.get('krw_available', 0):,.0f}원",
                f"threshold={decision.threshold_pct}% add={decision.add_ratio:.0%} | {decision.reason}",
            )
        return decision

    # ------------------------------------------------------------------ #
    #  전략 동적 조정 (포트폴리오 레벨)                                       #
    # ------------------------------------------------------------------ #
    def evaluate_tier1_sell(
        self,
        portfolio_name: str,
        pnl_pct: float,
        coin_details: list[dict],
        holding_minutes: int,
    ) -> dict:
        """Tier1(-1.0%) 진입 시 AI 기민 평가 → 즉각 매도 비율 결정

        Returns:
            {"sell_ratio": float(0.33~0.67), "reason": str}
        """
        result = self._agents["sell_strategist"].evaluate_tier1_action({
            "portfolio_name": portfolio_name,
            "pnl_pct": pnl_pct,
            "coin_details": coin_details,
            "holding_minutes": holding_minutes,
        })
        ev = result.get("tier1_result", {"sell_ratio": 0.5, "reason": "기본값"})
        self._log_decision(
            "sell_strategist", "tier1_eval",
            f"{portfolio_name} PnL={pnl_pct:+.2f}% {holding_minutes}분",
            f"매도비율={ev.get('sell_ratio', 0.5):.0%} | {ev.get('reason', '')}",
        )
        return ev

    def should_adjust_strategy(
        self,
        portfolio_name: str,
        combined_pnl_pct: float,
        holding_minutes: int,
        original_tp: float,
        original_sl: float,
        coin_details: list[dict],
        tier1_sold: bool = False,
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
            updated_symbols = []
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
                    updated_symbols.append(
                        f"{cr.get('symbol','')}({cr.get('pnl_pct',0):+.1f}%)"
                    )
                except Exception as e:
                    logger.warning(f"[특성 분석가] {cr.get('symbol', '')} 업데이트 오류: {e}")
            if updated_symbols:
                self._log_decision(
                    "coin_profile_analyst", "profile_update",
                    input_summary=f"포트폴리오 {portfolio_name} 청산 ({exit_type})",
                    output_summary=", ".join(updated_symbols),
                )

        return evaluation

    # ------------------------------------------------------------------ #
    #  총괄 평가 (3시간 주기, 스케줄러에서 호출)                               #
    # ------------------------------------------------------------------ #
    def run_meta_evaluation(self) -> list[AgentFeedback]:
        """전체 전문가 평가 실행 → 피드백 주입 + DB 저장"""
        logger.info("[Coordinator] 총괄 평가 시작...")

        decision_logs = self._repo.get_recent_decision_logs(hours=3)
        recent_evals = self._repo.get_recent_evaluations(limit=10)
        current_scores = self.get_agent_scores()

        trade_results = [
            {
                "portfolio_name": ev.portfolio_name,
                "pnl_pct": ev.pnl_pct,
                "exit_type": ev.exit_type,
                "held_minutes": ev.held_minutes,
                # 익절/손절 시 기술 지표 맥락 포함 → MetaEvaluator 패턴 학습용
                "evaluation": (ev.evaluation or "")[:200],
                "lesson": ev.lesson or "",
            }
            for ev in recent_evals
        ]

        # ── 코인별 손익 집계 (최근 7일) — directive에 회피 코인 강제 주입용 ──
        coin_pnl_summary: dict[str, dict] = {}
        try:
            import json as _json
            for ev in self._repo.get_recent_evaluations(limit=50):
                summary_str = ev.coins_summary or ""
                try:
                    coins = _json.loads(summary_str) if summary_str else []
                except Exception:
                    coins = []
                if not isinstance(coins, list):
                    continue
                # 포트폴리오 단위 PnL을 코인 수로 균등 분배 (근사)
                n_coins = max(1, len(coins))
                pnl_pct = ev.pnl_pct or 0.0
                # KRW 손익은 closing_total_assets - total_buy
                pnl_krw_total = (ev.closing_total_assets_krw or 0) - (ev.total_buy_krw or 0)
                per_coin_krw = pnl_krw_total / n_coins
                for coin in coins:
                    sym = coin.get("symbol") if isinstance(coin, dict) else None
                    if not sym:
                        continue
                    s = coin_pnl_summary.setdefault(
                        sym, {"count": 0, "total_pnl_krw": 0.0, "pnl_pct_sum": 0.0}
                    )
                    s["count"] += 1
                    s["total_pnl_krw"] += per_coin_krw
                    s["pnl_pct_sum"] += pnl_pct
            # 평균값 계산
            for sym, d in coin_pnl_summary.items():
                d["avg_pnl_pct"] = round(d["pnl_pct_sum"] / max(1, d["count"]), 2)
                d["total_pnl_krw"] = round(d["total_pnl_krw"], 0)
        except Exception as e:
            logger.warning(f"[메타 평가] 코인별 손익 집계 실패: {e}")
            coin_pnl_summary = {}

        result = self._meta.execute({
            "decision_logs": decision_logs,
            "trade_results": trade_results,
            "current_scores": current_scores,
            "coin_pnl_summary": coin_pnl_summary,
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
                # 피드백 주입 검증 로그 (v4.4) — directive 일부 출력하여 실제 주입 확인
                logger.info(
                    f"[총괄 평가] {fb.agent_role}: "
                    f"{previous_score:.0f} → {fb.score:.0f}점 "
                    f"({fb.priority}) directive='{(fb.directive or '')[:60]}'"
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

        # 반성문 저장 및 Agent에 요약 주입
        for fb in feedbacks:
            if not fb.reflection:
                continue
            try:
                self._repo.save_agent_reflection(
                    agent_role=fb.agent_role,
                    eval_period=eval_period,
                    score=fb.score,
                    reflection=fb.reflection,
                    prompt_summary=fb.prompt_summary,
                )
                # 최신 반성문 요약을 Agent 프롬프트에 반영
                agent = self._agents.get(fb.agent_role)
                if agent:
                    new_summary = self._repo.get_agent_reflection_prompt_summary(
                        fb.agent_role, limit=3
                    )
                    agent.update_reflection_summary(new_summary)
                    logger.info(
                        f"[반성문 저장] {fb.agent_role}: {fb.prompt_summary[:40]}"
                    )
            except Exception as e:
                logger.error(f"[반성문 저장 오류] {fb.agent_role}: {e}")

        return feedbacks
