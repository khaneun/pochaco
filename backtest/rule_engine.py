"""룰 기반 진입 엔진 (next_plan.md Phase 2 — LLM 대체 진입 룰).

candles.db 5분봉 전 종목에 walk-forward로 진입을 생성하고 청산을 시뮬한다.
LLM 6에이전트 의사결정을 "필터 통과 + 스코어 상위 N" 결정론 룰로 대체(v5 방향).
포트폴리오 동시보유 한도·신규진입 간격·쿨다운·(동적) 블랙리스트를 반영.

시나리오 차이는 RuleConfig로만 표현 — 동일 엔진/지표를 공유해 상대 비교가 본질:
  - rule_baseline: 풀 유니버스 + 변동성 보상 ON + 블랙리스트 OFF (현 v4.4 근사)
  - phase2:        메이저 화이트리스트 + 변동성 보상 OFF + 손실코인 영구 블랙리스트
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from backtest import data_loader
from backtest.engine import ExitParams, ExitResult, FEE_RATE, simulate_exit
from backtest.indicators import Indicators

logger = logging.getLogger(__name__)

# 스테이블코인 — 변동 없음, 항상 제외
_STABLES = {"USDT", "USDC", "DAI", "TUSD", "BUSD", "USDD", "FDUSD"}

# 청산사유 → 재매수 쿨다운(분)  [cooldown.py / CLAUDE.md v4.4 근사]
_COOLDOWN_MIN = {
    "트레일링익절": 360, "1차분할익절": 360, "2차분할익절": 360,
    "트레일링타임아웃": 360, "모멘텀상실": 360,
    "최종손절": 720, "빠른폭락손절": 720,
    "무수익조기청산": 60, "시간초과": 60,
}


@dataclass
class RuleConfig:
    """시나리오별 진입 룰 파라미터."""
    name: str
    whitelist: set[str] | None = None          # None=풀 유니버스
    use_volatility_reward: bool = True          # 변동성 가점 여부
    permanent_blacklist_losses: bool = False    # 손실 누적 코인 영구 차단
    blacklist_loss_threshold: int = 3           # 영구 차단 손실 횟수
    min_volume_krw: float = 30_000_000_000.0    # 거래대금 하한(빗썸 300억 기본)
    tp_pct: float = 3.5                           # 청산 트레일링 진입선(ExitParams)
    min_volatility_pct: float = 2.0              # 변동폭 하한(절대 %) — coin_selector 2차패스 근사
    rsi_overbought: float = 75.0
    max_concurrent: int = 8                      # 동시 보유 한도
    decision_interval_min: int = 60              # 신규진입 결정 간격(분)
    min_lookback_bars: int = 96                  # 최소 8h 누적 후 진입 허용


# ── 메이저 화이트리스트 (Phase 2) ──
# 진단: 메이저=수익(ETH/SOL/BTC/XRP 등), 잡알트=손실. 시총·유동성 상위 ~25종.
MAJOR_WHITELIST = {
    "BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "TRX", "LINK", "AVAX", "DOT",
    "POL", "MATIC", "BCH", "LTC", "ETC", "XLM", "ATOM", "NEAR", "APT", "ARB",
    "OP", "SUI", "UNI", "AAVE", "HBAR", "ONDO", "ENA",
}


@dataclass
class SimEntry:
    """룰 진입 1건 + 청산 결과."""
    symbol: str
    buy_ts: int
    buy_price: float
    sim: ExitResult


def _score(ind: Indicators, i: int, cfg: RuleConfig) -> float:
    """coin_selector 가중치 근사 스코어. use_volatility_reward로 변동성 항 토글."""
    rsi = ind.rsi[i]
    mom = ind.momentum[i]
    vola = ind.volatility_pct[i]
    vkrw = ind.vol_krw_24h[i]

    # RSI 위치(기술 신호 프록시, 0~5) — 40~55 이상적, 과매수 감점
    if 35 <= rsi <= 55:
        rsi_score = 5.0
    elif 55 < rsi <= 65:
        rsi_score = 3.0
    elif 65 < rsi <= 70:
        rsi_score = 1.0
    elif rsi > 70:
        rsi_score = -1.0
    else:                       # rsi < 35 (과매도 반등 기대)
        rsi_score = 3.5
    mom_score = max(mom, 0.0) / 2.0          # 0~5
    v_score = min(vkrw / 2e10, 3.0)          # 거래량 0~3

    total = rsi_score * 0.30 + mom_score * 0.20 + v_score * 0.15
    if cfg.use_volatility_reward:
        total += min(vola / 2.0, 5.0) * 0.15          # 변동성 가점
    else:
        total += (-2.0 if vola > 8.0 else 0.0) * 0.10  # 과대 변동성 감점
    return total


def _passes_filter(ind: Indicators, i: int, cfg: RuleConfig) -> bool:
    """coin_selector 사전 필터 근사."""
    vola = ind.volatility_pct[i]
    vkrw = ind.vol_krw_24h[i]
    rsi = ind.rsi[i]
    ch24 = ind.change_24h[i]
    ch3h = ind.change_3h[i]

    if vkrw < cfg.min_volume_krw:
        return False
    if vola < cfg.min_volatility_pct:
        return False
    if rsi >= cfg.rsi_overbought:
        return False
    # 하락추세 (coin_selector 기준 A/B 근사) — NaN은 비교 시 자동 무시
    if ch24 == ch24 and ch24 < -2.0:
        return False
    if ch3h == ch3h and ch3h < -1.5:
        return False
    return True


def run(exchange: str, cfg: RuleConfig,
        candles_db: str = data_loader._DB_PATH) -> list[SimEntry]:
    """walk-forward 진입 생성 + 청산 시뮬 → SimEntry 리스트."""
    fee = FEE_RATE.get(exchange, 0.0004)
    syms = data_loader.symbols(exchange, candles_db)

    # ── 종목별 지표 사전 계산 ──
    inds: dict[str, Indicators] = {}
    ts_min = ts_max = None
    for s in syms:
        if s in _STABLES:
            continue
        rows = data_loader.load_candles(s, exchange, db_path=candles_db)
        if len(rows) < cfg.min_lookback_bars + 4:
            continue
        ts = [int(r["ts_5m"]) for r in rows]
        o = [float(r["open"]) for r in rows]
        h = [float(r["high"]) for r in rows]
        l = [float(r["low"]) for r in rows]
        c = [float(r["close"]) for r in rows]
        v = [float(r["volume"]) for r in rows]
        inds[s] = Indicators(ts, o, h, l, c, v)
        ts_min = ts[0] if ts_min is None else min(ts_min, ts[0])
        ts_max = ts[-1] if ts_max is None else max(ts_max, ts[-1])

    if not inds:
        return []

    # 종목별 (ts → 인덱스) 매핑
    idx_of: dict[str, dict[int, int]] = {
        s: {int(t): k for k, t in enumerate(ind.ts)} for s, ind in inds.items()
    }

    # ── walk-forward ──
    step = cfg.decision_interval_min * 60
    # 결정 시각은 5분 그리드 정렬
    t0 = (int(ts_min) // 300) * 300
    entries: list[SimEntry] = []
    open_until: list[int] = []          # 보유 중 슬롯의 종료 ts
    cooldown_until: dict[str, int] = {}  # symbol → ts
    loss_count: dict[str, int] = {}      # 동적 블랙리스트용
    banned: set[str] = set()

    params = ExitParams(tp_pct=cfg.tp_pct, fee_rate=fee)

    T = t0
    while T <= ts_max:
        # 만료된 슬롯 회수
        open_until = [u for u in open_until if u > T]
        free = cfg.max_concurrent - len(open_until)
        if free <= 0:
            T += step
            continue

        # 후보 평가
        cands: list[tuple[float, str, int]] = []  # (score, symbol, idx)
        for s, ind in inds.items():
            if s in banned:
                continue
            if cfg.whitelist is not None and s not in cfg.whitelist:
                continue
            if cooldown_until.get(s, 0) > T:
                continue
            i = idx_of[s].get(T)
            if i is None or i < cfg.min_lookback_bars:
                continue
            if i >= len(ind.c) - 2:        # 미래 봉 부족
                continue
            if not _passes_filter(ind, i, cfg):
                continue
            cands.append((_score(ind, i, cfg), s, i))

        if not cands:
            T += step
            continue
        cands.sort(reverse=True)

        # 상위 free개 진입
        for sc, s, i in cands[:free]:
            ind = inds[s]
            buy_price = float(ind.c[i])
            fut = [(int(ind.ts[k]), float(ind.o[k]), float(ind.h[k]),
                    float(ind.l[k]), float(ind.c[k]), float(ind.v[k]))
                   for k in range(i + 1, len(ind.c))]
            sim = simulate_exit(buy_price, fut, params)
            entries.append(SimEntry(s, int(ind.ts[i]), buy_price, sim))
            open_until.append(sim.exit_ts if sim.closed else int(ind.ts[-1]))
            # 쿨다운
            cd = _COOLDOWN_MIN.get(sim.reason, 60)
            cooldown_until[s] = sim.exit_ts + cd * 60 if sim.closed else T + cd * 60
            # 동적 블랙리스트
            if cfg.permanent_blacklist_losses and sim.closed and sim.realized_pnl_pct < 0:
                loss_count[s] = loss_count.get(s, 0) + 1
                if loss_count[s] >= cfg.blacklist_loss_threshold:
                    banned.add(s)
        T += step

    logger.info(f"[rule_engine:{cfg.name}] 진입 {len(entries)}건 / "
                f"유니버스 {len(inds)}종목 / 차단 {len(banned)}종목")
    return entries
