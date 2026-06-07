"""candles.db 읽기 헬퍼 (next_plan.md 1.A).

engine/metrics가 사용할 캔들 조회 인터페이스. 수집기와 분리하여
분석 측에서 읽기 전용으로 접근한다(WAL 모드라 동시 읽기 안전).
"""
import os
import sqlite3

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "candles.db",
)


def _connect(db_path: str = _DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def coverage(db_path: str = _DB_PATH) -> dict:
    """수집 현황 요약 — 거래소별 종목수·행수·기간.

    Returns:
        {exchange: {"symbols": n, "rows": n, "from": ts, "to": ts}}
    """
    conn = _connect(db_path)
    out: dict = {}
    rows = conn.execute(
        "SELECT exchange, COUNT(DISTINCT symbol) syms, COUNT(*) n, "
        "MIN(ts_5m) mn, MAX(ts_5m) mx FROM candles GROUP BY exchange"
    ).fetchall()
    for r in rows:
        out[r["exchange"]] = {
            "symbols": r["syms"], "rows": r["n"],
            "from": r["mn"], "to": r["mx"],
        }
    conn.close()
    return out


def load_candles(symbol: str, exchange: str,
                 ts_from: int | None = None, ts_to: int | None = None,
                 db_path: str = _DB_PATH) -> list[sqlite3.Row]:
    """단일 종목 5분봉 시계열(오래된→최신) 조회."""
    conn = _connect(db_path)
    q = ("SELECT ts_5m, open, high, low, close, volume FROM candles "
         "WHERE exchange=? AND symbol=?")
    params: list = [exchange, symbol]
    if ts_from is not None:
        q += " AND ts_5m >= ?"; params.append(ts_from)
    if ts_to is not None:
        q += " AND ts_5m <= ?"; params.append(ts_to)
    q += " ORDER BY ts_5m"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def symbols(exchange: str, db_path: str = _DB_PATH) -> list[str]:
    """수집된 종목 심볼 목록."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM candles WHERE exchange=? ORDER BY symbol",
        (exchange,),
    ).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]
