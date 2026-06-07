"""baseline 시나리오 — 현 v4.4 청산 전략 재현 + 검증 게이트 (next_plan.md 1.A).

검증 게이트 방식:
  운영 DB(trades/portfolios)에서 실제 진입(코인·시각·매수가·TP/SL)을 강제 주입 →
  candles.db 5분봉으로 청산 로직만 시뮬 → 운영 실제 청산 결과와 비교.
  쿨다운/블랙리스트는 비활성(이미 실거래에 반영된 효과).
  합격선: 청산가 ±0.3% / 청산시각 ±10분 / 실현손익 ±5% 일치율 90%↑
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backtest import data_loader
from backtest.engine import ExitParams, ExitResult, FEE_RATE, simulate_exit


@dataclass
class Entry:
    portfolio_id: int
    symbol: str
    buy_ts: int                  # epoch 초
    buy_price: float
    tp_pct: float                # portfolio.take_profit_pct (트레일링 진입선)
    actual_exit_ts: int | None   # 운영 실제 청산시각(마지막 sell)
    actual_exit_price: float | None
    actual_pnl_pct: float | None
    candles: list = field(default_factory=list)


def _to_epoch(s: str) -> int:
    """'2026-06-07 12:14:22.668205'(UTC) → epoch 초."""
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def load_entries(op_db_path: str, exchange: str,
                 candles_db_path: str = data_loader._DB_PATH,
                 only_closed: bool = True) -> list[Entry]:
    """운영 DB에서 코인별 진입/실제청산을 추출하고 candles를 주입한다."""
    conn = sqlite3.connect(f"file:{op_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # 코인별 buy(첫 매수) + sell 집계 — portfolio 단위, 단일코인 가정
    q = """
      SELECT t.portfolio_id pid, t.symbol sym, t.side,
             t.price, t.units, t.krw_amount, t.created_at, p.take_profit_pct tp,
             p.is_open
      FROM trades t JOIN portfolios p ON p.id = t.portfolio_id
      ORDER BY t.portfolio_id, t.created_at
    """
    rows = conn.execute(q).fetchall()
    conn.close()

    # (pid, sym) 그룹화
    groups: dict[tuple, dict] = {}
    for r in rows:
        if only_closed and r["is_open"]:
            continue
        key = (r["pid"], r["sym"])
        g = groups.setdefault(key, {
            "tp": r["tp"], "buys": [], "sells": [],
        })
        leg = {"price": r["price"], "units": r["units"] or 0,
               "krw": r["krw_amount"] or 0, "ts": _to_epoch(r["created_at"])}
        (g["buys"] if r["side"] == "buy" else g["sells"]).append(leg)

    entries: list[Entry] = []
    for (pid, sym), g in groups.items():
        if not g["buys"]:
            continue
        # 진입: buy 가중평균가 + 첫 buy 시각
        buy_krw = sum(b["krw"] for b in g["buys"])
        buy_units = sum(b["units"] for b in g["buys"])
        buy_price = (buy_krw / buy_units) if buy_units else g["buys"][0]["price"]
        buy_ts = min(b["ts"] for b in g["buys"])
        tp = float(g["tp"] or 4.0)

        # 운영 실제 청산
        act_ts = act_px = act_pnl = None
        if g["sells"]:
            sell_krw = sum(s["krw"] for s in g["sells"])
            sell_units = sum(s["units"] for s in g["sells"])
            act_px = (sell_krw / sell_units) if sell_units else g["sells"][-1]["price"]
            act_ts = max(s["ts"] for s in g["sells"])
            if buy_krw > 0:
                act_pnl = (sell_krw - buy_krw) / buy_krw * 100.0

        # 진입 5분 버킷 — candle ts_5m과 정합
        buy_bucket = (buy_ts // 300) * 300
        rows_c = data_loader.load_candles(
            sym, exchange, ts_from=buy_bucket, db_path=candles_db_path
        )
        candles = [(c["ts_5m"], c["open"], c["high"], c["low"], c["close"],
                    c["volume"]) for c in rows_c]

        # 진입 시점에 실제 candle이 있어야 검증 가능 — 첫 봉이 진입 직후(≤2봉)가
        # 아니면(커버리지 밖 진입 등) 검증 불가로 candles 비움
        if candles and (candles[0][0] - buy_bucket) > 600:
            candles = []

        entries.append(Entry(pid, sym, buy_ts, buy_price, tp,
                             act_ts, act_px, act_pnl, candles))
    return entries


def run(entries: list[Entry], exchange: str,
        require_candles: bool = True) -> list[dict]:
    """각 진입에 청산 시뮬 실행 → 결과 리스트.

    Returns:
        [{symbol, sim: ExitResult, actual_*, has_candles}, ...]
    """
    fee = FEE_RATE.get(exchange, 0.0004)
    out: list[dict] = []
    for e in entries:
        if require_candles and len(e.candles) < 2:
            continue  # candles 미수집 구간 — 검증 불가
        params = ExitParams(tp_pct=e.tp_pct, fee_rate=fee)
        sim: ExitResult = simulate_exit(e.buy_price, e.candles, params)
        out.append({
            "symbol": e.symbol,
            "buy_ts": e.buy_ts,
            "sim": sim,
            "actual_exit_ts": e.actual_exit_ts,
            "actual_exit_price": e.actual_exit_price,
            "actual_pnl_pct": e.actual_pnl_pct,
            "n_candles": len(e.candles),
        })
    return out
