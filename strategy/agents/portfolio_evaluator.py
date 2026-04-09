"""포트폴리오 평가 전문가

완료된 매매의 성과를 객관적으로 분석하고,
다음 매매를 위한 구체적인 TP/SL 파라미터를 제안합니다.
기존 TradingAgent.evaluate_trade() 프롬프트를 계승합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent
from ..ai_agent import TradeEvaluation

logger = logging.getLogger(__name__)


class PortfolioEvaluator(BaseSpecialistAgent):
    """매매 후 성과 평가 + 다음 TP/SL 제안 전문가 Agent"""

    ROLE_NAME = "portfolio_evaluator"
    DISPLAY_NAME = "포트폴리오 평가가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 보유 자산의 포트폴리오 평가 전문가입니다.\n"
            "완료된 매매의 성과를 객관적으로 분석하고, "
            "다음 매매를 위한 구체적인 파라미터를 제안합니다.\n"
            "2단계 손절 전략의 효과를 평가하고, "
            "익절/손절 기준의 적정성을 판단합니다."
        )

    def execute(self, context: dict) -> dict:
        """매매 결과를 평가하고 다음 전략 파라미터를 제안

        Args:
            context: {
                "symbol": str,
                "buy_price": float,
                "sell_price": float,
                "pnl_pct": float,
                "held_minutes": float,
                "exit_type": str,        # "take_profit" | "stop_loss" | "timeout"
                "original_tp": float,
                "original_sl": float,
                "agent_reason": str,
                "original_sl_1st": float | None,
                "partial_sl_executed": bool,
                "eval_stats": dict | None,
            }

        Returns:
            {"evaluation": TradeEvaluation}
        """
        symbol = context.get("symbol", "UNKNOWN")
        buy_price = context.get("buy_price", 0)
        sell_price = context.get("sell_price", 0)
        pnl_pct = context.get("pnl_pct", 0)
        held_minutes = context.get("held_minutes", 0)
        exit_type = context.get("exit_type", "unknown")
        original_tp = context.get("original_tp", 5.0)
        original_sl = context.get("original_sl", -2.0)
        agent_reason = context.get("agent_reason", "")
        original_sl_1st = context.get("original_sl_1st")
        partial_sl_executed = context.get("partial_sl_executed", False)
        eval_stats = context.get("eval_stats")

        try:
            # 누적 성과 요약
            history_summary = ""
            if eval_stats and eval_stats.get("count", 0) > 0:
                history_summary = (
                    f"\n최근 {eval_stats['count']}건 누적 성과:\n"
                    f"- 승률: {eval_stats['win_rate']:.0%}, "
                    f"평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%\n"
                    f"- 평균 보유시간: {eval_stats['avg_hold_minutes']:.0f}분\n"
                    f"- 평균 익절 설정: +{eval_stats['avg_tp_set']:.1f}%, "
                    f"평균 2차 손절 설정: {eval_stats['avg_sl_set']:.1f}%"
                )

            sl1_info = f"{original_sl_1st}%" if original_sl_1st else "미설정"
            partial_info = (
                "예 (1차 손절 50% 실행됨)"
                if partial_sl_executed
                else "아니오"
            )

            # 종료 유형 한국어 변환
            exit_type_kr = {
                "take_profit": "익절 달성",
                "stop_loss": "손절 발동",
            }.get(exit_type, "시간초과")

            task_prompt = f"""아래 매매 결과를 분석하고, 다음 매매를 위한 2단계 손절 파라미터를 제안하세요.

**2단계 손절 전략:**
- 1차 손절(sl_1st_pct): 도달 시 보유량 50% 매도 → 나머지 반등 대기
- 2차 손절(sl_2nd_pct): 도달 시 나머지 전량 매도 (1차보다 더 낮은 음수)
- 실효 최대 손실 = sl_1st × 50% + sl_2nd × 50%
- 익절(take_profit_pct): 4.0%+ 진입 후 트레일링으로 10%+ 목표 — 손실 타이트, 수익 극대화

**이번 매매 결과:**
- 코인: {symbol}
- 매수가: {buy_price:,.0f}원 → 매도가: {sell_price:,.0f}원
- 실현 수익률: {pnl_pct:+.2f}%
- 보유 시간: {held_minutes:.0f}분 ({held_minutes / 60:.1f}시간)
- 종료 유형: {exit_type_kr}
- 1차 손절 실행 여부: {partial_info}
- 원래 익절 기준: +{original_tp}%
- 원래 1차 손절: {sl1_info}, 2차 손절: {original_sl}%
- AI 선정 이유: {agent_reason}
{history_summary}

**평가 관점:**
1. 익절이 너무 높아서 도달하지 못했는가? → 낮추기 (단, 최소 4.0% 유지)
2. 1차 손절이 너무 좁아서 바로 50% 팔렸는가? → 소폭 넓히기 (최대 -1.5% 유지, 타이트 원칙)
3. 2차 손절이 너무 좁아서 반등 없이 전부 팔렸는가? → 소폭 넓히기 (최대 -2.5% 유지)
4. 보유 시간이 길었다면 익절 진입점을 낮춰 빠른 트레일링 진입 도모

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "evaluation": "이번 매매에 대한 평가 (한국어, 150자 이내)",
  "suggested_tp_pct": 다음매매추천익절퍼센트(숫자, 4.0~12.0 범위),
  "suggested_sl_1st_pct": 다음매매추천1차손절퍼센트(음수, -0.5~-1.5 범위),
  "suggested_sl_pct": 다음매매추천2차손절퍼센트(음수, -0.8~-2.5 범위, 1차보다 더낮은음수),
  "lesson": "핵심 교훈 한 줄 (한국어, 50자 이내)"
}}"""

            logger.info(
                f"[PortfolioEvaluator] 매매 평가 중... [{symbol} {pnl_pct:+.2f}%]"
            )
            raw = self._call_llm(task_prompt, max_tokens=512)
            logger.info(f"[PortfolioEvaluator] 평가 응답: {raw}")

            data = self._parse_json(raw)

            suggested_tp = float(data.get("suggested_tp_pct", original_tp))
            suggested_sl_1st = float(data.get(
                "suggested_sl_1st_pct",
                original_sl_1st if original_sl_1st else -2.0,
            ))
            suggested_sl = float(data.get("suggested_sl_pct", original_sl))

            # ── 안전장치: TP 4~12%, SL1 -1.5~-0.5%, SL2 -2.5~-0.8% ──
            suggested_tp = max(4.0, min(12.0, suggested_tp))
            if suggested_sl_1st > 0:
                suggested_sl_1st = -abs(suggested_sl_1st)
            if suggested_sl > 0:
                suggested_sl = -abs(suggested_sl)
            suggested_sl_1st = max(-1.5, min(-0.5, suggested_sl_1st))
            suggested_sl = max(-2.5, min(-0.8, suggested_sl))
            # SL2는 SL1보다 0.2% 이상 낮아야 함
            if suggested_sl >= suggested_sl_1st - 0.2:
                suggested_sl = suggested_sl_1st - 0.3

            evaluation = TradeEvaluation(
                evaluation=data.get("evaluation", "평가 없음"),
                suggested_tp_pct=round(suggested_tp, 2),
                suggested_sl_1st_pct=round(suggested_sl_1st, 2),
                suggested_sl_pct=round(suggested_sl, 2),
                lesson=data.get("lesson", ""),
            )

            logger.info(
                f"[PortfolioEvaluator] 제안: TP=+{evaluation.suggested_tp_pct}% / "
                f"SL1={evaluation.suggested_sl_1st_pct}% / "
                f"SL2={evaluation.suggested_sl_pct}% / "
                f"교훈: {evaluation.lesson}"
            )
            return {"evaluation": evaluation}

        except Exception as e:
            logger.error(f"[PortfolioEvaluator] 평가 실패: {e}")
            return {
                "evaluation": TradeEvaluation(
                    evaluation=f"파싱 실패 — 기존 전략 유지 ({e})",
                    suggested_tp_pct=round(original_tp, 2),
                    suggested_sl_1st_pct=round(
                        original_sl_1st if original_sl_1st else -2.0, 2
                    ),
                    suggested_sl_pct=round(original_sl, 2),
                    lesson="",
                )
            }
