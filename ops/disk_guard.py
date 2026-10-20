"""디스크 가드 — 보관기간·용량 버퍼 기반 주기 정리 (v5.1)

배경 (2026-10-20 장애):
  - kuromi: 구 도커 이미지 13GB 적체 → 디스크 100% → 오더북 수집 58일 중단·봇 DB disk I/O error
  - pochaco: 8GB 디스크에 orderbook.db·journal·앱 로그가 정리 정책 없이 무한 증가

정리 단계:
  1. 상시 — 보관기간 초과 데이터 삭제
     - orderbook.db: --ob-days (기본 60일)
     - candles.db:   --candle-days (기본 365일, 백테스트 자산이라 길게)
     - journald:     --journal-max (기본 300M) 초과분 vacuum
  2. 사용률 ≥ --warn (기본 80%) — 공격적 정리
     - orderbook 보관기간 절반(최소 7일)으로 축소
     - journald 100M, docker 미사용 이미지 prune, apt 캐시 정리
     - 빈 페이지 비율 높은 DB는 수집기 잠시 정지 후 VACUUM (여유공간 충분 시)
  3. 정리 후에도 ≥ --crit (기본 90%) — 텔레그램 경보

실행 (systemd timer, 앱 사용자 권한 + journalctl/docker/systemctl은 sudo -n):
  python -m ops.disk_guard --app pochaco --data-dir /opt/pochaco/data
"""
import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time

from config import settings

logger = logging.getLogger(__name__)

_DELETE_BATCH = 5_000          # 배치당 삭제 행 수 — 수집기 쓰기 잠금 최소화
_VACUUM_FREE_RATIO = 0.30      # 빈 페이지 비율 이 이상이면 VACUUM 후보
_VACUUM_SPACE_FACTOR = 1.5     # VACUUM 임시공간 — DB 크기 × 이 배수 여유 필요
_MIN_OB_DAYS = 7               # 압박 시에도 오더북 최소 보관일


# ────────────────────────────────────────────────────────────────────────────
#  유틸
# ────────────────────────────────────────────────────────────────────────────
def _disk_pct(path: str = "/") -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100


def _sudo(*cmd: str) -> None:
    """sudo -n 명령 실행 — 실패해도 다음 단계 계속 (부분 실패 허용)"""
    try:
        r = subprocess.run(
            ["sudo", "-n", *cmd], capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            logger.warning(f"[명령 실패] {' '.join(cmd)}: {r.stderr.strip()[:200]}")
    except Exception as e:
        logger.warning(f"[명령 오류] {' '.join(cmd)}: {e}")


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
    except Exception as e:
        logger.warning(f"[텔레그램] 발송 실패: {e}")


# ────────────────────────────────────────────────────────────────────────────
#  DB 보관기간 정리
# ────────────────────────────────────────────────────────────────────────────
def prune_table(db_path: str, table: str, ts_col: str, keep_days: float) -> int:
    """보관기간 초과 행을 (exchange, symbol) 인덱스 단위 배치 삭제.

    Args:
        db_path: SQLite 경로
        table: 테이블명 (orderbook | candles)
        ts_col: epoch초 컬럼명
        keep_days: 보관일

    Returns:
        삭제 행 수
    """
    if not os.path.exists(db_path):
        return 0
    cutoff = int(time.time() - keep_days * 86400)
    conn = sqlite3.connect(db_path, timeout=60)
    deleted = 0
    try:
        keys = conn.execute(f"SELECT DISTINCT exchange, symbol FROM {table}").fetchall()
        for exchange, symbol in keys:
            while True:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN ("
                    f"  SELECT rowid FROM {table}"
                    f"  WHERE exchange=? AND symbol=? AND {ts_col}<? LIMIT ?)",
                    (exchange, symbol, cutoff, _DELETE_BATCH),
                )
                conn.commit()
                deleted += cur.rowcount
                if cur.rowcount < _DELETE_BATCH:
                    break
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    logger.info(f"[보관기간 정리] {os.path.basename(db_path)}: {deleted:,}행 삭제 (>{keep_days:g}일)")
    return deleted


def vacuum_if_bloated(db_path: str, collector_unit: str) -> None:
    """빈 페이지 비율이 높고 여유공간이 충분하면 수집기 정지 → VACUUM → 재기동.

    삭제만으로는 파일이 줄지 않으므로(빈 페이지 재사용) 실제 용량 회수용.
    """
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    if page_count == 0 or free_count / page_count < _VACUUM_FREE_RATIO:
        return
    size = os.path.getsize(db_path)
    if shutil.disk_usage(os.path.dirname(db_path)).free < size * _VACUUM_SPACE_FACTOR:
        logger.warning(f"[VACUUM 보류] {os.path.basename(db_path)} 여유공간 부족")
        return

    logger.info(
        f"[VACUUM] {os.path.basename(db_path)} 빈페이지 {free_count / page_count:.0%} "
        f"— {collector_unit} 일시 정지"
    )
    _sudo("systemctl", "stop", collector_unit)
    try:
        conn = sqlite3.connect(db_path, timeout=120)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[VACUUM 실패] {e}", exc_info=True)
    finally:
        _sudo("systemctl", "start", collector_unit)
    logger.info(
        f"[VACUUM 완료] {size / 1e6:,.0f}MB → {os.path.getsize(db_path) / 1e6:,.0f}MB"
    )


# ────────────────────────────────────────────────────────────────────────────
#  메인
# ────────────────────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> None:
    ob_db = os.path.join(args.data_dir, "orderbook.db")
    candle_db = os.path.join(args.data_dir, "candles.db")

    start_pct = _disk_pct()
    logger.info(f"=== 디스크 가드 시작 === 사용률 {start_pct:.1f}%")

    # ── 1단계: 상시 보관기간 정리
    for db, table, col, days in (
        (ob_db, "orderbook", "ts", args.ob_days),
        (candle_db, "candles", "ts_5m", args.candle_days),
    ):
        try:
            prune_table(db, table, col, days)
        except Exception as e:
            logger.error(f"[정리 실패] {table}: {e}", exc_info=True)
    _sudo("journalctl", f"--vacuum-size={args.journal_max}")

    # ── 2단계: 용량 버퍼 이탈 시 공격적 정리
    if _disk_pct() >= args.warn:
        logger.warning(f"[용량 경고] {_disk_pct():.1f}% ≥ {args.warn}% — 공격적 정리")
        try:
            prune_table(ob_db, "orderbook", "ts", max(_MIN_OB_DAYS, args.ob_days / 2))
        except Exception as e:
            logger.error(f"[정리 실패] orderbook 축소: {e}", exc_info=True)
        _sudo("journalctl", "--vacuum-size=100M")
        if shutil.which("docker"):
            _sudo("docker", "system", "prune", "-af")
        if shutil.which("apt-get"):
            _sudo("apt-get", "clean")
        elif shutil.which("dnf"):
            _sudo("dnf", "clean", "all")

    # 빈 페이지 회수 — 여유공간 있을 때만 (압박 여부 무관, 비대 DB 축소)
    for db, unit in ((ob_db, f"{args.app}-obcollector"), (candle_db, f"{args.app}-collector")):
        try:
            vacuum_if_bloated(db, unit)
        except Exception as e:
            logger.error(f"[VACUUM 오류] {db}: {e}", exc_info=True)

    end_pct = _disk_pct()
    logger.info(f"=== 디스크 가드 종료 === 사용률 {start_pct:.1f}% → {end_pct:.1f}%")

    # ── 3단계: 정리 후에도 위험 수준이면 경보
    if end_pct >= args.crit:
        _send_telegram(
            f"🚨 <b>중요 알람</b> — [{args.app}] 디스크 {end_pct:.1f}% "
            f"(정리 후에도 ≥{args.crit}%) — 수동 점검 필요"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="디스크 가드 — 보관기간·용량 버퍼 정리")
    parser.add_argument("--app", default=settings.APP_NAME, help="서비스 접두사 (pochaco|kuromi)")
    parser.add_argument("--data-dir", required=True, help="candles.db·orderbook.db 디렉토리")
    parser.add_argument("--ob-days", type=float, default=60, help="오더북 보관일")
    parser.add_argument("--candle-days", type=float, default=365, help="캔들 보관일")
    parser.add_argument("--journal-max", default="300M", help="journald 상시 상한")
    parser.add_argument("--warn", type=float, default=80, help="공격적 정리 개시 사용률(%%)")
    parser.add_argument("--crit", type=float, default=90, help="텔레그램 경보 사용률(%%)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run(args)


if __name__ == "__main__":
    main()
