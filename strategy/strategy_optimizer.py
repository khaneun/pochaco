"""전략 최적화 Agent — 수익 극대화를 위한 즉각적 파라미터 결정

철학:
- 익절: 낮고 빠르게 (1.0%~3.5%) — 작은 수익을 확실하게 확보
- 손절: 넓고 느리게 (2.0%~6.0%) — 반등 기회를 충분히 대기
- 즉각 반영: 매매 완료 즉시 분석 → 다음 파라미터 즉시 갱신

기존 TradingAgent가 코인 선정을 담당하고,
StrategyOptimizer가 익절/손절 파라미터 범위를 전담합니다.
"""
import json
import logging
from dataclasses import dataclass

from core.llm_provider import BaseLLMProvider, get_llm_provider

logger = logging.getLogger(__name__)


@dataclass
class StrategyParams:
    """전략 파라미터 묶음 — StrategyOptimizer가 결정"""
    target_tp: float = 2.0       # 권고 익절% (AI가 이 값 근처로 결정)
    target_sl: float = -3.0      # 권고 손절% (음수)
    tp_clamp_min: float = 1.0    # 익절 허용 최솟값
    tp_clamp_max: float = 3.5    # 익절 허용 최댓값
    sl_clamp_min: float = -6.0   # 손절 허용 최솟값 (더 넓음)
    sl_clamp_max: float = -2.0   # 손절 허용 최댓값 (덜 좁음)
    rationale: str = "기본 파라미터 (낮은 익절, 넓은 손절)"
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

        # repository의 suggested 기반 clamp (AI 제안의 가중평균)
        repo_tp_min = eval_stats.get("tp_clamp_min", 1.0)
        repo_tp_max = eval_stats.get("tp_clamp_max", 3.5)
        repo_sl_min = eval_stats.get("sl_clamp_min", -6.0)
        repo_sl_max = eval_stats.get("sl_clamp_max", -2.0)
        suggested_tp = eval_stats.get("avg_suggested_tp", 2.0)
        suggested_sl = eval_stats.get("avg_suggested_sl", -3.0)

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
            if avg_loss_size > 3.0:
                # 손실 크기가 큼 → 시장 악화, 손절 넓히기보다 코인 선정 개선 필요
                # 손절은 소폭만 확대, 대신 익절을 더 낮춰 빠른 탈출 유도
                target_sl = max(target_sl - 0.5, -6.0)
                rationale = f"연속 손절 {consecutive_losses}건(평균 -{avg_loss_size:.1f}%) — 시장 악화, 빠른 탈출 전략"
            else:
                # 손실 크기가 작음 → 손절이 좁아서 찍힌 것, 넓히기
                target_sl = max(target_sl - 1.5, -6.0)
                sl_min = max(sl_min - 1.0, -7.0)
                rationale = f"연속 손절 {consecutive_losses}건(소폭) → 손절 확대"
        elif consecutive_losses == 2:
            target_sl = max(target_sl - 0.5, -5.5)
            rationale = f"연속 손절 2건 → 손절 소폭 완화"
        elif win_rate < 0.35:
            target_sl = max(target_sl - 1.0, -5.5)
            rationale = f"낮은 승률 {win_rate:.0%} → 손절 완화"
        else:
            rationale = f"suggested 기반 유지 (승률 {win_rate:.0%})"

        # ── 익절 파라미터 결정 ──────────────────────────────
        # 기본: repository의 suggested 기반
        target_tp = round(suggested_tp, 1)
        tp_min, tp_max = repo_tp_min, repo_tp_max

        if consecutive_losses >= 3 and avg_loss_size > 3.0:
            # 시장 악화 시 익절을 더 낮춰 빠른 수익 실현
            target_tp = max(1.0, target_tp - 0.5)
            tp_max = min(tp_max, 2.5)
        elif avg_hold > 180:
            target_tp = max(1.0, target_tp - 0.5)
            tp_max = min(tp_max, 2.5)
        elif avg_hold > 90:
            target_tp = max(1.0, target_tp - 0.3)
        elif win_rate >= 0.6 and avg_pnl > 0.5:
            # 잘 되고 있으면 현재 전략 유지
            pass

        return StrategyParams(
            target_tp=max(0.8, min(4.0, round(target_tp, 1))),
            target_sl=max(-7.0, min(-1.5, round(target_sl, 1))),
            tp_clamp_min=max(0.8, round(tp_min, 1)),
            tp_clamp_max=min(4.5, round(tp_max, 1)),
            sl_clamp_min=max(-8.0, round(sl_min, 1)),
            sl_clamp_max=min(-1.5, round(sl_max, 1)),
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

**전략 철학 (반드시 준수):**
- 익절: 낮고 빠르게 — 목표 1.0%~3.5% (작은 수익이라도 확실히 확보)
- 손절: 넓고 느리게 — 목표 -2.0%~-6.0% (반등 기회를 충분히 대기)
- 문제: 익절이 높으면 도달 못 하고 결국 손절만 반복 → 야금야금 자산 감소
- 해결: 익절 낮추기 + 손절 넓히기 = 승률 높이기

**최근 {eval_stats['count']}건 매매 성과:**
- 승률: {eval_stats['win_rate']:.0%} (익절 {eval_stats['win_count']}건, 손절 {eval_stats['loss_count']}건)
- 평균 수익률: {eval_stats['avg_pnl_pct']:+.2f}%
- 평균 보유 시간: {eval_stats['avg_hold_minutes']:.0f}분
- 평균 익절 설정값: +{eval_stats['avg_tp_set']:.1f}%
- 평균 손절 설정값: {eval_stats['avg_sl_set']:.1f}%

**최근 거래 내역:**
{recent_text}

**최근 교훈:**
{lessons_text}

**지시:**
위 데이터를 분석해 다음 매매에 즉시 적용할 파라미터를 결정하세요.
- 연속 손절이 있으면 손절 폭을 과감히 넓히세요
- 익절은 무조건 낮게 — 달성 가능한 수준으로
- 보유 시간이 길수록 익절을 더 낮추세요 (기회비용 방지)

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이):
{{
  "target_tp": 권고익절%(숫자, 1.0~3.5 범위),
  "target_sl": 권고손절%(음수숫자, -2.0~-6.0 범위),
  "tp_clamp_min": 익절허용최솟값(숫자, 0.8~2.5),
  "tp_clamp_max": 익절허용최댓값(숫자, 2.0~4.5),
  "sl_clamp_min": 손절허용최솟값(음수, -4.0~-8.0),
  "sl_clamp_max": 손절허용최댓값(음수, -1.5~-3.0),
  "rationale": "결정 이유 (한국어, 80자 이내)",
  "confidence": 확신도(0.0~1.0)
}}"""

        raw = self._llm.chat(prompt, max_tokens=400)
        logger.debug(f"[StrategyOptimizer] LLM 원문: {raw}")

        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(clean)

        target_tp = max(0.8, min(4.5, float(data["target_tp"])))
        target_sl = float(data["target_sl"])
        if target_sl > 0:
            target_sl = -abs(target_sl)
        target_sl = max(-8.0, min(-1.5, target_sl))

        tp_min = max(0.8, min(3.0, float(data["tp_clamp_min"])))
        tp_max = max(tp_min + 0.5, min(5.0, float(data["tp_clamp_max"])))

        sl_min = float(data["sl_clamp_min"])
        sl_max = float(data["sl_clamp_max"])
        if sl_min > 0:
            sl_min = -abs(sl_min)
        if sl_max > 0:
            sl_max = -abs(sl_max)
        sl_min = max(-9.0, min(-2.0, sl_min))
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
