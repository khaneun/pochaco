"""백테스트 실행 CLI (next_plan.md 1.A).

baseline 시나리오를 실행하여 KPI + 검증 게이트 일치율을 산출하고,
결과를 stdout과 (옵션) 텔레그램으로 보고한다. EC2 systemd timer로 주기 실행.

실행:
    python -m backtest.cli --scenario baseline          # 자거래소 자동 감지
    python -m backtest.cli --scenario baseline --telegram
    python -m backtest.cli --op-db data/pochaco.db --candles data/candles.db
"""
import argparse
import logging
import os
import sys

from config import settings
from backtest import data_loader
from backtest.metrics import MatchStat, compute_kpi
from backtest.scenarios import baseline

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_op_db() -> str:
    """settings.DB_PATH 또는 data/<app>.db 추정."""
    p = getattr(settings, "DB_PATH", "") or ""
    if p and os.path.exists(p):
        return p
    app = getattr(settings, "APP_NAME", "pochaco") or "pochaco"
    return os.path.join(_ROOT, "data", f"{app}.db")


def _send_telegram(text: str) -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        logger.warning("[텔레그램] 토큰/챗ID 없음 — 발송 스킵")
        return
    import requests
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("[텔레그램] 발송 완료")
    except Exception as e:
        logger.warning(f"[텔레그램] 발송 실패: {e}")


def run_baseline(op_db: str, candles_db: str, exchange: str) -> str:
    """baseline 실행 → 보고 텍스트 반환."""
    cov = data_loader.coverage(candles_db).get(exchange, {})
    entries = baseline.load_entries(op_db, exchange, candles_db)
    results = baseline.run(entries, exchange, require_candles=True)

    sim_pnls = [r["sim"].realized_pnl_pct for r in results if r["sim"].closed]
    sim_reasons = [r["sim"].reason for r in results if r["sim"].closed]
    kpi = compute_kpi(sim_pnls, sim_reasons)

    # 검증 게이트 — 시뮬 vs 운영 (운영 청산이 있는 건만)
    ms = MatchStat()
    for r in results:
        sim = r["sim"]
        a_px, a_ts, a_pnl = (r["actual_exit_price"], r["actual_exit_ts"],
                             r["actual_pnl_pct"])
        if not sim.closed or a_px is None or a_ts is None or a_pnl is None:
            continue
        ms.total += 1
        if a_px > 0 and abs(sim.exit_price - a_px) / a_px <= 0.003:
            ms.price_match += 1
        if abs(sim.exit_ts - a_ts) <= 600:
            ms.time_match += 1
        if abs(sim.realized_pnl_pct - a_pnl) <= max(5.0, abs(a_pnl) * 0.05):
            ms.pnl_match += 1

    # ── 보고 텍스트 ──
    from datetime import datetime, timezone
    fr = cov.get("from"); to = cov.get("to")

    def _d(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%m/%d %H:%M") if ts else "-"

    lines = [
        f"📊 <b>백테스트 baseline — {exchange}</b>",
        f"candles: {cov.get('symbols', 0)}종목 {cov.get('rows', 0):,}행 "
        f"({_d(fr)}~{_d(to)} UTC)",
        f"검증 가능 진입: {len(results)}건",
        "",
        "<b>[시뮬 청산 KPI]</b>",
    ]
    lines += ["  " + ln for ln in kpi.as_lines()]
    if kpi.reason_dist:
        rd = " / ".join(f"{k} {v}" for k, v in kpi.reason_dist.items())
        lines.append(f"  청산사유: {rd}")
    lines += ["", "<b>[검증 게이트 — 시뮬 vs 운영]</b>"]
    lines += ["  " + ln for ln in ms.as_lines()]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="백테스트 실행")
    parser.add_argument("--scenario", default="baseline", choices=["baseline"])
    parser.add_argument("--op-db", default=None, help="운영 DB 경로")
    parser.add_argument("--candles", default=data_loader._DB_PATH, help="candles.db 경로")
    parser.add_argument("--exchange", default=None, help="bithumb|upbit (기본: settings)")
    parser.add_argument("--telegram", action="store_true", help="결과 텔레그램 발송")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    exchange = args.exchange or settings.EXCHANGE_PROVIDER
    op_db = args.op_db or _default_op_db()

    if not os.path.exists(op_db):
        logger.error(f"운영 DB 없음: {op_db}")
        sys.exit(1)
    if not os.path.exists(args.candles):
        logger.error(f"candles.db 없음: {args.candles}")
        sys.exit(1)

    logger.info(f"=== 백테스트 시작 === scenario={args.scenario} "
                f"exchange={exchange} op_db={op_db} candles={args.candles}")

    report = run_baseline(op_db, args.candles, exchange)
    print("\n" + report.replace("<b>", "").replace("</b>", "") + "\n")

    if args.telegram:
        _send_telegram(report)


if __name__ == "__main__":
    main()
