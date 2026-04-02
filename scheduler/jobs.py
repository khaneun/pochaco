"""APScheduler — 일별 리포트 및 DB 백업 전용

매매 로직(현금화·코인선정·매수)은 TradingEngine이 담당합니다.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import BithumbClient
from database import TradeRepository, backup_sqlite

logger = logging.getLogger(__name__)


class TradingScheduler:
    """일별 성과 기록 및 SQLite 백업 스케줄러"""

    def __init__(
        self,
        client: BithumbClient,
        repo: TradeRepository,
        get_daily_start_krw,          # callable: () -> float
        notifier=None,                # TelegramBot (선택)
    ):
        self._client = client
        self._repo = repo
        self._get_daily_start_krw = get_daily_start_krw
        self._notifier = notifier
        self._scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    def start(self) -> None:
        # 매일 23:55 일별 성과 기록
        self._scheduler.add_job(
            self._job_save_daily_report,
            CronTrigger(hour=23, minute=55),
            id="daily_report",
            replace_existing=True,
        )
        # 매일 23:50 SQLite 백업 (외부 DB 사용 시 자동 스킵)
        self._scheduler.add_job(
            backup_sqlite,
            CronTrigger(hour=23, minute=50),
            id="db_backup",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("스케줄러 시작 (23:50 백업 / 23:55 리포트)")

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------ #
    #  일별 성과 저장                                                       #
    # ------------------------------------------------------------------ #
    def _job_save_daily_report(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            ending_krw = self._client.get_krw_balance()
            starting_krw = self._get_daily_start_krw()

            recent = self._repo.get_recent_trades(100)
            today_trades = [
                t for t in recent
                if t.created_at.strftime("%Y-%m-%d") == today
            ]
            sell_trades = [t for t in today_trades if t.side == "sell"]
            buy_trades  = [t for t in today_trades if t.side == "buy"]

            # 익절로 끝난 매도 수 (note에 "익절" 포함)
            win_count = sum(1 for t in sell_trades if "익절" in (t.note or ""))

            report = self._repo.upsert_daily_report(
                date_str=today,
                starting_krw=starting_krw,
                ending_krw=ending_krw,
                trade_count=len(today_trades),
                win_count=win_count,
            )
            logger.info(f"[일별 리포트] {today} 저장 완료 (거래 {len(today_trades)}건, 익절 {win_count}건)")

            if self._notifier:
                try:
                    self._notifier.notify_daily_report(
                        date=today,
                        starting_krw=starting_krw,
                        ending_krw=ending_krw,
                        pnl_krw=report.pnl_krw,
                        pnl_pct=report.pnl_pct,
                        trade_count=len(today_trades),
                        win_count=win_count,
                    )
                except Exception as ne:
                    logger.warning(f"텔레그램 일별 리포트 알림 실패: {ne}")
        except Exception as e:
            logger.error(f"일별 리포트 저장 실패: {e}")
