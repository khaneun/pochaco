"""백테스트용 5분봉 캔들 수집기 (next_plan.md 1.A).

거래량 상위 N종목의 5분봉을 5분 주기로 `data/candles.db`에 누적 적재한다.
거래소는 `settings.EXCHANGE_PROVIDER`(bithumb|upbit)로 자동 결정되며,
EC2 systemd 유닛(pochaco-collector / kuromi-collector)으로 상시 운영한다.

빗썸 공개 API에 from/to 파라미터가 없어 과거 일괄 수집이 불가하므로
"지금부터 누적" 방식으로 우회한다. 운영 봇과는 별도 프로세스로 영향 0.

DB 스키마:
    candles(exchange, symbol, ts_5m, open, high, low, close, volume,
            PRIMARY KEY(exchange, symbol, ts_5m))

실행:
    python -m backtest.data_collector              # 상시 루프
    python -m backtest.data_collector --once       # 1회만 수집 (점검용)
    python -m backtest.data_collector --top 50     # 상위 종목 수 지정
"""
import argparse
import logging
import os
import sqlite3
import sys
import time

from config import settings
from core import get_exchange_client

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────── #
#  상수                                                                     #
# ────────────────────────────────────────────────────────────────────── #
_INTERVAL = "5m"                  # 5분봉
_COLLECT_PERIOD_SEC = 300         # 수집 주기(초) = 5분
_TOP_N_DEFAULT = 50              # 거래량 상위 N종목
_PER_SYMBOL_SLEEP = 0.15         # 종목 간 호출 간격(초) — rate limit 보호
_TS_BUCKET = 300                 # 5분(300초) 경계 정렬

# data/candles.db (프로젝트 루트 기준)
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "candles.db",
)


# ────────────────────────────────────────────────────────────────────── #
#  DB 초기화                                                                #
# ────────────────────────────────────────────────────────────────────── #
def _init_db(path: str) -> sqlite3.Connection:
    """candles.db 연결 생성 및 스키마 보장."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기(분석) 허용
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            exchange TEXT    NOT NULL,
            symbol   TEXT    NOT NULL,
            ts_5m    INTEGER NOT NULL,
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            volume   REAL,
            PRIMARY KEY (exchange, symbol, ts_5m)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candles_sym_ts "
        "ON candles(exchange, symbol, ts_5m)"
    )
    conn.commit()
    return conn


# ────────────────────────────────────────────────────────────────────── #
#  수집 로직                                                                #
# ────────────────────────────────────────────────────────────────────── #
def _top_symbols(client, top_n: int) -> list[str]:
    """24h 거래대금 상위 top_n 종목 심볼 반환.

    ticker 'ALL' 응답의 acc_trade_value_24H(거래대금) 기준 내림차순.
    빗썸/업비트 모두 동일 정규화 포맷(BaseExchangeClient).
    """
    resp = client.get_ticker("ALL")
    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    ranked: list[tuple[str, float]] = []
    for sym, d in data.items():
        if sym == "date" or not isinstance(d, dict):
            continue
        try:
            val = float(d.get("acc_trade_value_24H", 0) or 0)
        except (TypeError, ValueError):
            val = 0.0
        ranked.append((sym, val))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:top_n]]


def _norm_ts(raw_ts) -> int | None:
    """캔들 timestamp(ms 또는 s)를 5분 경계 epoch 초로 정규화."""
    try:
        ts = int(raw_ts)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:  # 13자리 → ms
        ts //= 1000
    return (ts // _TS_BUCKET) * _TS_BUCKET


def _collect_symbol(client, exchange: str, symbol: str) -> list[tuple]:
    """단일 종목 5분봉 조회 → INSERT용 row 리스트.

    캔들 포맷(빗썸/업비트 공통): [ts, open, close, high, low, volume]
    """
    resp = client.get_candlestick(symbol, _INTERVAL)
    candles = resp.get("data", []) if isinstance(resp, dict) else []
    rows: list[tuple] = []
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        ts_5m = _norm_ts(c[0])
        if ts_5m is None:
            continue
        try:
            o, close, high, low, vol = (
                float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
            )
        except (TypeError, ValueError):
            continue
        rows.append((exchange, symbol, ts_5m, o, high, low, close, vol))
    return rows


def collect_once(conn: sqlite3.Connection, client, exchange: str,
                 top_n: int) -> tuple[int, int]:
    """1회 수집 사이클 — 상위 top_n 종목 5분봉 적재.

    Returns:
        (신규 적재 행수, 처리 종목 수)
    """
    symbols = _top_symbols(client, top_n)
    total_new = 0
    ok = 0
    for sym in symbols:
        try:
            rows = _collect_symbol(client, exchange, sym)
        except Exception as e:
            # 부분 실패 허용 — 1종목 실패가 전체 사이클 중단 금지
            logger.warning(f"[{sym}] 캔들 수집 실패: {e}")
            continue
        if rows:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO candles "
                "(exchange, symbol, ts_5m, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            total_new += cur.rowcount if cur.rowcount > 0 else 0
            ok += 1
        time.sleep(_PER_SYMBOL_SLEEP)
    conn.commit()
    return total_new, ok


# ────────────────────────────────────────────────────────────────────── #
#  엔트리포인트                                                             #
# ────────────────────────────────────────────────────────────────────── #
def main() -> None:
    parser = argparse.ArgumentParser(description="5분봉 캔들 누적 수집기")
    parser.add_argument("--once", action="store_true", help="1회만 수집 후 종료")
    parser.add_argument("--top", type=int, default=_TOP_N_DEFAULT,
                        help=f"거래량 상위 N종목 (기본 {_TOP_N_DEFAULT})")
    parser.add_argument("--db", default=_DB_PATH, help="candles.db 경로")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    exchange = settings.EXCHANGE_PROVIDER
    client = get_exchange_client()
    conn = _init_db(args.db)

    logger.info(f"=== 캔들 수집기 시작 === 거래소={exchange} "
                f"상위{args.top}종목 주기{_COLLECT_PERIOD_SEC}s DB={args.db}")

    if args.once:
        new, ok = collect_once(conn, client, exchange, args.top)
        total = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        logger.info(f"[1회 수집 완료] 신규 {new}행 / 종목 {ok}개 / 누적 {total}행")
        conn.close()
        return

    while True:
        start = time.monotonic()
        try:
            new, ok = collect_once(conn, client, exchange, args.top)
            total = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            logger.info(f"[수집] 신규 {new}행 / 종목 {ok}개 / 누적 {total}행")
        except Exception as e:
            logger.error(f"[수집 사이클 오류] {e}", exc_info=True)
        # 주기 보정 — 수집 소요시간 제외하고 5분 간격 유지
        elapsed = time.monotonic() - start
        time.sleep(max(5.0, _COLLECT_PERIOD_SEC - elapsed))


if __name__ == "__main__":
    main()
