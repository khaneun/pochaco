"""매수 전략 전문가 (v4.0 — 포트폴리오 기반)

8개 코인으로 구성된 분산 포트폴리오를 선정합니다.
단일 LLM 호출로 8개 코인을 동시에 선정하여 포트폴리오 일관성을 확보합니다.
"""
import logging

from .base_agent import BaseSpecialistAgent
from .market_analyst import MarketCondition
from .asset_manager import AllocationDecision
from ..ai_agent import PortfolioDecision, PortfolioCoinPick
from ..market_analyzer import CoinSnapshot
from ..coin_selector import CoinScore

logger = logging.getLogger(__name__)

# 포트폴리오 코인 수 — v4.3: 8개 분산 → 5개 집중
# 코인당 자본 ↑ (익절 절대액 의미 있게) + 신호 품질 ↑ (낮은 score 코인 제외)
_PORTFOLIO_SIZE = 5


class BuyStrategist(BaseSpecialistAgent):
    """8개 코인 포트폴리오를 선정하는 매수 전문가 Agent"""

    ROLE_NAME = "buy_strategist"
    DISPLAY_NAME = "매수 전문가"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._base_prompt = (
            "당신은 5개 코인 집중 포트폴리오 구성 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "빗썸 거래소 상위 코인 중 5개를 선정하여 집중 포트폴리오를 구성합니다.\n"
            "5개 코인이 동시에 매수·매도되므로, 포트폴리오 전체의 종합 수익률이 관건입니다.\n"
            "개별 코인 수익률이 아니라 '5개의 합산'이 플러스여야 성공입니다.\n\n"
            "【포트폴리오 구성 철학 — 가장 중요】\n"
            "★ 5개로 줄였으니 신호 품질에 집중하세요. 약한 신호 5번째 채워넣지 마세요.\n"
            "★ 대형주(BTC,ETH,XRP 등) 1~2개 + 중형 알트코인 2~3개 + 강한 모멘텀 1개로 구성하세요.\n"
            "★ 같은 섹터(예: AI 코인끼리, 밈코인끼리) 중복 선정을 피하세요.\n"
            "★ 변동폭이 너무 작은 코인(< 2%)은 포트폴리오에 기여하지 못합니다.\n\n"
            "【★ 기술 지표 기반 선정 — 가장 중요 ★】\n"
            "각 코인의 기술 지표(RSI, MACD, OBV, 볼린저밴드)를 반드시 확인하세요.\n"
            "- RSI > 65인 코인은 과매수 접근 중 → 선정 시 감점 사유\n"
            "- RSI > 70인 코인은 조정 임박 → 선정 금지\n"
            "- MACD 하락/데드크로스인 코인 → 상승 모멘텀 소진, 3개 이하로 제한\n"
            "- OBV 하락 + 가격 상승 → 가짜 상승, 선정 금지\n"
            "- 볼린저밴드 상단(>80%) → 밴드 회귀 임박, 선정 자제\n"
            "- 이상적 진입: RSI 35~55, MACD 상승/골든크로스, OBV 상승, BB 하단~중간\n\n"
            "【선정 금지 규칙 — 반드시 준수】\n"
            "1. 24h 변동률이 -1.5% 이하인 코인은 절대 선정 금지 (강한 하락세, 이전 -3% → -1.5% 강화)\n"
            "2. 현재가 위치가 15% 이하인 코인 금지 (바닥 다이빙 위험)\n"
            "3. 24h 거래대금 100억원 미만 금지 (유동성 부족, 이전 50억 → 100억 강화)\n"
            "4. RSI > 70인 코인 선정 금지 (과매수)\n"
            "5. 가격-거래량 다이버전스 코인 선정 금지 (가짜 상승)\n"
            "6. 최근 포트폴리오에 포함된 종목은 가급적 회피\n"
            "7. ★ 직전 손절 코인은 절대 선정 금지 (시스템이 12시간 쿨다운 자동 적용 중)\n"
            "8. ★ 직전 평가에서 'feedback_prompt' 또는 'reflection_summary'에 명시된\n"
            "    회피 코인이 있으면 반드시 후보에서 제외 (예: 'MERL/KAT 매수 금지')\n\n"
            "【TP/SL 설정 원칙 — 분할 익절 시스템 도입 후】\n"
            "★ 시스템이 자동으로 분할 익절을 수행합니다 (반드시 인지):\n"
            "  · +1.5% 도달 시: 30% 자동 매도 (수익 확정)\n"
            "  · +3.0% 도달 시: 30% 추가 자동 매도 (총 60% 매도)\n"
            "  · take_profit_pct 도달 시: 잔여 40% 트레일링 익절\n\n"
            "★ 따라서 take_profit_pct는 '잔여분 트레일링 진입선'이며,\n"
            "  너무 높게 설정하면 분할 익절만으로 끝나 작은 수익에 그칩니다.\n"
            "  너무 낮게 설정하면 트레일링이 일찍 작동해 큰 추세를 놓칩니다.\n\n"
            "【과거 데이터 기반 권장값】\n"
            "- 과거 30일 데이터: TP 도달률 18.9% (5건 중 1건만 도달)\n"
            "- TP=3.0% 설정 38건 → 평균 -1.39% (도달 못함)\n"
            "- TP=4.0~5.0% 설정 → 평균 -2.7% ~ -4% (대부분 도달 못함)\n"
            "- 권장: take_profit_pct = **3.5~5.0%** (4% 근처가 균형)\n"
            "- SL: -1.5% 단일 손절. (분할 손절 v4.2에서 폐지 — 회복 방해)\n"
            "- 수수료 감안: 8코인 × 매수/매도 수수료 0.25% = 총 약 0.5% 비용 고려.\n\n"
            "【★ 절대 강제 룰 — 위반 시 점수 0점 ★】\n"
            "- 약세 시장(market_condition.sentiment=bearish) → 코인 0개 반환 (보류)\n"
            "- 시장 recommended_exposure ≤ 0.3 → 코인 3개만 반환 (최소 분산)\n"
            "- 후보 중 RSI≤40 비율이 50%↑이면 → 시장 과매도, 안전한 BTC/ETH 위주\n"
            "- 동일 섹터 코인 3개 초과 금지 (예: 밈코인 3개 초과 금지)\n"
            "- AI 미선정으로 백업이 떠받치는 사이클이 5회 누적되면 → 강제 보류 신호"
        )

    def execute(self, context: dict) -> dict:
        """8개 코인 포트폴리오를 선정하여 PortfolioDecision을 반환

        Args:
            context: {
                "snapshots": list[CoinSnapshot],
                "market_condition": MarketCondition,
                "allocation": AllocationDecision,
                "eval_stats": dict | None,
                "coin_scores": list[CoinScore] | None,
                "coin_profiles": dict[str, str],
            }

        Returns:
            {"portfolio_decision": PortfolioDecision}
        """
        snapshots: list[CoinSnapshot] = context.get("snapshots", [])
        market_condition: MarketCondition | None = context.get("market_condition")
        allocation: AllocationDecision | None = context.get("allocation")
        eval_stats: dict | None = context.get("eval_stats")
        coin_scores: list[CoinScore] | None = context.get("coin_scores")
        coin_profiles: dict[str, str] = context.get("coin_profiles", {})
        coin_profile_advisory: str = context.get("coin_profile_advisory", "")

        if not snapshots:
            raise RuntimeError("[BuyStrategist] 스냅샷이 비어있음 — 매수 불가")

        # 시장 데이터 텍스트
        market_text = self._snapshots_to_text(snapshots, scores=coin_scores)
        history_text = self._eval_stats_to_text(eval_stats) if eval_stats else ""
        profiles_text = self._profiles_to_text(coin_profiles) if coin_profiles else ""
        advisory_text = (
            f"\n**[⚠️ 특성 분석가 조언 — 반드시 반영]**\n{coin_profile_advisory}\n"
            if coin_profile_advisory else ""
        )
        specialist_context = self._build_specialist_context(market_condition, allocation)

        # clamp 범위
        has_clamp = eval_stats and "tp_clamp_min" in eval_stats
        if has_clamp:
            tp_min = eval_stats.get("tp_clamp_min", 3.5)
            tp_max = eval_stats.get("tp_clamp_max", 6.0)
            tp_guide = (
                f"- 포트폴리오 익절(take_profit_pct): 전략 최적화 범위 "
                f"**+{tp_min:.1f}%~+{tp_max:.1f}%** 내에서 설정 (필수)\n"
                f"  ※ 분할 익절(+1.5%/+3%)이 자동 작동하므로 trailing 진입선만 설정"
            )
        else:
            tp_min, tp_max = 3.5, 6.0
            tp_guide = (
                "- 포트폴리오 익절(take_profit_pct): 3.5%~6.0% 범위에서 설정\n"
                "  ※ 분할 익절(+1.5%/+3%)이 자동 작동하므로 trailing 진입선만 설정"
            )

        # 후보 심볼 목록 (검증용)
        valid_symbols = {s.symbol for s in snapshots}

        task_prompt = f"""전략: 가용 자금을 5개 코인에 균등 분배(20%씩)하여 포트폴리오를 구성합니다.
포트폴리오 종합 수익률이 목표이며, 5개 코인이 동시에 매수·매도됩니다.

{specialist_context}

아래는 빗썸 거래소 상위 코인의 실시간 시장 데이터입니다.

{market_text}
{history_text}
{advisory_text}
{profiles_text}

**포트폴리오 구성 원칙 (5개 코인 선정)**
1. **분산 효과** — 서로 다른 모멘텀·변동성 특성을 가진 코인을 선택하세요. 유사한 코인을 중복 선정하지 마세요.
2. **변동폭 현실성** — 변동폭이 너무 작은 코인(변동폭 < 2%)은 제외
3. **상승 모멘텀** — 현재가위치가 40~80% 구간이고, 모멘텀이 양수인 코인 우선
4. **거래대금 충분** — 최소 50억원/24h 이상
5. **하락 코인 제외** — 24h 변동률이 -2% 이하이거나 현재가위치가 15% 이하인 코인 제외
6. **최근 포트폴리오 종목 회피** — 위 과거 거래 목록의 코인은 가급적 제외

**포트폴리오 손익절 설정 — 분할 익절·손절 시스템 자동 작동**
시스템이 자동으로 단계별 매매를 수행합니다 (당신이 결정할 필요 없음):
  · +1.5% → 자동 30% 매도 (1차 분할 익절)
  · +3.0% → 자동 30% 매도 (2차 분할 익절, 총 60%)
  · take_profit_pct 도달 → 잔여 40% 트레일링 익절
  · -1.0% → 자동 분할 손절 (AI 평가 비율, 30~50%)
  · -1.5% → 자동 잔여 전량 매도 (최종 손절)
  · 진입 5분 내 -3% 또는 30분 내 -5% → 즉시 전량 매도 (빠른 폭락 보호)

{tp_guide}
- 포트폴리오 손절(stop_loss_pct): **최대 -2.0%** (절대 초과 불가)
- 수수료(매수+매도 약 0.4% × 5코인) 감안 후에도 순이익이 발생해야 함

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "coins": [
    {{"symbol": "코인심볼", "confidence": 확신도(0.0~1.0), "reason": "선정 이유 (한국어, 50자 이내)"}},
    ... (정확히 5개)
  ],
  "take_profit_pct": 포트폴리오익절퍼센트(숫자),
  "stop_loss_pct": 포트폴리오손절퍼센트(음수숫자, 최대-2.0),
  "portfolio_reason": "포트폴리오 구성 이유 (한국어, 100자 이내)"
}}"""

        logger.info("[BuyStrategist] 포트폴리오 구성 분석 중...")
        raw = self._call_llm(task_prompt, max_tokens=1024)
        logger.info(f"[BuyStrategist] LLM 응답: {raw}")

        # ── JSON 파싱 (실패해도 fallback 자동 보충이 반드시 실행되도록 분리) ──
        try:
            data = self._parse_json(raw)
        except (ValueError, KeyError) as e:
            logger.warning(
                f"[BuyStrategist] JSON 파싱 실패 — 자동 보충으로 전환: {e} "
                f"| 원문(앞100자): {raw[:100]!r}"
            )
            data = {}

        coins_raw = data.get("coins", [])
        take_profit_pct = float(data.get("take_profit_pct", 5.0))
        stop_loss_pct = float(data.get("stop_loss_pct", -2.0))
        portfolio_reason = data.get("portfolio_reason", "AI 파싱 실패 — 자동 보충")

        # ── 코인 파싱 + 검증 ──
        coins: list[PortfolioCoinPick] = []
        seen_symbols: set[str] = set()
        for c in coins_raw:
            sym = c.get("symbol", "").upper() if isinstance(c, dict) else ""
            if not sym or sym in seen_symbols or sym not in valid_symbols:
                continue
            seen_symbols.add(sym)
            coins.append(PortfolioCoinPick(
                symbol=sym,
                confidence=float(c.get("confidence", 0.5)),
                reason=c.get("reason", ""),
            ))
            if len(coins) >= _PORTFOLIO_SIZE:
                break

        # ── 부족분 자동 보충 1단계: 스코어 상위 후보 ──
        if len(coins) < _PORTFOLIO_SIZE and coin_scores:
            sorted_scores = sorted(coin_scores, key=lambda s: s.total_score, reverse=True)
            for sc in sorted_scores:
                if sc.symbol not in seen_symbols and sc.symbol in valid_symbols:
                    seen_symbols.add(sc.symbol)
                    coins.append(PortfolioCoinPick(
                        symbol=sc.symbol,
                        confidence=0.3,
                        reason="AI 미선정 — 스코어 기반 자동 보충",
                    ))
                    logger.warning(f"[BuyStrategist 보충] {sc.symbol} (스코어={sc.total_score:.1f})")
                    if len(coins) >= _PORTFOLIO_SIZE:
                        break

        # ── 부족분 자동 보충 2단계: 스냅샷 전체 후보 ──
        if len(coins) < _PORTFOLIO_SIZE:
            for s in snapshots:
                if s.symbol not in seen_symbols:
                    seen_symbols.add(s.symbol)
                    coins.append(PortfolioCoinPick(
                        symbol=s.symbol,
                        confidence=0.2,
                        reason="AI 미선정 — 후보 목록 자동 보충",
                    ))
                    logger.warning(f"[BuyStrategist 스냅샷 보충] {s.symbol}")
                    if len(coins) >= _PORTFOLIO_SIZE:
                        break

        if len(coins) < 3:
            logger.warning(
                f"[BuyStrategist] 유효한 코인 부족({len(coins)}개) — "
                f"스냅샷={len(snapshots)}개, 후보 심볼={len(valid_symbols)}개 "
                f"→ 이번 사이클 매수 스킵"
            )
            return {"portfolio_decision": None}

        # ── TP/SL clamp ──
        take_profit_pct = max(tp_min, min(tp_max, take_profit_pct))
        if stop_loss_pct > 0:
            stop_loss_pct = -abs(stop_loss_pct)
        stop_loss_pct = max(-2.0, min(-0.5, stop_loss_pct))

        decision = PortfolioDecision(
            coins=coins,
            take_profit_pct=round(take_profit_pct, 2),
            stop_loss_pct=round(stop_loss_pct, 2),
            portfolio_reason=portfolio_reason,
            confidence=sum(c.confidence for c in coins) / len(coins),
            llm_provider=self._llm.provider_name,
        )

        symbols_str = ", ".join(c.symbol for c in coins)
        logger.info(
            f"[BuyStrategist] 포트폴리오 구성: [{symbols_str}] / "
            f"TP=+{decision.take_profit_pct}% / SL={decision.stop_loss_pct}% / "
            f"코인 수={len(coins)}"
        )
        return {"portfolio_decision": decision}

    # ---------------------------------------------------------------- #
    #  유틸리티 메서드                                                     #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _snapshots_to_text(
        snapshots: list[CoinSnapshot],
        scores: list[CoinScore] | None = None,
    ) -> str:
        """스냅샷을 AI 프롬프트 텍스트로 변환"""
        score_map = {sc.symbol: sc for sc in (scores or [])}
        lines = []
        for s in snapshots:
            vol_pct = (
                (s.high_price - s.low_price) / s.low_price * 100
                if s.low_price > 0 else 0
            )
            price_range = s.high_price - s.low_price
            pos_pct = (
                (s.current_price - s.low_price) / price_range * 100
                if price_range > 0 else 50
            )
            line = (
                f"- {s.symbol}: "
                f"현재가={s.current_price:,.0f}원, "
                f"24h변동={s.change_pct_24h:+.2f}%, "
                f"거래대금={s.volume_krw_24h / 1e8:.0f}억원, "
                f"변동폭={vol_pct:.1f}%"
            )
            sc = score_map.get(s.symbol)
            if sc and sc.technical_summary:
                line += f" | {sc.technical_summary}"
                if sc.derivatives_summary:
                    line += f" | {sc.derivatives_summary}"
            elif sc:
                line += f" (스코어={sc.total_score:.1f})"
                if sc.derivatives_summary:
                    line += f" | {sc.derivatives_summary}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _eval_stats_to_text(stats: dict) -> str:
        """평가 통계를 프롬프트용 텍스트로 변환"""
        if not stats or stats.get("count", 0) == 0:
            return ""
        lines = [
            "\n**[과거 포트폴리오 성과 — 이 데이터를 기반으로 전략을 결정하세요]**",
            f"- 최근 {stats['count']}건 포트폴리오: 승률 {stats['win_rate']:.0%} "
            f"(익절 {stats['win_count']}건, 손절 {stats['loss_count']}건)",
            f"- 평균 실현 수익률: {stats['avg_pnl_pct']:+.2f}%",
            f"- 평균 보유 시간: {stats['avg_hold_minutes']:.0f}분",
            f"- 평균 익절 설정: +{stats['avg_tp_set']:.1f}%, 평균 손절 설정: {stats['avg_sl_set']:.1f}%",
        ]

        if stats.get("tp_direction"):
            tp_vals = stats.get("tp_trend", [])
            trend_str = "→".join(f"{v:.1f}" for v in tp_vals)
            lines.append(f"- 익절 제안 추세: {stats['tp_direction']} ({trend_str}%)")

        recent = stats.get("recent_trades", [])
        if recent:
            lines.append("- 최근 포트폴리오 결과 (종목 회피 참고):")
            for t in recent:
                exit_kr = "익절" if t["exit_type"] == "take_profit" else "손절"
                lines.append(
                    f"  * {t['portfolio_name']}: {exit_kr} {t['pnl_pct']:+.2f}%, "
                    f"보유 {t['held_minutes']:.0f}분"
                )

        if stats.get("recent_lessons"):
            lines.append("- 최근 교훈:")
            for lesson in stats["recent_lessons"]:
                lines.append(f"  * {lesson}")
        return "\n".join(lines)

    @staticmethod
    def _profiles_to_text(profiles: dict[str, str]) -> str:
        """코인 프로파일을 프롬프트 삽입용 텍스트로 변환"""
        if not profiles:
            return ""
        lines = [
            "\n**[특성 분석가 프로파일 — 과거 보유 이력 있음, 매수 전 반드시 참고]**",
        ]
        for symbol, profile in profiles.items():
            lines.append(f"\n### {symbol}")
            lines.append(profile.strip())
        return "\n".join(lines)

    @staticmethod
    def _build_specialist_context(
        market_condition: MarketCondition | None,
        allocation: AllocationDecision | None,
    ) -> str:
        """시장 분석가 + 자산 운용가 컨텍스트"""
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
