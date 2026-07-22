"""백테스트용 오더북 스냅샷 수집기 (v5 알파소스 ② 오더북 불균형).

거래량 상위 N종목의 호가창을 60초 주기로 `data/orderbook.db`에 누적 적재한다.
깊이별 호가잔량(원)을 L1/L5/L10/전체로 집계 저장하여, 후속 분석에서
오더북 불균형(imbalance = bidN/(bidN+askN))과 다음 봉 수익률의 관계를 측정한다.

거래소는 `settings.EXCHANGE_PROVIDER`(bithumb|upbit)로 자동 결정되며,
EC2 systemd 유닛(pochaco-obcollector / kuromi-obcollector)으로 상시 운영한다.
운영 봇/캔들 수집기와 별도 프로세스·별도 DB라 영향 0.

리드-래그(①)는 차익거래 속도에 잡아먹혀 리테일 실행 불가로 종결됨.
오더북 불균형은 5분봉 밖 마이크로구조 정보라 현 candles로는 측정 불가 →
"지금부터 스냅샷 누적" 후 2~3주 뒤 측정.

DB 스키마:
    orderbook(exchange, symbol, ts, mid, spread_bps,
              bid1, ask1, bid5, ask5, bid10, ask10, bidall, askall,
              PRIMARY KEY(exchange, symbol, ts))
    - bidN/askN = 상위 N레벨 누적 호가잔량(원 = 가격×수량)
    - ts = 스냅샷 epoch 초(버킷팅 없음 — 분석 시 임의 horizon 정렬)

실행:
    python -m backtest.orderbook_collector             # 상시 루프
    python -m backtest.orderbook_collector --once      # 1회만 (점검용)
    python -m backtest.orderbook_collector --top 40 --period 60
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
_COLLECT_PERIOD_SEC = 60          # 스냅샷 주기(초)
_TOP_N_DEFAULT = 40               # 거래량 상위 N종목
_PER_SYMBOL_SLEEP = 0.12          # 종목 간 호출 간격(초) — rate limit 보호
_DEPTHS = (1, 5, 10)              # 누적 집계 깊이

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "orderbook.db",
)


# ────────────────────────────────────────────────────────────────────── #
#  DB 초기화                                                                #
# ────────────────────────────────────────────────────────────────────── #
def _init_db(path: str) -> sqlite3.Connection:
    """orderbook.db 연결 생성 및 스키마 보장."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기(분석) 허용
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orderbook (
            exchange   TEXT    NOT NULL,
            symbol     TEXT    NOT NULL,
            ts         INTEGER NOT NULL,
            mid        REAL,
            spread_bps REAL,
            bid1  REAL, ask1  REAL,
            bid5  REAL, ask5  REAL,
            bid10 REAL, ask10 REAL,
            bidall REAL, askall REAL,
            PRIMARY KEY (exchange, symbol, ts)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ob_sym_ts "
        "ON orderbook(exchange, symbol, ts)"
    )
    conn.commit()
    return conn


# ────────────────────────────────────────────────────────────────────── #
#  수집 로직                                                                #
# ────────────────────────────────────────────────────────────────────── #
def _top_symbols(client, top_n: int) -> list[str]:
    """24h 거래대금 상위 top_n 종목 심볼 반환(candle 수집기와 동일 규약)."""
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


def _parse_levels(resp: dict) -> tuple[list[tuple[float, float]],
                                       list[tuple[float, float]]] | None:
    """빗썸/업비트 오더북 응답을 (bids, asks)로 정규화.

    각 레벨 = (price, qty). bids=매수(가격 내림차순), asks=매도(가격 오름차순).

    빗썸: data.bids/asks = [{"price": str, "quantity": str}, ...]
    업비트: data[0].orderbook_units = [{bid_price, bid_size, ask_price, ask_size}]
    """
    data = resp.get("data") if isinstance(resp, dict) else None
    if data is None:
        return None

    # 업비트: data가 리스트(단일 마켓)
    if isinstance(data, list):
        if not data or not isinstance(data[0], dict):
            return None
        units = data[0].get("orderbook_units", [])
        bids, asks = [], []
        for u in units:
            try:
                bp, bs = float(u["bid_price"]), float(u["bid_size"])
                ap, as_ = float(u["ask_price"]), float(u["ask_size"])
            except (KeyError, TypeError, ValueError):
                continue
            if bp > 0:
                bids.append((bp, bs))
            if ap > 0:
                asks.append((ap, as_))
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return (bids, asks) if bids and asks else None

    # 빗썸: data가 dict
    if isinstance(data, dict):
        raw_b = data.get("bids", [])
        raw_a = data.get("asks", [])
        bids, asks = [], []
        for x in raw_b:
            try:
                p, q = float(x["price"]), float(x["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            if p > 0:
                bids.append((p, q))
        for x in raw_a:
            try:
                p, q = float(x["price"]), float(x["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            if p > 0:
                asks.append((p, q))
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        return (bids, asks) if bids and asks else None

    return None


def _cum_krw(levels: list[tuple[float, float]], depth: int) -> float:
    """상위 depth 레벨 누적 호가잔량(원 = 가격×수량)."""
    return sum(p * q for p, q in levels[:depth])


def _snapshot_symbol(client, exchange: str, symbol: str,
                     ts: int) -> tuple | None:
    """단일 종목 호가 스냅샷 → INSERT용 row (없으면 None)."""
    resp = client.get_orderbook(symbol)
    parsed = _parse_levels(resp)
    if parsed is None:
        return None
    bids, asks = parsed
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    spread_bps = (best_ask - best_bid) / mid * 10000.0
    b1, a1 = _cum_krw(bids, 1), _cum_krw(asks, 1)
    b5, a5 = _cum_krw(bids, 5), _cum_krw(asks, 5)
    b10, a10 = _cum_krw(bids, 10), _cum_krw(asks, 10)
    ball, aall = _cum_krw(bids, 10**9), _cum_krw(asks, 10**9)
    return (exchange, symbol, ts, mid, spread_bps,
            b1, a1, b5, a5, b10, a10, ball, aall)


def collect_once(conn: sqlite3.Connection, client, exchange: str,
                 top_n: int) -> tuple[int, int]:
    """1회 스냅샷 사이클 — 상위 top_n 종목 호가 적재.

    Returns:
        (신규 적재 행수, 처리 종목 수)
    """
    symbols = _top_symbols(client, top_n)
    ts = int(time.time())
    rows: list[tuple] = []
    ok = 0
    for sym in symbols:
        try:
            row = _snapshot_symbol(client, exchange, sym, ts)
        except Exception as e:
            # 부분 실패 허용 — 1종목 실패가 전체 사이클 중단 금지
            logger.warning(f"[{sym}] 호가 스냅샷 실패: {e}")
            continue
        if row is not None:
            rows.append(row)
            ok += 1
        time.sleep(_PER_SYMBOL_SLEEP)
    new = 0
    if rows:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO orderbook "
            "(exchange, symbol, ts, mid, spread_bps, "
            " bid1, ask1, bid5, ask5, bid10, ask10, bidall, askall) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        new = cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
    return new, ok


# ────────────────────────────────────────────────────────────────────── #
#  엔트리포인트                                                             #
# ────────────────────────────────────────────────────────────────────── #
def main() -> None:
    parser = argparse.ArgumentParser(description="오더북 스냅샷 누적 수집기")
    parser.add_argument("--once", action="store_true", help="1회만 수집 후 종료")
    parser.add_argument("--top", type=int, default=_TOP_N_DEFAULT,
                        help=f"거래량 상위 N종목 (기본 {_TOP_N_DEFAULT})")
    parser.add_argument("--period", type=int, default=_COLLECT_PERIOD_SEC,
                        help=f"스냅샷 주기 초 (기본 {_COLLECT_PERIOD_SEC})")
    parser.add_argument("--db", default=_DB_PATH, help="orderbook.db 경로")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    exchange = settings.EXCHANGE_PROVIDER
    client = get_exchange_client()
    conn = _init_db(args.db)

    logger.info(f"=== 오더북 수집기 시작 === 거래소={exchange} "
                f"상위{args.top}종목 주기{args.period}s DB={args.db}")

    if args.once:
        new, ok = collect_once(conn, client, exchange, args.top)
        total = conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()[0]
        logger.info(f"[1회 수집 완료] 신규 {new}행 / 종목 {ok}개 / 누적 {total}행")
        conn.close()
        return

    while True:
        start = time.monotonic()
        try:
            new, ok = collect_once(conn, client, exchange, args.top)
            total = conn.execute("SELECT COUNT(*) FROM orderbook").fetchone()[0]
            logger.info(f"[스냅샷] 신규 {new}행 / 종목 {ok}개 / 누적 {total}행")
        except Exception as e:
            logger.error(f"[수집 사이클 오류] {e}", exc_info=True)
        # 주기 보정 — 수집 소요시간 제외
        elapsed = time.monotonic() - start
        time.sleep(max(2.0, args.period - elapsed))


if __name__ == "__main__":
    main()
