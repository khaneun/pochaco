"""토끼-28 포트폴리오 (portfolio_id=3) HEMI/H/ID 매도 기록 보정 스크립트

증상:
  - Trade id=38 (HEMI), id=40 (H), id=41 (ID) 가 추정가로 기록됨
  - StrategyEvaluation의 total_sell_krw / pnl_pct 도 오염됨

수정 방법:
  1. 빗썸 API에서 해당 종목의 실제 체결 완료 주문(state=done)을 조회
  2. 토끼-28 종료 시각 근처 항목을 자동 매칭
  3. Trade 레코드의 price / krw_amount 를 실제 체결가로 업데이트
  4. StrategyEvaluation total_sell_krw / pnl_pct 재계산

실행:
  cd /opt/pochaco
  python scripts/fix_rabbit28_trades.py [--dry-run]
"""
import argparse
import logging
import sys
import os
from datetime import datetime, timedelta, timezone

# ── 프로젝트 루트를 sys.path에 추가 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bithumb_client import BithumbClient
from database.models import SessionLocal, Trade, StrategyEvaluation
from sqlalchemy.orm import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────
#  수정 대상 설정
# ─────────────────────────────────────────────────────
PORTFOLIO_ID = 3
PORTFOLIO_NAME = "토끼-28"

# Trade 레코드 — 현재 저장된 추정값
TRADE_TARGETS: list[dict] = [
    {"trade_id": 38, "symbol": "HEMI", "approx_units": None},  # units는 DB에서 읽음
    {"trade_id": 40, "symbol": "H",    "approx_units": None},
    {"trade_id": 41, "symbol": "ID",   "approx_units": None},
]

# 토끼-28 종료 시각 (UTC) — DB의 StrategyEvaluation created_at 기준
# 조회 범위: 종료 전후 ±2시간
CLOSE_TIME_UTC_APPROX = None  # None이면 DB에서 자동 추출


# ─────────────────────────────────────────────────────
#  헬퍼
# ─────────────────────────────────────────────────────
def parse_bithumb_time(ts_str: str) -> datetime | None:
    """빗썸 ISO 시각 문자열 → UTC datetime"""
    if not ts_str:
        return None
    try:
        # 예: "2025-04-10T12:34:56+09:00"
        dt = datetime.fromisoformat(ts_str)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def find_best_match(
    executed_orders: list[dict],
    trade_units: float,
    ref_time: datetime,
    time_window_hours: float = 3.0,
) -> dict | None:
    """체결 수량과 시각으로 가장 유사한 주문을 찾음

    Args:
        executed_orders: get_executed_orders() 결과 목록
        trade_units: Trade 레코드의 units (수량)
        ref_time: 기준 시각 (UTC)
        time_window_hours: 이 범위 안에 있는 주문만 후보로 허용
    """
    window = timedelta(hours=time_window_hours)
    candidates = []
    for o in executed_orders:
        if o.get("side") != "ask":  # 매도만
            continue
        exec_time = parse_bithumb_time(o.get("created_at", ""))
        if exec_time is None:
            continue
        if abs((exec_time - ref_time).total_seconds()) > window.total_seconds():
            continue
        vol = o.get("executed_volume", 0)
        # 수량 유사도 (±5% 허용)
        if trade_units > 0 and vol > 0:
            ratio = min(vol, trade_units) / max(vol, trade_units)
            if ratio < 0.90:
                continue
        candidates.append((abs((exec_time - ref_time).total_seconds()), o))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ─────────────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────────────
def main(dry_run: bool) -> None:
    logger.info(f"=== {PORTFOLIO_NAME} 매도 기록 보정 {'(DRY-RUN)' if dry_run else ''} ===")

    client = BithumbClient()
    db: Session = SessionLocal()

    try:
        # ── 1. Trade 레코드 로드 ──
        trade_ids = [t["trade_id"] for t in TRADE_TARGETS]
        trades: list[Trade] = db.query(Trade).filter(Trade.id.in_(trade_ids)).all()
        trade_map = {t.id: t for t in trades}

        if not trade_map:
            logger.error("Trade 레코드를 찾지 못했습니다. trade_id 목록을 확인하세요.")
            return

        logger.info("현재 DB 상태:")
        for tid in trade_ids:
            t = trade_map.get(tid)
            if t:
                logger.info(
                    f"  Trade #{tid} [{t.symbol}] "
                    f"units={t.units:.6f} price={t.price:,.0f}원 "
                    f"krw={t.krw_amount:,.0f}원 created_at={t.created_at}"
                )

        # ── 2. 기준 시각 결정 (Trade 중 가장 최신 created_at) ──
        ref_time = max(
            (t.created_at for t in trade_map.values() if t and t.created_at),
            default=datetime.utcnow(),
        )
        logger.info(f"기준 시각 (UTC): {ref_time}")

        # ── 3. 종목별 실제 체결 기록 조회 + 매칭 ──
        updates: list[dict] = []  # {trade_id, symbol, new_price, new_krw, uuid}

        for target in TRADE_TARGETS:
            tid = target["trade_id"]
            symbol = target["symbol"]
            tr = trade_map.get(tid)
            if tr is None:
                logger.warning(f"  Trade #{tid} DB에 없음, 스킵")
                continue

            logger.info(f"\n[{symbol}] 빗썸 체결 완료 주문 조회...")
            try:
                done_orders = client.get_executed_orders(symbol, limit=50)
            except Exception as e:
                logger.error(f"  [{symbol}] API 조회 실패: {e}")
                continue

            logger.info(f"  [{symbol}] 조회된 done 주문 수: {len(done_orders)}")
            for o in done_orders[:5]:  # 최근 5개만 미리 표시
                logger.info(
                    f"    uuid={o['uuid'][:8]}... side={o['side']} "
                    f"vol={o['executed_volume']:.6f} funds={o['executed_funds']:,.0f}원 "
                    f"avg={o['avg_price']:,.2f}원 at={o['created_at']}"
                )

            best = find_best_match(done_orders, tr.units, ref_time, time_window_hours=3.0)
            if best is None:
                logger.warning(
                    f"  [{symbol}] ±3시간 내 수량 일치 주문 없음 — 수동 확인 필요\n"
                    f"  현재 DB: units={tr.units:.6f}, 기준시각={ref_time}"
                )
                # 수량 무관 시각 일치 완화 재시도
                best = find_best_match(done_orders, tr.units, ref_time, time_window_hours=24.0)
                if best:
                    logger.info(f"  [{symbol}] ±24시간 완화 범위에서 후보 발견, 사용")

            if best is None:
                logger.warning(f"  [{symbol}] 매칭 실패 — 이 종목은 수동 보정 필요")
                continue

            new_price = best["avg_price"]
            new_krw = best["executed_funds"]
            if new_krw <= 0 and new_price > 0:
                new_krw = new_price * tr.units

            logger.info(
                f"  [{symbol}] 매칭 성공:\n"
                f"    uuid={best['uuid']}\n"
                f"    체결가={new_price:,.2f}원 → 체결금액={new_krw:,.0f}원\n"
                f"    기존: price={tr.price:,.0f}원 / krw={tr.krw_amount:,.0f}원\n"
                f"    변경: price={new_price:,.2f}원 / krw={new_krw:,.0f}원"
            )
            updates.append({
                "trade_id": tid,
                "symbol": symbol,
                "new_price": new_price,
                "new_krw": new_krw,
                "uuid": best["uuid"],
            })

        if not updates:
            logger.warning("업데이트할 항목이 없습니다.")
            return

        # ── 4. Trade 레코드 업데이트 ──
        if not dry_run:
            for upd in updates:
                tr = trade_map[upd["trade_id"]]
                tr.price = upd["new_price"]
                tr.krw_amount = upd["new_krw"]
                tr.order_id = upd["uuid"]
                logger.info(
                    f"  Trade #{upd['trade_id']} [{upd['symbol']}] 업데이트 완료"
                )
            db.flush()
        else:
            logger.info("[DRY-RUN] Trade 레코드 업데이트 스킵")

        # ── 5. StrategyEvaluation 재계산 ──
        eval_rec: StrategyEvaluation | None = (
            db.query(StrategyEvaluation)
            .filter(StrategyEvaluation.portfolio_id == PORTFOLIO_ID)
            .order_by(StrategyEvaluation.created_at.desc())
            .first()
        )
        if eval_rec:
            # 전체 sell Trade 합산
            sell_trades: list[Trade] = (
                db.query(Trade)
                .filter(Trade.portfolio_id == PORTFOLIO_ID, Trade.side == "sell")
                .all()
            )
            # 업데이트된 krw_amount 반영 (dry_run이면 메모리상 변경값 사용)
            total_sell = 0.0
            for st in sell_trades:
                # updates에 있는 것은 new_krw로, 나머지는 DB 값 그대로
                upd_map = {u["trade_id"]: u["new_krw"] for u in updates}
                krw = upd_map.get(st.id, st.krw_amount)
                total_sell += krw

            new_pnl_pct = (
                (total_sell - eval_rec.total_buy_krw) / eval_rec.total_buy_krw * 100
                if eval_rec.total_buy_krw > 0 else 0.0
            )
            logger.info(
                f"\n[StrategyEvaluation #{eval_rec.id}] 재계산:\n"
                f"  total_buy_krw = {eval_rec.total_buy_krw:,.0f}원\n"
                f"  total_sell_krw: {eval_rec.total_sell_krw:,.0f}원 → {total_sell:,.0f}원\n"
                f"  pnl_pct: {eval_rec.pnl_pct:.2f}% → {new_pnl_pct:.2f}%"
            )
            if not dry_run:
                eval_rec.total_sell_krw = total_sell
                eval_rec.pnl_pct = new_pnl_pct
                logger.info("  StrategyEvaluation 업데이트 완료")
            else:
                logger.info("  [DRY-RUN] StrategyEvaluation 업데이트 스킵")
        else:
            logger.warning("StrategyEvaluation 레코드를 찾지 못했습니다.")

        # ── 6. 커밋 ──
        if not dry_run:
            db.commit()
            logger.info("\n=== DB 커밋 완료 ===")
        else:
            db.rollback()
            logger.info("\n=== DRY-RUN 완료 (DB 변경 없음) ===")

    except Exception as e:
        db.rollback()
        logger.error(f"오류 발생: {e}", exc_info=True)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="토끼-28 매도 기록 보정")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="변경 없이 조회·매칭 결과만 출력",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
