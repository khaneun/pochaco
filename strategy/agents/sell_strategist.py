"""매도 전략 전문가

보유 중인 포지션의 상태를 분석하고 TP/SL 조정 여부를 판단합니다.
기존 TradingAgent.should_adjust_strategy() 프롬프트를 계승합니다.
보유 시간이 길어지면 기회비용을 고려하여 익절선을 낮추는 방향으로 유도합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent

logger = logging.getLogger(__name__)


class SellStrategist(BaseSpecialistAgent):
    """익절과 손절을 전략적으로 수행하는 매도 전문가 Agent"""

    ROLE_NAME = "sell_strategist"
    DISPLAY_NAME = "매도 전문가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 익절과 손절을 전략적으로 수행하는 매도 전문가입니다.\n"
            "보유 중인 포지션의 상태를 분석하고 TP/SL 조정 여부를 판단합니다.\n"
            "보유 시간이 길어지면 기회비용을 고려하여 익절선을 낮추는 방향으로 유도합니다.\n"
            "2단계 손절 전략을 항상 고려하며, 1차 손절 실행 여부에 따라 전략을 조정합니다."
        )

    def execute(self, context: dict) -> dict:
        """보유 포지션의 TP/SL 조정 여부를 판단

        Args:
            context: {
                "symbol": str,
                "buy_price": float,
                "current_price": float,
                "current_pnl_pct": float,
                "holding_minutes": int,
                "original_tp": float,
                "original_sl": float,
                "original_sl_1st": float | None,
                "sl1_executed": bool,
            }

        Returns:
            {
                "adjust": bool,
                "new_take_profit_pct": float,
                "new_stop_loss_1st_pct": float,
                "new_stop_loss_pct": float,
                "reason": str,
            }
        """
        symbol = context.get("symbol", "UNKNOWN")
        buy_price = context.get("buy_price", 0)
        current_price = context.get("current_price", 0)
        current_pnl_pct = context.get("current_pnl_pct", 0)
        holding_minutes = context.get("holding_minutes", 0)
        original_tp = context.get("original_tp", 5.0)
        original_sl = context.get("original_sl", -2.0)
        original_sl_1st = context.get("original_sl_1st")
        sl1_executed = context.get("sl1_executed", False)

        try:
            # 보유 시간에 따른 가이드라인
            time_guidance = self._get_time_guidance(holding_minutes)

            sl1_info = f"{original_sl_1st}%" if original_sl_1st else "미설정"
            sl1_status = (
                "1차 손절 이미 실행됨 (50% 매도 완료, 나머지 50% 보유 중)"
                if sl1_executed
                else "1차 손절 미실행"
            )

            task_prompt = f"""현재 보유 포지션 (2단계 손절 전략):
- 코인: {symbol}
- 매수가: {buy_price:,.0f}원
- 현재가: {current_price:,.0f}원
- 현재 수익률: {current_pnl_pct:+.2f}%
- 보유 시간: {holding_minutes}분 ({holding_minutes / 60:.1f}시간)
- 원래 익절 기준: +{original_tp}%
- 원래 1차 손절: {sl1_info} (50% 매도 포인트)
- 원래 2차 손절: {original_sl}% (전량 매도 포인트)
- 현재 상태: {sl1_status}

**시간 기반 가이드라인:** {time_guidance}

**조정 원칙:**
- 익절은 낮추는 방향 — 오래 보유할수록 익절 낮추기 (최소 +1.5% 유지)
- 1차 손절은 현재 수익률보다 낮게 유지 (이미 실행됐다면 조정 불필요)
- 2차 손절은 1차보다 0.3% 이상 더 낮아야 함
- 현재 수익이 있다면 수익 확보 위해 익절 낮추기 검토

아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "adjust": true 또는 false,
  "new_take_profit_pct": 숫자(조정 불필요 시 원래값),
  "new_stop_loss_1st_pct": 음수숫자(1차손절, 조정 불필요 시 원래값),
  "new_stop_loss_pct": 음수숫자(2차손절, 조정 불필요 시 원래값),
  "reason": "이유 (한국어, 50자 이내)"
}}"""

            raw = self._call_llm(task_prompt, max_tokens=300)
            logger.info(f"[SellStrategist] 전략 조정 응답: {raw}")

            data = self._parse_json(raw)

            if data.get("adjust"):
                new_tp = float(data.get("new_take_profit_pct", original_tp))
                new_sl_1st = float(data.get(
                    "new_stop_loss_1st_pct",
                    original_sl_1st if original_sl_1st else original_sl * 0.8,
                ))
                new_sl = float(data.get("new_stop_loss_pct", original_sl))

                # ── 안전장치: TP 3~12%, SL1 -2~-0.5%, SL2 -3~-0.8% clamp ──
                new_tp = max(3.0, min(12.0, new_tp))
                if new_sl_1st > 0:
                    new_sl_1st = -abs(new_sl_1st)
                if new_sl > 0:
                    new_sl = -abs(new_sl)
                new_sl_1st = max(-2.0, min(-0.5, new_sl_1st))
                new_sl = max(-3.0, min(-0.8, new_sl))
                if new_sl >= new_sl_1st - 0.2:
                    new_sl = new_sl_1st - 0.3

                data["new_take_profit_pct"] = round(new_tp, 2)
                data["new_stop_loss_1st_pct"] = round(new_sl_1st, 2)
                data["new_stop_loss_pct"] = round(new_sl, 2)

            logger.info(
                f"[SellStrategist] {symbol}: "
                f"조정={'예' if data.get('adjust') else '아니오'} / "
                f"{data.get('reason', '')}"
            )
            return data

        except Exception as e:
            logger.error(f"[SellStrategist] 분석 실패: {e}")
            return {
                "adjust": False,
                "new_take_profit_pct": original_tp,
                "new_stop_loss_1st_pct": original_sl_1st if original_sl_1st else original_sl * 0.8,
                "new_stop_loss_pct": original_sl,
                "reason": "파싱 실패, 기존 전략 유지",
            }

    @staticmethod
    def _get_time_guidance(holding_minutes: int) -> str:
        """보유 시간에 따른 가이드라인 텍스트 생성"""
        if holding_minutes < 30:
            return (
                "아직 보유 초기 단계입니다. "
                "급격한 변동이 없다면 기존 전략을 유지하세요."
            )
        elif holding_minutes < 120:
            return (
                "보유 시간이 30분 이상 경과했습니다. "
                "현재 수익이 있다면 익절선을 낮춰 수익 확보를 고려하세요."
            )
        elif holding_minutes < 360:
            return (
                "보유 시간이 2시간 이상입니다. 기회비용이 발생하고 있습니다. "
                "익절선을 적극적으로 낮춰 빠른 수익 실현 또는 본전 탈출을 권장합니다."
            )
        else:
            return (
                "보유 시간이 6시간 이상으로 매우 깁니다. "
                "현재 수익이 +1% 이상이면 즉시 익절을, "
                "손실이면 2차 손절선 상향을 강력히 권장합니다."
            )
