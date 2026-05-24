"""총괄 전문가 평가가

3시간마다 5개 전문가를 종합 평가하고 피드백+점수를 부여합니다.
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
    "investment_strategist",
    "buy_strategist",
    "sell_strategist",
    "portfolio_evaluator",
    "coin_profile_analyst",
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
    reflection: str = ""       # 1인칭 반성문 (해당 전문가 관점)
    prompt_summary: str = ""   # 프롬프트 주입용 핵심 요약 (30자 이내)


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
            "  - ★ 손절률 자동 감점 (50%→-10, 70%→-20, 100%→-30)\n"
            "  - 시장 심리 판단이 실제 결과와 일치했는가?\n"
            "  - bearish 판단 후 익절이면 → 과도한 보수성 / bullish 후 손절이면 → 위험 감지 실패\n\n"
            "■ asset_manager (자산 운용가)\n"
            "  - ★★ 책임: 총 자산이 감소했으면 이것은 실패다\n"
            "  - ★ 순손실 발생 시 자동 감점 (손실률 1%→-10, 2%→-20, 3%이상→-30)\n"
            "  - 손절이 반복되는데 투자 비율을 줄이지 않았으면 → 리스크 관리 실패, 강한 감점\n"
            "  - 투자 보류가 잦으면 → 기회비용 발생, 감점\n"
            "  - 비율만 조정하고 실제 손실을 막지 못했다면 → 역할을 다하지 못한 것\n\n"
            "■ investment_strategist (투자 전문가)\n"
            "  - 기회 포착 능력: 진입 시점이 수익으로 연결됐는가?\n"
            "  - 투자 강행 판단이 손절로 끝났으면 → 과도한 공격성, 감점\n"
            "  - 투자 보류 판단이 옳았는가? (보류 후 시장이 하락했으면 +, 상승했으면 -)\n"
            "  - opportunity_score가 높았는데 손절이면 → 기회 판단 오류, 강한 감점\n"
            "  - 자산 운용가와 의견 충돌 시 결과가 어땠는가?\n\n"
            "■ buy_strategist (매수 전문가)\n"
            "  - ★ 손절률 자동 감점 (50%→-10, 70%→-15, 100%→-20)\n"
            "  - RSI 과매수(>70) 코인 포함 → 즉시 감점\n"
            "  - MACD 하락/데드크로스 코인 비중이 높았는가?\n"
            "  - 8개 코인 분산 효과와 TP/SL 설정의 현실성\n\n"
            "■ sell_strategist (매도 전문가)\n"
            "  - TP/SL 조정 타이밍과 기회비용\n"
            "  - 불필요한 조정으로 혼란을 야기했는가?\n\n"
            "■ portfolio_evaluator (포트폴리오 평가가)\n"
            "  - ★★★ 가장 중요: 손절 발생 시 이 전문가가 가장 큰 책임을 진다\n"
            "  - ★ 손절 자동 감점 (50%→-15, 70%→-25, 100%→-35) — 가장 강한 패널티\n"
            "  - ★ 평균 손실 규모 자동 감점 (평균PnL -1%→-5, -2%→-15, -3%이상→-25)\n"
            "  - 제안한 TP가 너무 높아 달성 못하고 손절됐으면 → 현실성 없는 TP 설정\n"
            "  - 제안한 SL이 너무 빡빡해 정상 변동성에 손절됐으면 → SL 조정 실패\n"
            "  - 교훈(lesson)이 다음 사이클에서 실제로 반영됐는가?\n"
            "  - 익절이 나오면 +20점 보너스 (좋은 TP/SL 설정의 직접 증거)\n\n"
            "【점수 부여 기준】\n"
            "- 80+: 탁월함. 판단이 매매 성과에 직접 기여\n"
            "- 60~79: 양호함. 개선 여지 있으나 큰 문제 없음\n"
            "- 40~59: 미흡함. 명확한 개선 필요\n"
            "- 40 미만: 심각함. 즉시 전략 전환 필요\n"
            "★ LLM 기본 점수가 70점 근처에 몰리는 현상을 경계하라.\n"
            "  실제 성과 데이터를 보고 냉정하게 평가해야 한다. 손절이 많으면 40점대가 정상이다."
        )

    def execute(self, context: dict) -> dict:
        """5개 전문가를 종합 평가하고 피드백을 반환

        Args:
            context: {
                "decision_logs": list,    # 최근 3시간 의사결정 기록
                "trade_results": list,    # 최근 매매 결과
                "current_scores": dict,   # 각 agent 현재 점수 {role: score}
                "coin_pnl_summary": dict, # {symbol: {count, total_pnl_krw, avg_pnl_pct}} (옵션)
            }

        Returns:
            {"feedbacks": list[AgentFeedback]}
        """
        decision_logs = context.get("decision_logs", [])
        trade_results = context.get("trade_results", [])
        current_scores = context.get("current_scores", {})
        coin_pnl_summary = context.get("coin_pnl_summary", {})

        try:
            # 의사결정 기록 텍스트
            decision_logs_text = self._format_decision_logs(decision_logs)
            trade_results_text = self._format_trade_results(trade_results)
            current_scores_text = self._format_current_scores(current_scores)
            coin_pnl_text = self._format_coin_pnl_summary(coin_pnl_summary)

            # 손실 빈발 코인 추출 (directive에 명시적 회피 지시 강제)
            loss_coins_text = ""
            if coin_pnl_summary:
                losers = [
                    (sym, data) for sym, data in coin_pnl_summary.items()
                    if data.get("total_pnl_krw", 0) < -3000
                ]
                losers.sort(key=lambda x: x[1].get("total_pnl_krw", 0))
                if losers:
                    top_losers = losers[:5]
                    loss_coins_text = (
                        "\n【★ 손실 빈발 코인 (반드시 directive에 회피 지시 포함) ★】\n"
                        + "\n".join(
                            f"  • {sym}: {d['count']}회, "
                            f"누적 {d.get('total_pnl_krw', 0):.0f}원, "
                            f"평균 {d.get('avg_pnl_pct', 0):+.2f}%"
                            for sym, d in top_losers
                        )
                    )

            task_prompt = f"""최근 3시간 전문가별 의사결정 기록:
{decision_logs_text}

최근 포트폴리오 매매 결과:
{trade_results_text}

【최근 코인별 손익 분포】
{coin_pnl_text}{loss_coins_text}

현재 점수: {current_scores_text}

【평가 요청】
5명의 전문가를 각각 평가하세요.
★ directive 작성 강제 룰 (위반 시 평가가 본인이 감점):
  - 명령형으로 작성 ("~하세요", "~금지")
  - 반드시 구체적 수치 포함 (예: "TP를 3% 이하로", "BTC 포함 필수")
  - 데이터 근거 포함 (예: "최근 3건 손절이므로", "{loss_coins_text and '손실 빈발 코인 명시 회피'}")
  - "현재 방향 유지" / "데이터 수집" / "더 잘하세요" 같은 추상 표현 금지
  - 손실 빈발 코인이 있으면 buy_strategist directive에 회피 코인 명시 강제
    (예: "MERL/KAT 매수 금지 — 5회 누적 -50000원 손실")

각 전문가의 역할과 평가 관점:
- market_analyst: 시장 심리 판단 정확도 → 결과와 일치했나?
- asset_manager: 투자 비율 적절성 → 리스크 조절이 성과에 기여했나?
- buy_strategist: 8개 코인 선정 품질 → 분산 효과, 하락 코인 포함 여부
- sell_strategist: TP/SL 조정 효과 → 조정이 수익 개선에 기여했나?
- portfolio_evaluator: 파라미터 제안 정확도 → 제안대로 했을 때 성과 개선되었나?

priority: reinforce(유지·강화) | improve(개선필요) | critical(즉시개선)

strengths/weaknesses는 각 50자 이내, directive는 80자 이내로 작성.
reflection: 해당 전문가 1인칭으로 작성 ("저는 이번에 ~을 잘못했습니다. ~을 개선하겠습니다.") 150자 이내.
prompt_summary: 반성문의 핵심을 한 줄 압축 (30자 이내, 프롬프트에 직접 주입됨).

JSON으로만 응답 (마크다운 코드블록 없이):
{{"agents": [
  {{"role": "market_analyst", "score": 75, "strengths": "구체적 잘한 점", "weaknesses": "구체적 부족한 점", "directive": "다음 분석 시 구체적 명령 (수치 포함)", "priority": "reinforce", "reflection": "저는 이번에... 개선하겠습니다.", "prompt_summary": "핵심 반성 한 줄"}},
  {{"role": "asset_manager", "score": 70, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "improve", "reflection": "...", "prompt_summary": "..."}},
  {{"role": "buy_strategist", "score": 65, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "improve", "reflection": "...", "prompt_summary": "..."}},
  {{"role": "sell_strategist", "score": 60, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "critical", "reflection": "...", "prompt_summary": "..."}},
  {{"role": "portfolio_evaluator", "score": 35, "strengths": "...", "weaknesses": "손절 2건 발생으로 파라미터 제안 실패", "directive": "손절 발생 직후 SL을 즉시 좁히고 TP를 낮춰 안전한 청산 유도", "priority": "critical", "reflection": "저는 이번에 손절 2건을 막지 못했습니다. SL 범위를 더 좁게 조정하겠습니다.", "prompt_summary": "손절 2건 — SL 즉시 축소 필수"}},
  {{"role": "coin_profile_analyst", "score": 65, "strengths": "...", "weaknesses": "...", "directive": "...", "priority": "improve", "reflection": "...", "prompt_summary": "..."}}
]}}"""

            logger.info("[MetaEvaluator] 전문가 종합 평가 시작...")
            raw = self._call_llm(task_prompt, max_tokens=1600)
            logger.info(f"[MetaEvaluator] 평가 응답: {raw}")

            data = self._parse_json(raw)
            agents_data = data.get("agents", [])

            # ── 손절률 기반 명시적 감점 계산 ──
            stop_loss_penalty = self._calc_stop_loss_penalty(trade_results)

            feedbacks: list[AgentFeedback] = []
            for agent_data in agents_data:
                role = agent_data.get("role", "")
                if role not in AGENT_ROLES:
                    logger.warning(f"[MetaEvaluator] 알 수 없는 역할: {role} — 무시")
                    continue

                # 점수 0~100 clamp
                score = max(0.0, min(100.0, float(agent_data.get("score", 50))))

                # ★ 손절 패널티 적용 (LLM 판단 + 명시적 감점 합산)
                if role in stop_loss_penalty:
                    penalty = stop_loss_penalty[role]
                    original = score
                    score = max(0.0, score - penalty)
                    logger.info(
                        f"[MetaEvaluator] {role}: 손절 패널티 "
                        f"-{penalty:.0f}점 적용 ({original:.0f} → {score:.0f})"
                    )

                priority = agent_data.get("priority", "improve")
                if priority not in ("reinforce", "improve", "critical"):
                    priority = "improve"

                # 손절 패널티가 크면 priority 자동 격상
                if role in stop_loss_penalty and stop_loss_penalty[role] >= 10:
                    priority = "critical"

                feedback = AgentFeedback(
                    agent_role=role,
                    score=round(score, 1),
                    strengths=agent_data.get("strengths", ""),
                    weaknesses=agent_data.get("weaknesses", ""),
                    directive=agent_data.get("directive", ""),
                    priority=priority,
                    reflection=agent_data.get("reflection", ""),
                    prompt_summary=agent_data.get("prompt_summary", ""),
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
        """매매 결과를 텍스트로 변환 (익절 시 기술 지표 맥락 포함)"""
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
                # 익절 시 기술 지표 맥락 → 시장 분석가 directive에 반영할 패턴 학습
                evaluation = (r.get("evaluation") or "").strip()
                if exit_type == "take_profit" and evaluation:
                    lines.append(f"   ✅ 익절 당시 분석: {evaluation[:150]}")
                # 교훈 (익절/손절 모두 포함)
                lesson = (r.get("lesson") or "").strip()
                if lesson:
                    lines.append(f"   💡 교훈: {lesson[:100]}")
            else:
                lines.append(f"{i}. {r}")
        return "\n".join(lines)

    @staticmethod
    def _format_coin_pnl_summary(summary: dict) -> str:
        """코인별 손익 분포 요약 (directive에 회피 코인 강제 주입용)"""
        if not summary:
            return "데이터 없음"
        items = sorted(
            summary.items(),
            key=lambda x: x[1].get("total_pnl_krw", 0),
        )
        lines = []
        for sym, d in items[:15]:
            count = d.get("count", 0)
            tot = d.get("total_pnl_krw", 0)
            avg = d.get("avg_pnl_pct", 0)
            tag = "🔴" if tot < -3000 else ("🟢" if tot > 3000 else "⚪")
            lines.append(f"  {tag} {sym}: {count}회, 누적 {tot:+,.0f}원, 평균 {avg:+.2f}%")
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
    def _calc_stop_loss_penalty(trade_results: list) -> dict[str, float]:
        """손절률·평균 수익률 기반 명시적 감점 산출

        손절이 반복되거나 전체 손실이 누적되면 LLM 판단과 무관하게 자동 감점.
        - market_analyst / buy_strategist: 손절률 기반
        - portfolio_evaluator: 손절률 기반 (가장 강하게)
        - asset_manager: 평균 PnL 기반 (총 자산 감소 책임)

        Returns:
            {role: penalty_points} — 해당 role에 차감할 점수
        """
        if not trade_results:
            return {}

        total = len(trade_results)
        if total == 0:
            return {}

        stop_losses = sum(
            1 for r in trade_results
            if isinstance(r, dict) and r.get("exit_type") == "stop_loss"
        )
        sl_rate = stop_losses / total

        # 평균 PnL 계산 (asset_manager 감점용)
        pnl_values = [
            float(r["pnl_pct"])
            for r in trade_results
            if isinstance(r, dict) and r.get("pnl_pct") is not None
        ]
        avg_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0.0

        penalty: dict[str, float] = {}

        # ── 손절률 기반 감점 ─────────────────────────
        # 손절률 50% 이상: 시장 분석가 -10, 매수 전문가 -10, 포트폴리오 평가가 -15
        # 손절률 70% 이상: 시장 분석가 -20, 매수 전문가 -15, 포트폴리오 평가가 -25
        # 손절률 100%:     시장 분석가 -30, 매수 전문가 -20, 포트폴리오 평가가 -35
        if sl_rate >= 1.0:
            penalty["market_analyst"] = 30
            penalty["buy_strategist"] = 20
            penalty["portfolio_evaluator"] = 35
        elif sl_rate >= 0.7:
            penalty["market_analyst"] = 20
            penalty["buy_strategist"] = 15
            penalty["portfolio_evaluator"] = 25
        elif sl_rate >= 0.5:
            penalty["market_analyst"] = 10
            penalty["buy_strategist"] = 10
            penalty["portfolio_evaluator"] = 15

        # ── 평균 PnL 기반 asset_manager 감점 ────────
        # 평균 수익률 -1% 미만: -10
        # 평균 수익률 -2% 미만: -20
        # 평균 수익률 -3% 미만: -30
        if avg_pnl < -3.0:
            penalty["asset_manager"] = 30
        elif avg_pnl < -2.0:
            penalty["asset_manager"] = 20
        elif avg_pnl < -1.0:
            penalty["asset_manager"] = 10

        if penalty:
            logger.info(
                f"[MetaEvaluator] 손절률 {sl_rate:.0%} ({stop_losses}/{total}건) "
                f"| 평균 PnL {avg_pnl:.2f}% → 감점: {penalty}"
            )

        return penalty

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
