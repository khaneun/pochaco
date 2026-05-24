"""매도 전략 전문가 (v4.0 — 포트폴리오 기반)

포트폴리오 종합 P&L을 분석하고 TP/SL 조정 여부를 판단합니다.
개별 코인이 아닌 포트폴리오 전체의 손익절을 관리합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent

logger = logging.getLogger(__name__)


class SellStrategist(BaseSpecialistAgent):
    """포트폴리오 레벨 익절/손절을 전략적으로 관리하는 매도 전문가 Agent"""

    ROLE_NAME = "sell_strategist"
    DISPLAY_NAME = "매도 전문가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 8개 코인 포트폴리오의 매도 타이밍 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "보유 중인 포트폴리오의 종합 수익률을 분석하고 TP/SL 조정 여부를 판단합니다.\n"
            "시스템이 30분마다 당신에게 질문하며, 당신의 조정이 최종 수익에 직결됩니다.\n\n"
            "【매도 메커니즘 이해 — v4.2 (반드시 숙지)】\n"
            "- 분할 익절: 포트폴리오 P&L +1.5% → 30% 매도 (수익 확정)\n"
            "- 분할 익절: +3.0% → 추가 30% 매도\n"
            "- 단일 손절: -1.5% → 잔여 전량 매도 (분할 손절은 폐지됨)\n"
            "- 빠른 폭락 컷: 5분 내 -3% / 30분 내 -5% → 즉시 전량\n"
            "- 트레일링 익절: 포트폴리오 P&L이 TP 도달 → 고점 추적 → 하락 시 매도\n\n"
            "【조정 의사결정 원칙 — 반드시 준수】\n"
            "★ 보유 30분 미만: 조기 조정은 불필요. adjust=false 권장.\n"
            "★ 보유 1~2시간 + 수익 없음: 익절선 소폭 하향(-0.5%) 고려\n"
            "★ 보유 2시간+ + 손실 중: 익절선 적극 하향, 빠른 탈출 유도\n"
            "★ 보유 4시간+: 기회비용 심각. 익절 최소 +3.0%까지 낮춰야 함\n"
            "★ 분할 매도가 이미 진행된 상태: 남은 물량 빠르게 처리 권장\n\n"
            "【절대 원칙】\n"
            "- 손절 하드캡 -2.0%는 절대 변경할 수 없음 (시스템 레벨 제한)\n"
            "- 조정할 때는 명확한 근거를 reason에 포함하세요\n"
            "- 8개 코인 중 6개 이상이 손실이면 → 빠른 탈출 강력 권고"
        )

    def execute(self, context: dict) -> dict:
        """포트폴리오 TP/SL 조정 여부를 판단

        Args:
            context: {
                "portfolio_name": str,
                "combined_pnl_pct": float,
                "holding_minutes": int,
                "original_tp": float,
                "original_sl": float,
                "coin_details": list[dict],  # [{symbol, pnl_pct, buy_krw, current_value}]
                "tier1_sold": bool,
            }

        Returns:
            {"adjust_result": {
                "adjust": bool,
                "new_take_profit_pct": float,
                "new_stop_loss_pct": float,
                "reason": str,
            }}
        """
        portfolio_name = context.get("portfolio_name", "UNKNOWN")
        combined_pnl_pct = context.get("combined_pnl_pct", 0)
        holding_minutes = context.get("holding_minutes", 0)
        original_tp = context.get("original_tp", 5.0)
        original_sl = context.get("original_sl", -2.0)
        coin_details = context.get("coin_details", [])
        tier1_sold = context.get("tier1_sold", False)

        try:
            time_guidance = self._get_time_guidance(holding_minutes)

            # 개별 코인 현황 텍스트
            coin_lines = []
            for cd in coin_details:
                coin_lines.append(
                    f"  - {cd['symbol']}: 수익 {cd['pnl_pct']:+.2f}%, "
                    f"투입 {cd['buy_krw']:,.0f}원 → 현재 {cd['current_value']:,.0f}원"
                )
            coin_text = "\n".join(coin_lines) if coin_lines else "  (정보 없음)"

            tier_status = "전량 보유"
            if tier1_sold:
                tier_status = "1차 분할 매도 완료 (잔여 약 50~67%)"

            task_prompt = f"""현재 보유 포트폴리오:
- 포트폴리오: {portfolio_name} ({len(coin_details)}개 코인)
- 종합 수익률: {combined_pnl_pct:+.2f}%
- 보유 시간: {holding_minutes}분 ({holding_minutes / 60:.1f}시간)
- 원래 익절 기준: +{original_tp}%
- 원래 손절 기준: {original_sl}% (최대 -2.0%)
- 분할 매도 상태: {tier_status}

**개별 코인 현황:**
{coin_text}

**시간 기반 가이드라인:** {time_guidance}

**조정 원칙:**
- 익절은 낮추는 방향 — 오래 보유할수록 익절 낮추기 (최소 +3.0% 유지)
- 손절은 -1.5%를 절대 초과할 수 없음 (하드캡)
- 분할 매도가 이미 진행되었다면 남은 물량의 빠른 처리를 고려
- 포트폴리오 내 대부분 코인이 하락 중이면 익절을 낮추어 빠른 탈출 권장

아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "adjust": true 또는 false,
  "new_take_profit_pct": 숫자(조정 불필요 시 원래값),
  "new_stop_loss_pct": 음수숫자(조정 불필요 시 원래값, 최대 -1.5),
  "reason": "이유 (한국어, 50자 이내)"
}}"""

            raw = self._call_llm(task_prompt, max_tokens=300)
            logger.info(f"[SellStrategist] 전략 조정 응답: {raw}")

            data = self._parse_json(raw)

            if data.get("adjust"):
                new_tp = float(data.get("new_take_profit_pct", original_tp))
                new_sl = float(data.get("new_stop_loss_pct", original_sl))

                # ── 안전장치: TP 3.0~12%, SL 최대 -1.5% ──
                new_tp = max(3.0, min(12.0, new_tp))
                if new_sl > 0:
                    new_sl = -abs(new_sl)
                new_sl = max(-1.5, min(-0.5, new_sl))

                data["new_take_profit_pct"] = round(new_tp, 2)
                data["new_stop_loss_pct"] = round(new_sl, 2)

            logger.info(
                f"[SellStrategist] {portfolio_name}: "
                f"조정={'예' if data.get('adjust') else '아니오'} / "
                f"{data.get('reason', '')}"
            )
            return {"adjust_result": data}

        except Exception as e:
            logger.error(f"[SellStrategist] 분석 실패: {e}")
            return {
                "adjust_result": {
                    "adjust": False,
                    "new_take_profit_pct": original_tp,
                    "new_stop_loss_pct": original_sl,
                    "reason": "파싱 실패, 기존 전략 유지",
                }
            }

    @staticmethod
    def _get_time_guidance(holding_minutes: int) -> str:
        """보유 시간에 따른 가이드라인 텍스트 생성"""
        if holding_minutes < 30:
            return "아직 보유 초기 단계입니다. 급격한 변동이 없다면 기존 전략을 유지하세요."
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
                "현재 수익이 +1% 이상이면 즉시 익절을, 손실이면 빠른 탈출을 강력히 권장합니다."
            )

    def evaluate_tier1_action(self, context: dict) -> dict:
        """Tier1(-1.0%) 진입 시 즉각 매도 비율 AI 평가

        Args:
            context: {
                "portfolio_name": str,
                "pnl_pct": float,       # 현재 종합 수익률 (≤ -1.0%)
                "coin_details": list,   # [{symbol, pnl_pct, buy_krw, current_value}]
                "holding_minutes": int,
            }

        Returns:
            {"tier1_result": {"sell_ratio": float(0.33~0.67), "reason": str}}
        """
        portfolio_name = context.get("portfolio_name", "UNKNOWN")
        pnl_pct = context.get("pnl_pct", -1.0)
        coin_details = context.get("coin_details", [])
        holding_minutes = context.get("holding_minutes", 0)

        try:
            coin_lines = []
            for cd in coin_details:
                coin_lines.append(
                    f"  - {cd['symbol']}: 수익 {cd['pnl_pct']:+.2f}%, "
                    f"투입 {cd['buy_krw']:,.0f}원 → 현재 {cd['current_value']:,.0f}원"
                )
            coin_text = "\n".join(coin_lines) if coin_lines else "  (정보 없음)"

            task_prompt = f"""포트폴리오가 -1.0% 손실 구간에 진입했습니다. 즉각 매도 비율을 결정하세요.

포트폴리오: {portfolio_name} ({len(coin_details)}개 코인)
종합 수익률: {pnl_pct:+.2f}%
보유 시간: {holding_minutes}분

**개별 코인 현황:**
{coin_text}

**판단 기준:**
- 최악의 경우 -1.5%에서 잔여 전량 손절됩니다
- 코인들이 전반적으로 계속 하락 중 → 매도 비율 높게 (0.60~0.67)
- 일부만 하락, 반등 가능성 있음 → 매도 비율 중간 (0.45~0.55)
- 단기 과매도, 반등 기대 → 매도 비율 낮게 (0.33~0.44)
- 반드시 0.33 이상 0.67 이하로 결정

아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "sell_ratio": 0.33~0.67 사이 숫자,
  "reason": "판단 근거 (한국어, 40자 이내)"
}}"""

            raw = self._call_llm(task_prompt, max_tokens=200)
            logger.info(f"[SellStrategist] Tier1 평가 응답: {raw}")

            data = self._parse_json(raw)
            ratio = float(data.get("sell_ratio", 0.5))
            ratio = max(0.33, min(0.67, ratio))
            data["sell_ratio"] = round(ratio, 2)

            return {"tier1_result": data}

        except Exception as e:
            logger.error(f"[SellStrategist] Tier1 평가 실패: {e}")
            return {"tier1_result": {"sell_ratio": 0.5, "reason": "기본값 (평가 실패)"}}
