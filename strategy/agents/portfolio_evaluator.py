"""포트폴리오 평가 전문가 (v4.0 — 포트폴리오 기반)

완료된 포트폴리오 매매의 종합 성과를 분석하고,
다음 포트폴리오를 위한 TP/SL 파라미터를 제안합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent
from ..ai_agent import TradeEvaluation

logger = logging.getLogger(__name__)


class PortfolioEvaluator(BaseSpecialistAgent):
    """포트폴리오 매매 후 성과 평가 + 다음 TP/SL 제안 전문가 Agent"""

    ROLE_NAME = "portfolio_evaluator"
    DISPLAY_NAME = "포트폴리오 평가가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 포트폴리오 매매 성과 평가 및 전략 개선 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "완료된 포트폴리오의 종합 성과를 심층 분석하고,\n"
            "다음 포트폴리오가 더 나은 성과를 내도록 구체적인 TP/SL 파라미터를 제안합니다.\n"
            "당신의 제안이 다음 사이클의 파라미터에 직접 반영됩니다.\n\n"
            "【평가 프레임워크 — 반드시 이 관점으로 분석】\n"
            "1. ★분산 효과 분석★: 8개 코인 중 승자/패자 비율은?\n"
            "   - 8개 중 5개 이상 손실이면 → 구성 자체가 잘못됨 (시장 역행 or 유사 코인 중복)\n"
            "   - 4:4 비율이면 → 분산 효과 발휘, 종합 수익률이 관건\n"
            "   - 6개 이상 수익이면 → 좋은 구성, TP를 높여도 됨\n"
            "2. TP 적정성: 도달 못 했으면 낮추기, 일찍 도달했으면 높이기\n"
            "3. SL 적정성: 반등 없이 연쇄 매도되었으면 시장 판단 오류\n"
            "4. 보유 시간 효율: 2시간 이상 보유하고 소폭 수익이면 TP 낮추기 권고\n\n"
            "【제안 원칙 — 반드시 준수】\n"
            "- suggested_tp: 4.0%~12.0% (수익 극대화를 위해 높은 목표 유지. 최소 4.0% 사수)\n"
            "- suggested_sl: -2.0%~-0.5% (너무 좁으면 빈번한 손절, 너무 넓으면 큰 손실)\n"
            "- lesson은 매수 전문가가 읽을 핵심 한 줄. 구체적이어야 합니다.\n"
            "- 연속 손절 중이라면 → TP를 낮추고 SL은 현재 유지 권고 (빠른 수익 실현)"
        )

    def execute(self, context: dict) -> dict:
        """포트폴리오 매매 결과를 평가하고 다음 전략 파라미터를 제안

        Args:
            context: {
                "portfolio_name": str,
                "total_buy_krw": float,
                "total_sell_krw": float,
                "combined_pnl_pct": float,
                "held_minutes": float,
                "exit_type": str,
                "original_tp": float,
                "original_sl": float,
                "coin_results": list[dict],  # [{symbol, buy_krw, sell_krw, pnl_pct}]
                "portfolio_reason": str,
                "eval_stats": dict | None,
            }

        Returns:
            {"evaluation": TradeEvaluation}
        """
        portfolio_name = context.get("portfolio_name", "UNKNOWN")
        total_buy_krw = context.get("total_buy_krw", 0)
        total_sell_krw = context.get("total_sell_krw", 0)
        combined_pnl_pct = context.get("combined_pnl_pct", 0)
        held_minutes = context.get("held_minutes", 0)
        exit_type = context.get("exit_type", "unknown")
        original_tp = context.get("original_tp", 5.0)
        original_sl = context.get("original_sl", -2.0)
        coin_results = context.get("coin_results", [])
        portfolio_reason = context.get("portfolio_reason", "")
        eval_stats = context.get("eval_stats")

        try:
            # 개별 코인 결과 텍스트
            coin_lines = []
            slippage_lines = []
            total_slippage_krw = 0.0
            winners = 0
            losers = 0
            for cr in coin_results:
                pnl = cr.get("pnl_pct", 0)
                label = "+" if pnl >= 0 else ""
                coin_lines.append(
                    f"  - {cr['symbol']}: {label}{pnl:.2f}% "
                    f"({cr.get('buy_krw', 0):,.0f}원 → {cr.get('sell_krw', 0):,.0f}원)"
                )
                if pnl >= 0:
                    winners += 1
                else:
                    losers += 1

                # 체결 품질 (목표가 vs 실제 체결가)
                tp = cr.get("target_price", 0)
                sp = cr.get("sell_price", 0)
                coin_units = cr.get("units", 0)
                if tp > 0 and sp > 0:
                    slip_pct = (sp - tp) / tp * 100
                    slip_krw = (sp - tp) * coin_units
                    total_slippage_krw += slip_krw
                    slippage_lines.append(
                        f"  - {cr['symbol']}: 목표 {tp:,.0f}원 → "
                        f"체결 {sp:,.0f}원 ({slip_pct:+.2f}%)"
                    )
            coin_text = "\n".join(coin_lines) if coin_lines else "  (정보 없음)"
            slippage_text = "\n".join(slippage_lines) if slippage_lines else "  (데이터 없음)"

            # 누적 성과 요약
            history_summary = ""
            if eval_stats and eval_stats.get("count", 0) > 0:
                history_summary = (
                    f"\n최근 {eval_stats['count']}건 누적 성과:\n"
                    f"- 승률: {eval_stats['win_rate']:.0%}, "
                    f"평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%\n"
                    f"- 평균 보유시간: {eval_stats['avg_hold_minutes']:.0f}분"
                )

            exit_type_kr = {
                "take_profit": "익절 달성",
                "stop_loss": "손절 발동",
            }.get(exit_type, "시간초과")

            task_prompt = f"""아래 포트폴리오 매매 결과를 분석하고, 다음 포트폴리오를 위한 파라미터를 제안하세요.

**포트폴리오 결과:**
- 포트폴리오: {portfolio_name} ({len(coin_results)}개 코인)
- 총 투입: {total_buy_krw:,.0f}원 → 회수: {total_sell_krw:,.0f}원
- 종합 수익률: {combined_pnl_pct:+.2f}%
- 보유 시간: {held_minutes:.0f}분 ({held_minutes / 60:.1f}시간)
- 종료 유형: {exit_type_kr}
- 원래 익절: +{original_tp}%, 원래 손절: {original_sl}%
- 구성 이유: {portfolio_reason}
- 개별 승/패: {winners}승 {losers}패

**개별 코인 결과:**
{coin_text}

**체결 품질 (목표가 vs 실제 체결가):**
{slippage_text}
총 슬리피지: {total_slippage_krw:+,.0f}원
{history_summary}

**평가 관점:**
1. 포트폴리오 분산 효과 — 8개 코인 중 승자/패자 비율이 어떠한가?
2. 익절이 너무 높아서 도달하지 못했는가? → 낮추기 (단, 최소 4.0% 유지)
3. 손절이 너무 좁아서 반등 기회 없이 매도되었는가? → 소폭 넓히기 (최대 -2.0%)
4. 보유 시간이 길었다면 익절을 낮춰 빠른 트레일링 진입 도모
5. ★체결 품질★: 목표가 대비 실제 체결가 편차가 큰 코인은 유동성 부족.
   슬리피지가 -0.3% 이상이면 다음 포트폴리오에서 해당 코인 재선정 불가 권고.

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "evaluation": "이번 포트폴리오에 대한 평가 (한국어, 200자 이내)",
  "suggested_tp_pct": 다음포트폴리오추천익절퍼센트(숫자, 4.0~12.0 범위),
  "suggested_sl_pct": 다음포트폴리오추천손절퍼센트(음수, -2.0~-0.5 범위),
  "lesson": "핵심 교훈 한 줄 (한국어, 50자 이내)"
}}"""

            logger.info(
                f"[PortfolioEvaluator] 포트폴리오 평가 중... "
                f"[{portfolio_name} {combined_pnl_pct:+.2f}%]"
            )
            raw = self._call_llm(task_prompt, max_tokens=512)
            logger.info(f"[PortfolioEvaluator] 평가 응답: {raw}")

            data = self._parse_json(raw)

            suggested_tp = float(data.get("suggested_tp_pct", original_tp))
            suggested_sl = float(data.get("suggested_sl_pct", original_sl))

            # ── 안전장치: TP 4~12%, SL 최대 -2.0% ──
            suggested_tp = max(4.0, min(12.0, suggested_tp))
            if suggested_sl > 0:
                suggested_sl = -abs(suggested_sl)
            suggested_sl = max(-2.0, min(-0.5, suggested_sl))

            evaluation = TradeEvaluation(
                evaluation=data.get("evaluation", "평가 없음"),
                suggested_tp_pct=round(suggested_tp, 2),
                suggested_sl_pct=round(suggested_sl, 2),
                lesson=data.get("lesson", ""),
            )

            logger.info(
                f"[PortfolioEvaluator] 제안: TP=+{evaluation.suggested_tp_pct}% / "
                f"SL={evaluation.suggested_sl_pct}% / 교훈: {evaluation.lesson}"
            )
            return {"evaluation": evaluation}

        except Exception as e:
            logger.error(f"[PortfolioEvaluator] 평가 실패: {e}")
            return {
                "evaluation": TradeEvaluation(
                    evaluation=f"파싱 실패 — 기존 전략 유지 ({e})",
                    suggested_tp_pct=round(original_tp, 2),
                    suggested_sl_pct=round(original_sl, 2),
                    lesson="",
                )
            }
