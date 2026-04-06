"""전략 최적화 Agent — 수익 극대화를 위한 즉각적 파라미터 결정

철학 (2단계 손절 전략):
- 1차 손절(SL1): -1.0%~-3.0% — 도달 시 50% 매도, 나머지 반등 대기
- 2차 손절(SL2): -1.5%~-4.5% — 도달 시 나머지 전량 매도
- 실효 최대 손실 = SL1×50% + SL2×50% ≈ -2.25% (단순 손절 대비 절반 수준)
- 익절: 크게 가져가기 (2.0%~6.0%) — 손실 분산으로 더 큰 목표 가능
- 즉각 반영: 매매 완료 즉시 분석 → 다음 파라미터 즉시 갱신

StrategyOptimizer가 관리하는 클램프는 2차 손절(SL2) 기준입니다.
1차 손절(SL1)은 AI Agent가 SL2 대비 0.3~1.5% 위에서 결정합니다.
"""
import json
import logging
from dataclasses import dataclass

from core.llm_provider import BaseLLMProvider, get_llm_provider

logger = logging.getLogger(__name__)


@dataclass
class StrategyParams:
    """전략 파라미터 묶음 — StrategyOptimizer가 결정 (2단계 손절 전략)"""
    target_tp: float = 3.5       # 권고 익절% (AI가 이 값 근처로 결정)
    target_sl: float = -2.5      # 권고 2차 손절% (음수)
    tp_clamp_min: float = 2.0    # 익절 허용 최솟값
    tp_clamp_max: float = 6.0    # 익절 허용 최댓값
    sl_clamp_min: float = -4.5   # 2차 손절 허용 최솟값
    sl_clamp_max: float = -1.5   # 2차 손절 허용 최댓값
    rationale: str = "기본 파라미터 (2단계 손절: SL1≈-2%, SL2≈-2.5%, 익절 크게)"
    confidence: float = 0.5


_DEFAULT_PARAMS = StrategyParams()


class StrategyOptimizer:
    """수익 극대화 전담 전략 Agent

    TradingAgent가 *어떤 코인*을 살지 결정하면,
    StrategyOptimizer가 *익절/손절을 얼마로* 설정할지 결정합니다.

    매매 완료 직후 optimize()를 호출해 파라미터를 갱신하면
    다음 select_coin() 호출 시 즉시 반영됩니다.
    """

    def __init__(self, llm: BaseLLMProvider | None = None):
        self._llm = llm or get_llm_provider()
        self._params: StrategyParams = _DEFAULT_PARAMS

    # ---------------------------------------------------------------- #
    #  파라미터 조회                                                      #
    # ---------------------------------------------------------------- #
    def get_params(self) -> StrategyParams:
        """현재 최적화된 파라미터 반환 (없으면 기본값)"""
        return self._params

    # ---------------------------------------------------------------- #
    #  최적화 실행                                                        #
    # ---------------------------------------------------------------- #
    def optimize(self, eval_stats: dict) -> StrategyParams:
        """최근 성과 기반 즉각 파라미터 최적화

        1단계: 휴리스틱으로 즉각 결정 (LLM 없이, 빠름)
        2단계: 데이터 충분 시 LLM 심층 분석으로 덮어쓰기 (정확)
        """
        if not eval_stats or eval_stats.get("count", 0) == 0:
            self._params = _DEFAULT_PARAMS
            logger.info("[StrategyOptimizer] 과거 데이터 없음 — 기본 파라미터 사용")
            return self._params

        # 1단계: 휴리스틱 즉각 적용
        quick = self._heuristic_optimize(eval_stats)
        self._params = quick
        logger.info(
            f"[StrategyOptimizer] 휴리스틱 즉각 적용 | "
            f"익절 {quick.tp_clamp_min}~{quick.tp_clamp_max}% "
            f"/ 손절 {quick.sl_clamp_min}~{quick.sl_clamp_max}% "
            f"| {quick.rationale}"
        )

        # 2단계: 3건 이상이면 LLM 심층 분석
        if eval_stats.get("count", 0) >= 3:
            try:
                llm_params = self._llm_optimize(eval_stats)
                self._params = llm_params
                logger.info(
                    f"[StrategyOptimizer] LLM 최적화 완료 | "
                    f"익절 목표={llm_params.target_tp}% ({llm_params.tp_clamp_min}~{llm_params.tp_clamp_max}%) "
                    f"/ 손절 목표={llm_params.target_sl}% ({llm_params.sl_clamp_min}~{llm_params.sl_clamp_max}%) "
                    f"| {llm_params.rationale}"
                )
            except Exception as e:
                logger.warning(f"[StrategyOptimizer] LLM 분석 실패 — 휴리스틱 유지: {e}")

        return self._params

    # ---------------------------------------------------------------- #
    #  내부: 휴리스틱 분석                                                #
    # ---------------------------------------------------------------- #
    def _heuristic_optimize(self, eval_stats: dict) -> StrategyParams:
        """LLM 없이 규칙 기반으로 즉각 파라미터 결정

        repository의 suggested 값(과거 AI 제안 가중평균)을 기반으로 하되,
        연속 손절 등 특수 상황에서만 규칙으로 오버라이드합니다.
        """
        recent_trades = eval_stats.get("recent_trades", [])
        win_rate = eval_stats.get("win_rate", 0.5)
        avg_hold = eval_stats.get("avg_hold_minutes", 60.0)
        avg_pnl = eval_stats.get("avg_pnl_pct", 0.0)

        # repository의 suggested 기반 clamp (AI 제안의 가중평균) — 2단계 손절 전략 기준
        repo_tp_min = eval_stats.get("tp_clamp_min", 2.0)
        repo_tp_max = eval_stats.get("tp_clamp_max", 6.0)
        repo_sl_min = eval_stats.get("sl_clamp_min", -4.5)   # 2차 손절 기준
        repo_sl_max = eval_stats.get("sl_clamp_max", -1.5)
        suggested_tp = eval_stats.get("avg_suggested_tp", 3.5)
        suggested_sl = eval_stats.get("avg_suggested_sl", -2.5)

        # 최근 연속 손절 카운트 + 평균 손실 크기
        consecutive_losses = 0
        loss_pnl_sum = 0.0
        for t in recent_trades:
            if t.get("exit_type") == "stop_loss":
                consecutive_losses += 1
                loss_pnl_sum += t.get("pnl_pct", 0)
            else:
                break
        avg_loss_size = abs(loss_pnl_sum / consecutive_losses) if consecutive_losses > 0 else 0

        # ── 손절 파라미터 결정 ──────────────────────────────
        # 기본: repository의 suggested 기반 (AI 제안 가중평균 계승)
        target_sl = round(suggested_sl, 1)
        sl_min, sl_max = repo_sl_min, repo_sl_max

        if consecutive_losses >= 3:
            if avg_loss_size > 2.0:
                # 손실 크기가 큼 → SL2를 약간 넓히고 익절도 조정
                target_sl = max(target_sl - 0.3, -4.5)
                rationale = f"연속 손절 {consecutive_losses}건(평균 -{avg_loss_size:.1f}%) — SL2 소폭 완화"
            else:
                # 손실 크기가 작음 → SL1이 너무 좁은 것, SL2 범위 유지
                target_sl = max(target_sl - 0.5, -4.0)
                sl_min = max(sl_min - 0.5, -5.0)
                rationale = f"연속 손절 {consecutive_losses}건(소폭) → SL2 완화"
        elif consecutive_losses == 2:
            target_sl = max(target_sl - 0.3, -4.0)
            rationale = f"연속 손절 2건 → SL2 소폭 완화"
        elif win_rate < 0.35:
            target_sl = max(target_sl - 0.5, -4.0)
            rationale = f"낮은 승률 {win_rate:.0%} → SL2 완화"
        else:
            rationale = f"suggested 기반 유지 (승률 {win_rate:.0%})"

        # ── 익절 파라미터 결정 ──────────────────────────────
        # 기본: repository의 suggested 기반 (2단계 손절로 리스크 감소 → 더 큰 익절 목표)
        target_tp = round(suggested_tp, 1)
        tp_min, tp_max = repo_tp_min, repo_tp_max

        if consecutive_losses >= 3 and avg_loss_size > 2.0:
            # 시장 악화 시 익절을 소폭 낮춰 빠른 수익 실현
            target_tp = max(2.0, target_tp - 0.5)
            tp_max = min(tp_max, 5.0)
        elif avg_hold > 240:
            target_tp = max(2.0, target_tp - 1.0)
            tp_max = min(tp_max, 4.5)
        elif avg_hold > 120:
            target_tp = max(2.0, target_tp - 0.5)
        elif win_rate >= 0.6 and avg_pnl > 1.0:
            # 잘 되고 있으면 익절 목표 유지 또는 소폭 상향
            target_tp = min(6.0, target_tp + 0.3)
            tp_max = min(7.0, tp_max + 0.5)

        return StrategyParams(
            target_tp=max(2.0, min(7.0, round(target_tp, 1))),
            target_sl=max(-5.0, min(-1.5, round(target_sl, 1))),
            tp_clamp_min=max(1.5, round(tp_min, 1)),
            tp_clamp_max=min(7.0, round(tp_max, 1)),
            sl_clamp_min=max(-5.5, round(sl_min, 1)),
            sl_clamp_max=min(-1.0, round(sl_max, 1)),
            rationale=rationale,
            confidence=0.7,
        )

    # ---------------------------------------------------------------- #
    #  내부: LLM 심층 분석                                               #
    # ---------------------------------------------------------------- #
    def _llm_optimize(self, eval_stats: dict) -> StrategyParams:
        """LLM을 통한 심층 전략 최적화"""
        recent = eval_stats.get("recent_trades", [])
        recent_lines = []
        for t in recent:
            exit_kr = "익절" if t["exit_type"] == "take_profit" else "손절"
            recent_lines.append(
                f"  - {t['symbol']}: {exit_kr} {t['pnl_pct']:+.2f}%, "
                f"보유 {t['held_minutes']:.0f}분"
            )
        recent_text = "\n".join(recent_lines) if recent_lines else "  (없음)"

        lessons = eval_stats.get("recent_lessons", [])
        lessons_text = "\n".join(f"  - {l}" for l in lessons) if lessons else "  (없음)"

        prompt = f"""당신은 단기 변동성 매매 전략 최적화 전문가입니다.

**전략 철학 — 2단계 손절 (반드시 준수):**
- 1차 손절(SL1): -1.0%~-3.0% → 도달 시 50% 매도, 나머지 반등 대기
- 2차 손절(SL2): -1.5%~-4.5% → 도달 시 나머지 전량 매도 (SL1보다 0.3~1.5% 더 낮음)
- 실효 최대 손실 ≈ SL1×50% + SL2×50% (단순 손절보다 훨씬 유리)
- 익절: 더 큰 목표 (2.0%~6.0%) — 손실 분산으로 R:R 대폭 개선
- 이 파라미터는 2차 손절(SL2) 범위를 결정합니다

**최근 {eval_stats['count']}건 매매 성과:**
- 승률: {eval_stats['win_rate']:.0%} (익절 {eval_stats['win_count']}건, 손절 {eval_stats['loss_count']}건)
- 평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%
- 평균 보유 시간: {eval_stats['avg_hold_minutes']:.0f}분
- 평균 익절 설정값: +{eval_stats['avg_tp_set']:.1f}%
- 평균 2차 손절 설정값: {eval_stats['avg_sl_set']:.1f}%

**최근 거래 내역:**
{recent_text}

**최근 교훈:**
{lessons_text}

**지시:**
위 데이터를 분석해 다음 매매에 즉시 적용할 파라미터를 결정하세요.
- 연속 손절이 있으면 SL2 범위를 소폭 완화하세요 (단, -4.5% 이상 넓히지 마세요)
- 익절은 2단계 손절 덕분에 더 크게 가져갈 수 있음 — 승률보다 기대수익 중심
- 보유 시간이 길수록 익절을 낮추세요 (기회비용 방지)

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{{
  "target_tp": 권고익절%(숫자, 2.0~6.0 범위),
  "target_sl": 권고2차손절%(음수숫자, -1.5~-4.5 범위),
  "tp_clamp_min": 익절허용최솟값(숫자, 1.5~3.5),
  "tp_clamp_max": 익절허용최댓값(숫자, 3.5~7.0),
  "sl_clamp_min": 2차손절허용최솟값(음수, -2.5~-5.5),
  "sl_clamp_max": 2차손절허용최댓값(음수, -1.0~-2.5),
  "rationale": "결정 이유 (한국어, 80자 이내)",
  "confidence": 확신도(0.0~1.0)
}}"""

        raw = self._llm.chat(prompt, max_tokens=400)
        logger.debug(f"[StrategyOptimizer] LLM 원문: {raw}")

        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(clean)

        target_tp = max(2.0, min(7.0, float(data["target_tp"])))
        target_sl = float(data["target_sl"])
        if target_sl > 0:
            target_sl = -abs(target_sl)
        target_sl = max(-5.0, min(-1.5, target_sl))

        tp_min = max(1.5, min(4.0, float(data["tp_clamp_min"])))
        tp_max = max(tp_min + 1.0, min(7.5, float(data["tp_clamp_max"])))

        sl_min = float(data["sl_clamp_min"])
        sl_max = float(data["sl_clamp_max"])
        if sl_min > 0:
            sl_min = -abs(sl_min)
        if sl_max > 0:
            sl_max = -abs(sl_max)
        sl_min = max(-6.0, min(-2.0, sl_min))
        sl_max = max(sl_min + 0.5, min(-1.0, sl_max))

        return StrategyParams(
            target_tp=round(target_tp, 1),
            target_sl=round(target_sl, 1),
            tp_clamp_min=round(tp_min, 1),
            tp_clamp_max=round(tp_max, 1),
            sl_clamp_min=round(sl_min, 1),
            sl_clamp_max=round(sl_max, 1),
            rationale=str(data.get("rationale", "")),
            confidence=float(data.get("confidence", 0.7)),
        )
