"""총괄 전문가 평가가

6시간마다 5개 전문가를 종합 평가하고 피드백+점수를 부여합니다.
각 전문가의 다음 판단에 직접 삽입될 구체적인 개선 지시(directive)를 작성합니다.
"""
import logging
from dataclasses import dataclass

from .base_agent import BaseSpecialistAgent

logger = logging.getLogger(__name__)

# 평가 대상 Agent 역할 목록
AGENT_ROLES = [
    "market_analyst",
    "asset_manager",
    "buy_strategist",
    "sell_strategist",
    "portfolio_evaluator",
]


@dataclass
class AgentFeedback:
    """개별 전문가에 대한 평가 결과"""
    agent_role: str
    score: float          # 0~100
    strengths: str        # 잘하는 부분
    weaknesses: str       # 못하는 부분
    directive: str        # 구체적 개선 지시 (프롬프트에 삽입될 내용)
    priority: str         # reinforce | improve | critical


class MetaEvaluator(BaseSpecialistAgent):
    """5개 전문가를 종합 평가하는 총괄 평가 Agent"""

    ROLE_NAME = "meta_evaluator"
    DISPLAY_NAME = "총괄 평가가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 AI 자동매매 시스템의 전문가 총괄 평가위원입니다.\n"
            "5명의 전문가의 최근 판단과 매매 결과를 심층 분석하여 구체적 피드백을 부여합니다.\n\n"
            "【★ directive 작성 원칙 — 가장 중요 ★】\n"
            "directive는 해당 전문가의 다음 LLM 호출에 직접 삽입됩니다.\n"
            "따라서 directive는 반드시:\n"
            "1. '~하세요', '~금지', '~우선' 등 명령형으로 작성\n"
            "2. 구체적 수치를 포함 (예: 'TP를 4% 이하로 설정하세요')\n"
            "3. 실제 데이터 기반 근거 포함 (예: '최근 3건 연속 손절이므로')\n"
            "4. 추상적 표현 금지 ('더 잘하세요'는 무의미)\n\n"
            "【전문가별 평가 기준】\n"
            "■ market_analyst (시장 분석가)\n"
            "  - 시장 심리 판단이 실제 결과와 일치했는가?\n"
            "  - bearish 판단 후 포트폴리오가 익절이면 → 과도한 보수성\n"
            "  - bullish 판단 후 포트폴리오가 손절이면 → 위험 감지 실패\n\n"
            "■ asset_manager (자산 운용가)\n"
            "  - 투자 비율이 적절했는가? (손절 후 비율 축소, 익절 후 비율 유지/확대)\n"
            "  - 투자 보류 판단이 합당했는가?\n\n"
            "■ buy_strategist (매수 전문가)\n"
            "  - 선정된 8개 코인의 분산 효과는?\n"
            "  - 하락 코인이 다수 포함되었는가?\n"
            "  - TP/SL 설정이 현실적이었는가?\n\n"
            "■ sell_strategist (매도 전문가)\n"
            "  - TP/SL 조정 타이밍이 적절했는가?\n"
            "  - 기회비용이 발생했는가? (보유 시간 대비 수익률)\n"
            "  - 불필요한 조정으로 혼란을 야기했는가?\n\n"
            "■ portfolio_evaluator (포트폴리오 평가가)\n"
            "  - 제안한 TP/SL이 다음 사이클에서 효과적이었는가?\n"
            "  - 교훈(lesson)이 실제로 유용했는가?\n\n"
            "【점수 부여 기준】\n"
            "- 80+: 탁월함. 판단이 매매 성과에 직접 기여\n"
            "- 60~79: 양호함. 개선 여지 있으나 큰 문제 없음\n"
            "- 40~59: 미흡함. 명확한 개선 필요\n"
            "- 40 미만: 심각함. 즉시 전략 전환 필요"
        )

    def execute(self, context: dict) -> dict:
        """5개 전문가를 종합 평가하고 피드백을 반환

        Args:
            context: {
                "decision_logs": list,    # 최근 6시간 의사결정 기록
                "trade_results": list,    # 최근 매매 결과
                "current_scores": dict,   # 각 agent 현재 점수 {role: score}
            }

        Returns:
            {"feedbacks": list[AgentFeedback]}
        """
        decision_logs = context.get("decision_logs", [])
        trade_results = context.get("trade_results", [])
        current_scores = context.get("current_scores", {})

        try:
            # 의사결정 기록 텍스트
            decision_logs_text = self._format_decision_logs(decision_logs)
            trade_results_text = self._format_trade_results(trade_results)
            current_scores_text = self._format_current_scores(current_scores)

            task_prompt = f"""최근 6시간 전문가별 의사결정 기록:
{decision_logs_text}

최근 포트폴리오 매매 결과:
{trade_results_text}

현재 점수: {current_scores_text}

【평가 요청】
5명의 전문가를 각각 평가하세요.
★ directive가 핵심입니다. 해당 전문가의 다음 LLM 호출에 직접 주입되므로:
  - 명령형으로 작성 ("~하세요", "~금지")
  - 구체적 수치 포함 ("TP를 3% 이하로", "BTC 포함 필수")
  - 데이터 근거 포함 ("최근 3건 손절이므로")

각 전문가의 역할과 평가 관점:
- market_analyst: 시장 심리 판단 정확도 → 결과와 일치했나?
- asset_manager: 투자 비율 적절성 → 리스크 조절이 성과에 기여했나?
- buy_strategist: 8개 코인 선정 품질 → 분산 효과, 하락 코인 포함 여부
- sell_strategist: TP/SL 조정 효과 → 조정이 수익 개선에 기여했나?
- portfolio_evaluator: 파라미터 제안 정확도 → 제안대로 했을 때 성과 개선되었나?

priority: reinforce(유지·강화) | improve(개선필요) | critical(즉시개선)

strengths/weaknesses는 각 50자 이내, directive는 80자 이내로 작성.

JSON으로만 응답 (마크다운 코드블록 없이):
{{"agents": [
  {{"role": "market_analyst", "score": 75, "strengths": "구체적 잘한 점", "weaknesses": "구체적 부족한 점", "directive": "다음 분석 시 구체적 명령 (수치 포함)", "priority": "reinforce"}},
  {{"role": "asset_manager", "score": 70, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "improve"}},
  {{"role": "buy_strategist", "score": 65, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "improve"}},
  {{"role": "sell_strategist", "score": 60, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "critical"}},
  {{"role": "portfolio_evaluator", "score": 70, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "reinforce"}}
]}}"""

            logger.info("[MetaEvaluator] 전문가 종합 평가 시작...")
            raw = self._call_llm(task_prompt, max_tokens=1024)
            logger.info(f"[MetaEvaluator] 평가 응답: {raw}")

            data = self._parse_json(raw)
            agents_data = data.get("agents", [])

            feedbacks: list[AgentFeedback] = []
            for agent_data in agents_data:
                role = agent_data.get("role", "")
                if role not in AGENT_ROLES:
                    logger.warning(f"[MetaEvaluator] 알 수 없는 역할: {role} — 무시")
                    continue

                # 점수 0~100 clamp
                score = max(0.0, min(100.0, float(agent_data.get("score", 50))))

                priority = agent_data.get("priority", "improve")
                if priority not in ("reinforce", "improve", "critical"):
                    priority = "improve"

                feedback = AgentFeedback(
                    agent_role=role,
                    score=round(score, 1),
                    strengths=agent_data.get("strengths", ""),
                    weaknesses=agent_data.get("weaknesses", ""),
                    directive=agent_data.get("directive", ""),
                    priority=priority,
                )
                feedbacks.append(feedback)

                logger.info(
                    f"[MetaEvaluator] {role}: "
                    f"점수={feedback.score} / 우선순위={feedback.priority} / "
                    f"지시={feedback.directive[:50]}..."
                )

            # 누락된 역할이 있으면 기본 피드백 추가
            evaluated_roles = {f.agent_role for f in feedbacks}
            for role in AGENT_ROLES:
                if role not in evaluated_roles:
                    logger.warning(
                        f"[MetaEvaluator] {role} 평가 누락 — 기본 피드백 생성"
                    )
                    feedbacks.append(AgentFeedback(
                        agent_role=role,
                        score=current_scores.get(role, 50.0),
                        strengths="평가 데이터 부족",
                        weaknesses="평가 데이터 부족",
                        directive="현재 방향을 유지하면서 더 많은 데이터를 수집하세요.",
                        priority="improve",
                    ))

            return {"feedbacks": feedbacks}

        except Exception as e:
            logger.error(f"[MetaEvaluator] 평가 실패: {e}")
            # 에러 시: 기존 점수 유지, 빈 피드백 반환
            return {"feedbacks": self._default_feedbacks(current_scores)}

    @staticmethod
    def _format_decision_logs(logs: list) -> str:
        """의사결정 기록을 텍스트로 변환"""
        if not logs:
            return "기록 없음"

        lines = []
        for i, log in enumerate(logs, 1):
            if isinstance(log, dict):
                role = log.get("role", "unknown")
                decision = log.get("decision", "")
                timestamp = log.get("timestamp", "")
                lines.append(f"{i}. [{timestamp}] {role}: {decision}")
            else:
                lines.append(f"{i}. {log}")
        return "\n".join(lines)

    @staticmethod
    def _format_trade_results(results: list) -> str:
        """매매 결과를 텍스트로 변환"""
        if not results:
            return "최근 매매 없음"

        lines = []
        for i, r in enumerate(results, 1):
            if isinstance(r, dict):
                label = r.get("portfolio_name") or r.get("symbol", "?")
                pnl = r.get("pnl_pct", 0)
                exit_type = r.get("exit_type", "?")
                held = r.get("held_minutes", 0)
                exit_kr = {"take_profit": "익절", "stop_loss": "손절"}.get(
                    exit_type, "시간초과"
                )
                lines.append(
                    f"{i}. {label}: {exit_kr} {pnl:+.2f}% (보유 {held:.0f}분)"
                )
            else:
                lines.append(f"{i}. {r}")
        return "\n".join(lines)

    @staticmethod
    def _format_current_scores(scores: dict) -> str:
        """현재 점수를 텍스트로 변환"""
        if not scores:
            return "초기 상태 (모두 50점)"

        role_names = {
            "market_analyst": "시장 분석가",
            "asset_manager": "자산 운용가",
            "buy_strategist": "매수 전문가",
            "sell_strategist": "매도 전문가",
            "portfolio_evaluator": "포트폴리오 평가가",
        }
        lines = []
        for role, score in scores.items():
            name = role_names.get(role, role)
            lines.append(f"- {name}({role}): {score:.1f}점")
        return "\n".join(lines)

    @staticmethod
    def _default_feedbacks(current_scores: dict) -> list[AgentFeedback]:
        """에러 시 기본 피드백 목록 (기존 점수 유지)"""
        feedbacks = []
        for role in AGENT_ROLES:
            feedbacks.append(AgentFeedback(
                agent_role=role,
                score=current_scores.get(role, 50.0),
                strengths="",
                weaknesses="",
                directive="",
                priority="improve",
            ))
        return feedbacks
