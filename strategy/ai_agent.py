"""AI Agent - 코인 선정 및 익절/손절 기준 결정 + 성과 기반 자기 개선

LLM 공급자에 의존하지 않습니다. core.llm_provider.get_llm_provider()가
.env의 LLM_PROVIDER 값에 따라 Anthropic / OpenAI / Gemini 중 하나를 주입합니다.
"""
import json
import logging
from dataclasses import dataclass

from core.llm_provider import BaseLLMProvider, get_llm_provider
from .market_analyzer import CoinSnapshot

logger = logging.getLogger(__name__)


@dataclass
class AgentDecision:
    symbol: str
    take_profit_pct: float
    stop_loss_pct: float
    confidence: float
    reason: str
    llm_provider: str = ""      # 어떤 LLM이 결정했는지 기록


@dataclass
class TradeEvaluation:
    """매매 후 AI 평가 결과"""
    evaluation: str             # 평가 텍스트
    suggested_tp_pct: float     # 다음 매매에 제안하는 익절%
    suggested_sl_pct: float     # 다음 매매에 제안하는 손절%
    lesson: str                 # 핵심 교훈 한 줄


class TradingAgent:
    """LLM 공급자에 위임하는 매매 의사결정 에이전트"""

    def __init__(self, llm: BaseLLMProvider | None = None):
        self._llm = llm or get_llm_provider()

    @property
    def provider_name(self) -> str:
        return self._llm.provider_name

    # ---------------------------------------------------------------- #
    #  공통 프롬프트 빌더                                                 #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _snapshots_to_text(snapshots: list[CoinSnapshot]) -> str:
        lines = []
        for s in snapshots:
            lines.append(
                f"- {s.symbol}: "
                f"현재가={s.current_price:,.0f}원, "
                f"24h변동={s.change_pct_24h:+.2f}%, "
                f"24h거래대금={s.volume_krw_24h / 1e8:.1f}억원, "
                f"고가={s.high_price:,.0f}, 저가={s.low_price:,.0f}"
            )
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
            lines.append("- 최근 거래 코인 (재선정 시 주의):")
            for t in recent:
                exit_kr = "익절" if t["exit_type"] == "take_profit" else "손절"
                lines.append(
                    f"  * {t['symbol']}: {exit_kr} {t['pnl_pct']:+.2f}%, "
                    f"보유 {t['held_minutes']:.0f}분"
                )

        if stats.get("recent_lessons"):
            lines.append("- 최근 교훈:")
            for lesson in stats["recent_lessons"]:
                lines.append(f"  * {lesson}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- #
    #  코인 선정                                                          #
    # ---------------------------------------------------------------- #
    def select_coin(
        self,
        snapshots: list[CoinSnapshot],
        eval_stats: dict | None = None,
    ) -> AgentDecision:
        """시장 데이터 + 과거 성과를 분석해 매수할 코인 1개와 익절/손절 기준을 결정"""

        market_text = self._snapshots_to_text(snapshots)
        history_text = self._eval_stats_to_text(eval_stats) if eval_stats else ""

        # 과거 성과가 있으면 적응형 범위를 프롬프트에 강제
        if eval_stats and eval_stats.get("count", 0) >= 3:
            tp_min = eval_stats.get("tp_clamp_min", 2.0)
            tp_max = eval_stats.get("tp_clamp_max", 8.0)
            sl_min = eval_stats.get("sl_clamp_min", -3.0)
            sl_max = eval_stats.get("sl_clamp_max", -1.0)
            tp_guide = (
                f"- 익절(take_profit_pct): 과거 성과 기반 적응 범위 **+{tp_min:.1f}%~+{tp_max:.1f}%** 내에서 설정 (필수)\n"
                f"- 손절(stop_loss_pct): 적응 범위 **{sl_min:.1f}%~{sl_max:.1f}%** 내에서 설정 (필수)"
            )
            rr_guide = "- 리워드:리스크 비율 최소 2:1 이상 (과거 성과 기반 유연 적용)"
        else:
            tp_min, tp_max = 2.0, 8.0
            sl_min, sl_max = -3.0, -1.0
            tp_guide = (
                "- 익절(take_profit_pct): 2%~8% 범위에서 현실적으로 설정\n"
                "- 손절(stop_loss_pct): -1.0%~-3.0% 범위에서 설정"
            )
            rr_guide = "- 리워드:리스크 비율 최소 2:1 이상"

        prompt = f"""당신은 단기 변동성 매매 전문 트레이더입니다.
전략: 코인 1개를 전액 매수 → 익절 또는 손절 시 전량 매도 → 즉시 반복.
수익은 오직 가격 변동성에서만 나옵니다.

아래는 빗썸 거래소 상위 코인의 실시간 시장 데이터입니다.

{market_text}
{history_text}

**코인 선정 기준 (우선순위 순)**
1. 강한 단기 상승 모멘텀 — 현재가가 당일 고가 대비 10% 이내에 있고, 24h 변동률이 양수이며 상승 추세인 코인 최우선
2. 거래대금 충분 — 최소 50억원/24h 이상, 슬리피지 최소화 및 빠른 체결 보장
3. 고저 차이가 클 것 — 24h 고저 차이가 5% 이상인 코인 우선 (변동성 확보)
4. 하락 중인 코인은 절대 선정 금지 — 24h 변동률이 음수이거나 현재가가 당일 저가 근처인 코인 제외

**익절·손절 기준 설정 원칙 (반드시 준수)**
{rr_guide}
{tp_guide}
- 수수료(매수+매도 약 0.4%) 감안 후에도 순이익이 발생해야 함
- **핵심: 달성 가능한 현실적 목표를 설정하세요. 너무 높은 익절선은 기회비용만 증가시킵니다.**

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "symbol": "코인심볼(예:BTC)",
  "take_profit_pct": 익절퍼센트(숫자, 예:3.5),
  "stop_loss_pct": 손절퍼센트(음수숫자, 예:-1.5),
  "confidence": 확신도(0.0~1.0),
  "reason": "선정 이유 (한국어, 100자 이내) — 상승 모멘텀 근거 포함"
}}"""

        logger.info(f"AI Agent ({self.provider_name}): 코인 선정 분석 중...")
        raw = self._llm.chat(prompt, max_tokens=512)
        logger.info(f"AI Agent 응답: {raw}")

        try:
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)

            symbol = data["symbol"].upper()
            take_profit_pct = float(data["take_profit_pct"])
            stop_loss_pct = float(data["stop_loss_pct"])
            confidence = float(data.get("confidence", 0.5))
            reason = data.get("reason", "")

            # ── 안전장치: 적응형 범위 clamp ──
            # tp_min/tp_max, sl_min/sl_max는 eval_stats 기반 또는 기본값
            if stop_loss_pct > 0:
                stop_loss_pct = -abs(stop_loss_pct)
            if stop_loss_pct > sl_max:
                logger.warning(f"[AI 보정] stop_loss {stop_loss_pct}% → {sl_max}% (범위 초과)")
                stop_loss_pct = sl_max
            if stop_loss_pct < sl_min:
                logger.warning(f"[AI 보정] stop_loss {stop_loss_pct}% → {sl_min}% (범위 초과)")
                stop_loss_pct = sl_min

            if take_profit_pct < tp_min:
                logger.warning(f"[AI 보정] take_profit {take_profit_pct}% → {tp_min}% (범위 하한)")
                take_profit_pct = tp_min
            if take_profit_pct > tp_max:
                logger.warning(f"[AI 보정] take_profit {take_profit_pct}% → {tp_max}% (범위 상한)")
                take_profit_pct = tp_max

            # R:R 최소 2:1 보정
            rr_ratio = take_profit_pct / abs(stop_loss_pct) if stop_loss_pct != 0 else 99
            if rr_ratio < 2.0:
                take_profit_pct = abs(stop_loss_pct) * 2.0
                logger.warning(f"[AI 보정] R:R 미달 → take_profit={take_profit_pct}%")

            return AgentDecision(
                symbol=symbol,
                take_profit_pct=round(take_profit_pct, 2),
                stop_loss_pct=round(stop_loss_pct, 2),
                confidence=confidence,
                reason=reason,
                llm_provider=self.provider_name,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"AI Agent 응답 파싱 실패: {e}\n원문: {raw}")
            raise RuntimeError(f"AI Agent 응답 파싱 실패: {e}")

    # ---------------------------------------------------------------- #
    #  매매 후 성과 평가 (Post-Trade Review)                              #
    # ---------------------------------------------------------------- #
    def evaluate_trade(
        self,
        symbol: str,
        buy_price: float,
        sell_price: float,
        pnl_pct: float,
        held_minutes: float,
        exit_type: str,
        original_tp: float,
        original_sl: float,
        agent_reason: str,
        eval_stats: dict | None = None,
    ) -> TradeEvaluation:
        """매매 완료 후 성과를 평가하고 다음 전략 파라미터를 제안"""

        history_summary = ""
        if eval_stats and eval_stats.get("count", 0) > 0:
            history_summary = f"""
최근 {eval_stats['count']}건 누적 성과:
- 승률: {eval_stats['win_rate']:.0%}, 평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%
- 평균 보유시간: {eval_stats['avg_hold_minutes']:.0f}분
- 평균 익절 설정: +{eval_stats['avg_tp_set']:.1f}%, 평균 손절 설정: {eval_stats['avg_sl_set']:.1f}%"""

        prompt = f"""당신은 트레이딩 전략 평가 전문가입니다.
아래 매매 결과를 분석하고, 다음 매매를 위한 전략 파라미터를 제안하세요.

**이번 매매 결과:**
- 코인: {symbol}
- 매수가: {buy_price:,.0f}원 → 매도가: {sell_price:,.0f}원
- 실현 수익률: {pnl_pct:+.2f}%
- 보유 시간: {held_minutes:.0f}분 ({held_minutes/60:.1f}시간)
- 종료 유형: {"익절 달성" if exit_type == "take_profit" else "손절 발동"}
- 원래 익절 기준: +{original_tp}%, 원래 손절 기준: {original_sl}%
- AI 선정 이유: {agent_reason}
{history_summary}

**평가 관점:**
1. 익절/손절 기준이 현실적이었는가? (너무 높으면 기회비용, 너무 낮으면 수수료 손실)
2. 보유 시간이 적절했는가? (장시간 보유는 기회비용 증가)
3. 코인 선정 판단은 적절했는가?

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "evaluation": "이번 매매에 대한 평가 (한국어, 150자 이내)",
  "suggested_tp_pct": 다음매매추천익절퍼센트(숫자, 2.0~8.0 범위),
  "suggested_sl_pct": 다음매매추천손절퍼센트(음수숫자, -1.0~-3.0 범위),
  "lesson": "핵심 교훈 한 줄 (한국어, 50자 이내)"
}}"""

        logger.info(f"AI Agent ({self.provider_name}): 매매 평가 중... [{symbol} {pnl_pct:+.2f}%]")
        raw = self._llm.chat(prompt, max_tokens=512)
        logger.info(f"AI 평가 응답: {raw}")

        try:
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)

            suggested_tp = float(data.get("suggested_tp_pct", original_tp))
            suggested_sl = float(data.get("suggested_sl_pct", original_sl))

            # 범위 보정
            suggested_tp = max(2.0, min(8.0, suggested_tp))
            if suggested_sl > 0:
                suggested_sl = -abs(suggested_sl)
            suggested_sl = max(-3.0, min(-1.0, suggested_sl))

            return TradeEvaluation(
                evaluation=data.get("evaluation", "평가 없음"),
                suggested_tp_pct=round(suggested_tp, 2),
                suggested_sl_pct=round(suggested_sl, 2),
                lesson=data.get("lesson", ""),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"AI 평가 응답 파싱 실패: {e}\n원문: {raw}")
            return TradeEvaluation(
                evaluation=f"파싱 실패 — 기존 전략 유지 ({e})",
                suggested_tp_pct=round(original_tp, 2),
                suggested_sl_pct=round(original_sl, 2),
                lesson="",
            )

    # ---------------------------------------------------------------- #
    #  보유 중 전략 동적 조정                                              #
    # ---------------------------------------------------------------- #
    def should_adjust_strategy(
        self,
        symbol: str,
        buy_price: float,
        current_price: float,
        current_pnl_pct: float,
        holding_minutes: int,
        original_tp: float,
        original_sl: float,
    ) -> dict:
        """포지션 보유 중 익절/손절 기준 조정 여부를 LLM에게 질의

        보유 시간이 길어지면 기회비용을 고려해 익절선을 낮추는 방향으로 유도.
        """

        # 보유 시간에 따른 가이드라인
        if holding_minutes < 30:
            time_guidance = "아직 보유 초기 단계입니다. 급격한 변동이 없다면 기존 전략을 유지하세요."
        elif holding_minutes < 120:
            time_guidance = (
                "보유 시간이 30분 이상 경과했습니다. "
                "현재 수익이 있다면 익절선을 낮춰 수익 확보를 고려하세요."
            )
        elif holding_minutes < 360:
            time_guidance = (
                "보유 시간이 2시간 이상입니다. 기회비용이 발생하고 있습니다. "
                "익절선을 적극적으로 낮춰 빠른 수익 실현 또는 본전 탈출을 권장합니다."
            )
        else:
            time_guidance = (
                "보유 시간이 6시간 이상으로 매우 깁니다. "
                "현재 수익이 +1% 이상이면 즉시 익절을, 손실이면 손절선 상향을 강력히 권장합니다."
            )

        prompt = f"""현재 보유 포지션:
- 코인: {symbol}
- 매수가: {buy_price:,.0f}원
- 현재가: {current_price:,.0f}원
- 현재 수익률: {current_pnl_pct:+.2f}%
- 보유 시간: {holding_minutes}분 ({holding_minutes/60:.1f}시간)
- 원래 익절 기준: +{original_tp}%
- 원래 손절 기준: {original_sl}%

**시간 기반 가이드라인:** {time_guidance}

현재 상황에서 익절/손절 기준을 조정해야 하나요?
- 조정 시 익절선은 현재 수익률보다 최소 0.5% 이상으로 설정
- 수수료(0.4%)를 감안해 최소 +1.5% 이상의 익절선 유지

아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "adjust": true 또는 false,
  "new_take_profit_pct": 숫자(조정 불필요 시 원래값),
  "new_stop_loss_pct": 숫자(조정 불필요 시 원래값),
  "reason": "이유 (한국어, 50자 이내)"
}}"""

        raw = self._llm.chat(prompt, max_tokens=256)
        logger.info(f"AI 전략 조정 응답: {raw}")
        try:
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)

            if data.get("adjust"):
                new_tp = float(data.get("new_take_profit_pct", original_tp))
                new_sl = float(data.get("new_stop_loss_pct", original_sl))
                # 안전장치
                new_tp = max(1.5, min(15.0, new_tp))
                if new_sl > 0:
                    new_sl = -abs(new_sl)
                new_sl = max(-5.0, min(-0.5, new_sl))
                data["new_take_profit_pct"] = round(new_tp, 2)
                data["new_stop_loss_pct"] = round(new_sl, 2)

            return data
        except json.JSONDecodeError:
            return {
                "adjust": False,
                "new_take_profit_pct": original_tp,
                "new_stop_loss_pct": original_sl,
                "reason": "파싱 실패, 기존 전략 유지",
            }
