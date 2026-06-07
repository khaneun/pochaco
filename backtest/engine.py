"""청산 로직 시뮬레이터 (next_plan.md 1.A).

trading_engine.py의 스마트 매도 상태 머신을 단일 코인 기준으로 재현한다.
운영의 포트폴리오(8코인 묶음) 동시성은 무시하고 코인 1개를 독립 시뮬하므로
누적 수익률·MDD는 참고용, 시나리오 간 상대 비교가 본질이다.

체결 모델 (next_plan.md):
  - 5분봉 [low, high] 범위로 트리거 판정 (종가 단일 체결 ❌)
  - 한 봉에서 TP·SL 동시 트리거 시 SL 우선 (보수적)
  - 트레일링 peak는 high 기준, 폭락/손절 하방은 low 기준
  - 트리거 임계가에서 체결 (슬리피지 0), 수수료 양방향 차감
"""
from dataclasses import dataclass, field

# ── 거래소별 편도 수수료율 ────────────────────────────────────────────── #
FEE_RATE = {
    "bithumb": 0.0004,   # 0.04%
    "upbit":   0.0005,   # 0.05%
}


@dataclass
class ExitParams:
    """v4.4 청산 파라미터 (trading_engine.py 상수와 1:1 대응)."""
    tp_pct: float                      # 트레일링 진입선 (portfolio.take_profit_pct, 보통 3~4.5)
    fee_rate: float = 0.0004           # 편도 수수료율
    max_hold_min: float = 360.0        # 6시간 강제 청산
    early_check_min: float = 180.0     # 3시간 무수익 점검
    early_peak_threshold: float = 1.0  # peak < +1% 면 조기청산
    rapid_5min_pct: float = -3.0       # 5분 내 폭락 손절
    rapid_30min_pct: float = -5.0      # 30분 내 폭락 손절
    final_sl_pct: float = -1.5         # 단일 최종 손절
    tier1_tp_pct: float = 1.0          # 1차 분할 익절 진입
    tier1_ratio: float = 0.50
    tier2_tp_pct: float = 2.5          # 2차 분할 익절 진입
    tier2_ratio: float = 0.30
    trail_timeout_sec: float = 1800.0  # 30분


@dataclass
class ExitResult:
    exit_ts: int                       # 청산 완료 봉 ts_5m
    exit_price: float                  # 최종(잔여분) 청산가
    realized_pnl_pct: float            # 수수료 차감 가중 실현 손익률(%)
    reason: str
    holding_min: float
    peak_pnl_pct: float
    closed: bool                       # 데이터 내에서 청산 완료 여부
    legs: list[dict] = field(default_factory=list)  # 부분 매도 내역


def _trail_offset(peak: float) -> float:
    """trading_engine._calc_trail_offset 재현."""
    if peak >= 15.0:
        return 2.5
    if peak >= 10.0:
        return 1.8
    if peak >= 7.0:
        return 1.2
    if peak >= 5.0:
        return 0.8
    return 0.3


def simulate_exit(buy_price: float, candles: list,
                  params: ExitParams) -> ExitResult:
    """단일 코인 진입가 + 5분봉 시계열로 청산을 시뮬한다.

    Args:
        buy_price: 진입가
        candles: [(ts_5m, open, high, low, close, volume), ...] 오래된→최신
                 candles[0]은 진입 직후 첫 봉
        params: 청산 파라미터

    Returns:
        ExitResult — 미청산이면 closed=False, 마지막 봉 종가로 평가
    """
    if buy_price <= 0 or not candles:
        return ExitResult(0, buy_price, 0.0, "데이터없음", 0.0, 0.0, False)

    fee2 = params.fee_rate * 2  # 양방향
    entry_ts = candles[0][0]

    remaining = 1.0          # 잔여 비율
    realized = 0.0           # 수수료 차감 가중 손익률(%) 누적
    tier1_sold = False
    tier2_sold = False
    phase = "MONITORING"
    peak = 0.0
    trailing_since = 0
    offset = 0.3
    legs: list[dict] = []

    def pnl_at(price: float) -> float:
        return (price / buy_price - 1.0) * 100.0

    def book(ratio: float, price: float, reason: str) -> None:
        """부분/전량 매도 기록 — 수수료 차감 손익률을 가중 누적."""
        nonlocal remaining, realized
        gross = pnl_at(price)
        net = gross - fee2 * 100.0  # 수수료 %p 차감
        realized += net * ratio
        legs.append({"ratio": round(ratio, 4), "price": round(price, 6),
                     "pnl_pct": round(net, 3), "reason": reason})
        remaining -= ratio

    last = candles[-1]
    for c in candles:
        ts, o, high, low, close, _vol = c
        holding = (ts - entry_ts) / 60.0
        pnl_high = pnl_at(high)
        pnl_low = pnl_at(low)
        pnl_close = pnl_at(close)

        # ── 시간 기반 강제 청산 ──
        if holding >= params.max_hold_min:
            book(remaining, close, "시간초과")
            return ExitResult(ts, close, realized, "시간초과", holding, peak, True, legs)
        if (holding >= params.early_check_min
                and peak < params.early_peak_threshold
                and phase != "TRAILING"):
            book(remaining, close, "무수익조기청산")
            return ExitResult(ts, close, realized, "무수익조기청산", holding, peak, True, legs)

        if phase == "MONITORING":
            # 0. 빠른 폭락 (low 기준) — 최우선
            if (holding < 5 and pnl_low <= params.rapid_5min_pct):
                px = buy_price * (1 + params.rapid_5min_pct / 100.0)
                book(remaining, px, "빠른폭락손절")
                return ExitResult(ts, px, realized, "빠른폭락손절", holding, peak, True, legs)
            if (holding < 30 and pnl_low <= params.rapid_30min_pct):
                px = buy_price * (1 + params.rapid_30min_pct / 100.0)
                book(remaining, px, "빠른폭락손절")
                return ExitResult(ts, px, realized, "빠른폭락손절", holding, peak, True, legs)

            # 1. SL 우선 (동시 트리거 시 보수적) — low 기준
            if pnl_low <= params.final_sl_pct:
                px = buy_price * (1 + params.final_sl_pct / 100.0)
                book(remaining, px, "최종손절")
                return ExitResult(ts, px, realized, "최종손절", holding, peak, True, legs)

            # 2. 트레일링 진입 (high 기준)
            if pnl_high >= params.tp_pct:
                phase = "TRAILING"
                peak = pnl_high
                trailing_since = ts
                offset = _trail_offset(peak)
                continue

            # 3. 2차 분할 익절 (+2.5%, 30%)
            if not tier2_sold and pnl_high >= params.tier2_tp_pct:
                px = buy_price * (1 + params.tier2_tp_pct / 100.0)
                book(params.tier2_ratio * 1.0, px, "2차분할익절")
                tier2_sold = True
                tier1_sold = True
                if peak < params.tier2_tp_pct:
                    peak = params.tier2_tp_pct
                continue
            # 4. 1차 분할 익절 (+1.0%, 50%)
            if not tier1_sold and pnl_high >= params.tier1_tp_pct:
                px = buy_price * (1 + params.tier1_tp_pct / 100.0)
                book(params.tier1_ratio, px, "1차분할익절")
                tier1_sold = True
                if peak < params.tier1_tp_pct:
                    peak = params.tier1_tp_pct
                continue
            # peak 갱신 (보유 지속)
            if pnl_high > peak:
                peak = pnl_high

        else:  # TRAILING
            if pnl_high > peak:
                peak = pnl_high
                offset = _trail_offset(peak)
            # 하락폭 — low 기준 최대 하락
            drop_low = peak - pnl_low
            if drop_low >= offset:
                px = buy_price * (1 + (peak - offset) / 100.0)
                book(remaining, px, "트레일링익절")
                return ExitResult(ts, px, realized, "트레일링익절", holding, peak, True, legs)
            if (ts - trailing_since) >= params.trail_timeout_sec:
                book(remaining, close, "트레일링타임아웃")
                return ExitResult(ts, close, realized, "트레일링타임아웃", holding, peak, True, legs)
            if pnl_low < params.tp_pct * 0.2:
                px = buy_price * (1 + (params.tp_pct * 0.2) / 100.0)
                book(remaining, px, "모멘텀상실")
                return ExitResult(ts, px, realized, "모멘텀상실", holding, peak, True, legs)

    # 데이터 끝 — 미청산. 잔여분 마지막 종가 평가 (참고용)
    ts, close = last[0], last[4]
    holding = (ts - entry_ts) / 60.0
    if remaining > 0:
        book(remaining, close, "데이터끝(미청산)")
    return ExitResult(ts, close, realized, "미청산", holding, peak, False, legs)
