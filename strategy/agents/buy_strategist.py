"""매수 전략 전문가

코인 선정 + TP/SL 결정을 담당합니다.
기존 TradingAgent.select_coin() 프롬프트 구조를 계승하며,
MarketCondition과 AllocationDecision 컨텍스트를 추가로 활용합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent
from .market_analyst import MarketCondition
from .asset_manager import AllocationDecision
from ..ai_agent import AgentDecision
from ..market_analyzer import CoinSnapshot
from ..coin_selector import CoinScore

logger = logging.getLogger(__name__)


class BuyStrategist(BaseSpecialistAgent):
    """코인 선정과 매수 전략(TP/SL)을 결정하는 전문가 Agent"""

    ROLE_NAME = "buy_strategist"
    DISPLAY_NAME = "매수 전문가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 단기 변동성 매매에 특화된 매수 전문가입니다.\n"
            "시장 분석가의 판단과 자산 운용가의 배분 지침을 참고하되, \n"
            "코인 선정과 진입 타이밍 결정은 당신의 전문 영역입니다.\n"
            "2단계 손절 전략(1차 50% 매도 + 2차 전량)을 사용하며, \n"
            "수익 극대화를 위해 트레일링 익절(5%+ 진입)을 목표로 합니다."
        )

    def execute(self, context: dict) -> dict:
        """코인을 선정하고 TP/SL을 결정하여 AgentDecision을 반환

        Args:
            context: {
                "snapshots": list[CoinSnapshot],
                "market_condition": MarketCondition,
                "allocation": AllocationDecision,
                "eval_stats": dict | None,
                "coin_scores": list[CoinScore] | None,
            }

        Returns:
            {"decision": AgentDecision}
        """
        snapshots: list[CoinSnapshot] = context.get("snapshots", [])
        market_condition: MarketCondition | None = context.get("market_condition")
        allocation: AllocationDecision | None = context.get("allocation")
        eval_stats: dict | None = context.get("eval_stats")
        coin_scores: list[CoinScore] | None = context.get("coin_scores")

        if not snapshots:
            raise RuntimeError("[BuyStrategist] 스냅샷이 비어있음 — 매수 불가")

        # 시장 데이터 텍스트
        market_text = self._snapshots_to_text(snapshots, scores=coin_scores)
        history_text = self._eval_stats_to_text(eval_stats) if eval_stats else ""

        # 시장 상태 + 배분 컨텍스트
        specialist_context = self._build_specialist_context(market_condition, allocation)

        # clamp 범위 결정
        has_clamp = eval_stats and "tp_clamp_min" in eval_stats
        if has_clamp:
            tp_min = eval_stats.get("tp_clamp_min", 2.0)
            tp_max = eval_stats.get("tp_clamp_max", 6.0)
            sl1_min = eval_stats.get("sl_clamp_min", -4.5)
            sl1_max = eval_stats.get("sl_clamp_max", -1.5)
            sl2_min = max(-5.5, sl1_min - 1.5)
            sl2_max = min(-1.8, sl1_max - 0.3)
            tp_guide = (
                f"- 익절(take_profit_pct): 전략 최적화 범위 **+{tp_min:.1f}%~+{tp_max:.1f}%** 내에서 설정 (필수)\n"
                f"- 1차 손절(sl_1st_pct): **{sl1_min:.1f}%~{sl1_max:.1f}%** — 도달 시 보유량의 50%만 매도\n"
                f"- 2차 손절(sl_2nd_pct): **{sl2_min:.1f}%~{sl2_max:.1f}%** — 도달 시 나머지 전량 매도 "
                f"(반드시 1차보다 더 낮은 음수)"
            )
            rr_guide = (
                "- 2단계 손절 전략: 최대 실효 손실을 줄여 더 큰 익절 목표 가능. "
                "수익이 클수록 전략 성과 극대화."
            )
        else:
            tp_min, tp_max = 4.0, 10.0
            sl1_min, sl1_max = -1.5, -0.5
            sl2_min, sl2_max = -2.5, -0.8
            tp_guide = (
                "- 익절(take_profit_pct): 4.0%~10.0% 범위에서 설정 (트레일링으로 10%+ 목표)\n"
                "- 1차 손절(sl_1st_pct): -0.5%~-1.5% 범위 — 빠르게 인지, 50%만 매도 (기준: -1% 전후)\n"
                "- 2차 손절(sl_2nd_pct): -0.8%~-2.5% 범위 — 나머지 전량 매도 "
                "(기준: -1.5% 전후, 1차보다 반드시 더 낮은 음수)"
            )
            rr_guide = (
                "- 타이트 손절 전략: 빠르게 손실 인지·탈출. "
                "익절은 5%+ 진입 후 트레일링으로 크게 수익 극대화."
            )

        task_prompt = f"""전략: 코인 1개를 전액 매수 → 2단계 손절 또는 익절 시 매도 → 즉시 반복.
수익은 오직 가격 변동성에서만 나옵니다.

{specialist_context}

【2단계 손절 전략 설명】
- 1차 손절(sl_1st_pct) 도달 → 보유량 50%만 매도 → 나머지 50%는 반등 대기
- 2차 손절(sl_2nd_pct) 도달 → 나머지 전량 매도
- 실효 최대 손실 = sl_1st × 50% + sl_2nd × 50% (단순 손절보다 훨씬 유리)
- 따라서 익절 목표를 더 크게 잡아 수익을 극대화해야 함

아래는 빗썸 거래소 상위 코인의 실시간 시장 데이터입니다.

{market_text}
{history_text}

**코인 선정 기준 (우선순위 순)**
1. **변동폭 vs 익절 현실성** — 변동폭이 목표 익절%의 1.5배 이상인 코인만 선정 (예: 익절 3% 목표 시 변동폭 4.5% 이상 필수). 변동폭이 작은 코인은 절대 선정 금지
2. **최근 익절 종목 재선정 절대 금지** — 위 과거 거래 목록에서 【익절 직후】로 표시된 종목은 어떤 상황에서도 선정하지 마세요
3. 강한 단기 상승 모멘텀 — 현재가위치가 50~80% 구간이고, 모멘텀이 양수인 코인 최우선. 스코어가 높은 코인을 우선 검토하세요
4. 거래대금 충분 — 최소 50억원/24h 이상
5. 하락 중인 코인은 절대 선정 금지 — 24h 변동률이 음수이거나 현재가위치가 20% 이하인 코인 제외

**익절·손절 기준 설정 원칙 (반드시 준수)**
{rr_guide}
{tp_guide}
- 수수료(매수+매도 약 0.4%) 감안 후에도 순이익이 발생해야 함
- **핵심: 2단계 손절로 리스크를 줄였으니 익절은 크게! sl_2nd는 sl_1st보다 반드시 더 낮은 음수여야 함.**

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "symbol": "코인심볼(예:BTC)",
  "take_profit_pct": 익절퍼센트(숫자, 예:4.0),
  "sl_1st_pct": 1차손절퍼센트(음수숫자, 예:-2.0),
  "sl_2nd_pct": 2차손절퍼센트(음수숫자, sl_1st보다 더낮은음수, 예:-2.5),
  "confidence": 확신도(0.0~1.0),
  "reason": "선정 이유 (한국어, 100자 이내) — 상승 모멘텀 근거 포함"
}}"""

        logger.info(f"[BuyStrategist] 코인 선정 분석 중...")
        raw = self._call_llm(task_prompt, max_tokens=512)
        logger.info(f"[BuyStrategist] LLM 응답: {raw}")

        try:
            data = self._parse_json(raw)

            symbol = data["symbol"].upper()
            take_profit_pct = float(data["take_profit_pct"])
            sl_1st = float(data.get("sl_1st_pct", data.get("stop_loss_pct", sl1_max)))
            sl_2nd = float(data.get("sl_2nd_pct", data.get("stop_loss_pct", sl2_max)))
            confidence = float(data.get("confidence", 0.5))
            reason = data.get("reason", "")

            # ── 안전장치: 음수 보장 ──
            if sl_1st > 0:
                sl_1st = -abs(sl_1st)
            if sl_2nd > 0:
                sl_2nd = -abs(sl_2nd)

            # ── SL1 clamp ──
            sl_1st = max(sl1_min, min(sl1_max, sl_1st))

            # ── SL2 clamp: SL1보다 0.2% 이상 더 낮아야 함 ──
            sl_2nd = max(sl2_min, min(sl2_max, sl_2nd))
            if sl_2nd >= sl_1st - 0.2:
                sl_2nd = round(sl_1st - 0.3, 2)
                logger.warning(
                    f"[BuyStrategist 보정] sl_2nd를 sl_1st({sl_1st}%) - 0.3% → {sl_2nd}%로 보정"
                )

            # ── TP clamp ──
            if take_profit_pct < tp_min:
                logger.warning(
                    f"[BuyStrategist 보정] take_profit {take_profit_pct}% → {tp_min}% (범위 하한)"
                )
                take_profit_pct = tp_min
            if take_profit_pct > tp_max:
                logger.warning(
                    f"[BuyStrategist 보정] take_profit {take_profit_pct}% → {tp_max}% (범위 상한)"
                )
                take_profit_pct = tp_max

            # ── 실효 R:R 체크: TP / 실효손실(SL1×50%+SL2×50%) ──
            effective_loss = abs(sl_1st) * 0.5 + abs(sl_2nd) * 0.5
            rr_ratio = take_profit_pct / effective_loss if effective_loss > 0 else 99
            if rr_ratio < 0.8:
                take_profit_pct = round(effective_loss * 0.8, 2)
                logger.warning(
                    f"[BuyStrategist 보정] R:R 0.8 미달 → take_profit={take_profit_pct}%"
                )

            decision = AgentDecision(
                symbol=symbol,
                take_profit_pct=round(take_profit_pct, 2),
                stop_loss_1st_pct=round(sl_1st, 2),
                stop_loss_pct=round(sl_2nd, 2),
                confidence=confidence,
                reason=reason,
                llm_provider=self._llm.provider_name,
            )

            logger.info(
                f"[BuyStrategist] 선정: {decision.symbol} / "
                f"TP=+{decision.take_profit_pct}% / "
                f"SL1={decision.stop_loss_1st_pct}% / "
                f"SL2={decision.stop_loss_pct}% / "
                f"확신도={decision.confidence:.1f}"
            )
            return {"decision": decision}

        except (KeyError, ValueError) as e:
            logger.error(f"[BuyStrategist] 응답 파싱 실패: {e}\n원문: {raw}")
            raise RuntimeError(f"[BuyStrategist] 응답 파싱 실패: {e}")

    # ---------------------------------------------------------------- #
    #  유틸리티 메서드 (기존 TradingAgent에서 이관)                         #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _snapshots_to_text(
        snapshots: list[CoinSnapshot],
        scores: list[CoinScore] | None = None,
    ) -> str:
        """스냅샷을 AI 프롬프트 텍스트로 변환 (스코어 정보 포함)"""
        score_map = {sc.symbol: sc for sc in (scores or [])}
        lines = []
        for s in snapshots:
            # 변동폭 계산
            vol_pct = (
                (s.high_price - s.low_price) / s.low_price * 100
                if s.low_price > 0 else 0
            )
            # 현재가 위치
            price_range = s.high_price - s.low_price
            pos_pct = (
                (s.current_price - s.low_price) / price_range * 100
                if price_range > 0 else 50
            )
            line = (
                f"- {s.symbol}: "
                f"현재가={s.current_price:,.0f}원, "
                f"24h변동={s.change_pct_24h:+.2f}%, "
                f"24h거래대금={s.volume_krw_24h / 1e8:.1f}억원, "
                f"고가={s.high_price:,.0f}, 저가={s.low_price:,.0f}, "
                f"변동폭={vol_pct:.1f}%, 현재가위치={pos_pct:.0f}%"
            )
            sc = score_map.get(s.symbol)
            if sc:
                line += f" (모멘텀={sc.momentum:+.1f}, 스코어={sc.total_score:.1f})"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _eval_stats_to_text(stats: dict) -> str:
        """평가 통계를 프롬프트용 텍스트로 변환"""
        if not stats:
            return ""
        lines = [
            "\n**[과거 매매 성과 — 이 데이터를 기반으로 전략 파라미터를 결정하세요]**",
            f"- 최근 {stats['count']}건 매매: 승률 {stats['win_rate']:.0%} "
            f"(익절 {stats['win_count']}건, 손절 {stats['loss_count']}건)",
            f"- 평균 실현 수익률: {stats['avg_pnl_pct']:+.2f}%",
            f"- 평균 보유 시간: {stats['avg_hold_minutes']:.0f}분",
            f"- 기존 평균 익절 설정: +{stats['avg_tp_set']:.1f}%, 평균 손절 설정: {stats['avg_sl_set']:.1f}%",
            f"- AI 제안 평균 익절: +{stats['avg_suggested_tp']:.1f}%, 제안 평균 손절: {stats['avg_suggested_sl']:.1f}%",
        ]

        # 추세 방향 정보
        if stats.get("tp_direction"):
            tp_vals = stats.get("tp_trend", [])
            trend_str = "→".join(f"{v:.1f}" for v in tp_vals)
            lines.append(
                f"- 익절 제안 추세: {stats['tp_direction']} ({trend_str}%)"
            )

        # 최근 거래 코인 정보
        recent = stats.get("recent_trades", [])
        if recent:
            lines.append("- 최근 거래 코인 (선정 금지 원칙):")
            for t in recent:
                exit_kr = "익절" if t["exit_type"] == "take_profit" else "손절"
                warning = (
                    "【익절 직후 — 반드시 제외】"
                    if t["exit_type"] == "take_profit"
                    else "【손절 직후 — 가급적 제외】"
                )
                lines.append(
                    f"  * {t['symbol']}: {exit_kr} {t['pnl_pct']:+.2f}%, "
                    f"보유 {t['held_minutes']:.0f}분 {warning}"
                )

        if stats.get("recent_lessons"):
            lines.append("- 최근 교훈:")
            for lesson in stats["recent_lessons"]:
                lines.append(f"  * {lesson}")
        return "\n".join(lines)

    @staticmethod
    def _build_specialist_context(
        market_condition: MarketCondition | None,
        allocation: AllocationDecision | None,
    ) -> str:
        """시장 분석가 + 자산 운용가 컨텍스트를 프롬프트 텍스트로 변환"""
        parts = []
        if market_condition:
            parts.append(
                f"【시장 분석가 판단】\n"
                f"- 심리: {market_condition.sentiment} / 리스크: {market_condition.risk_level}\n"
                f"- 시장 강도: {market_condition.strength} / "
                f"권장 투자 비율: {market_condition.recommended_exposure}\n"
                f"- 요약: {market_condition.summary}"
            )
        if allocation:
            invest_str = "투자 진행" if allocation.should_invest else "투자 보류"
            parts.append(
                f"【자산 운용가 지침】\n"
                f"- 결정: {invest_str} / 투자 비율: {allocation.invest_ratio:.0%}\n"
                f"- 사유: {allocation.reason}"
            )
        return "\n\n".join(parts) if parts else ""
