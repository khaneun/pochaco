"""APScheduler — 일별 리포트, DB 백업, 총괄 전문가 평가

매매 로직(현금화·코인선정·매수)은 TradingEngine이 담당합니다.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import BaseExchangeClient
from database import TradeRepository, backup_sqlite

logger = logging.getLogger(__name__)


class TradingScheduler:
    """일별 성과 기록, SQLite 백업, 3시간 총괄 평가 스케줄러"""

    def __init__(
        self,
        client: BaseExchangeClient,
        repo: TradeRepository,
        get_daily_start_krw,          # callable: () -> float
        notifier=None,                # TelegramBot (선택)
        coordinator=None,             # AgentCoordinator (선택)
    ):
        self._client = client
        self._repo = repo
        self._get_daily_start_krw = get_daily_start_krw
        self._notifier = notifier
        self._coordinator = coordinator
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
        # 3시간마다 총괄 전문가 평가 (0, 3, 6, 9, 12, 15, 18, 21시)
        if self._coordinator:
            self._scheduler.add_job(
                self._job_meta_evaluation,
                CronTrigger(hour="0,3,6,9,12,15,18,21", minute=0),
                id="meta_evaluation",
                replace_existing=True,
            )
        self._scheduler.start()
        meta_str = " / 3시간 주기 총괄평가" if self._coordinator else ""
        logger.info(f"스케줄러 시작 (23:50 백업 / 23:55 리포트{meta_str})")

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------ #
    #  일별 성과 저장                                                       #
    # ------------------------------------------------------------------ #
    def _job_save_daily_report(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            # 종료 총자산 = KRW + 보유 코인 평가액
            ending_krw = self._client.get_krw_balance()
            open_pf = self._repo.get_open_portfolio()
            if open_pf:
                try:
                    for pos in self._repo.get_portfolio_positions(open_pf.id):
                        try:
                            cur_price = self._client.get_current_price(pos.symbol)
                            ending_krw += pos.units * cur_price
                        except Exception:
                            ending_krw += pos.buy_krw
                except Exception as pe:
                    logger.warning(f"포트폴리오 평가액 조회 실패: {pe}")

            starting_krw = self._get_daily_start_krw()

            recent = self._repo.get_recent_trades(200)
            today_trades = [
                t for t in recent
                if t.created_at.strftime("%Y-%m-%d") == today
            ]
            sell_trades = [t for t in today_trades if t.side == "sell"]

            # 익절로 끝난 매도 수 (note에 "익절" 포함)
            win_count = sum(1 for t in sell_trades if "익절" in (t.note or ""))

            # 수수료 추정: 거래 금액 × 0.25% (빗썸 기본 수수료)
            total_fee = sum(
                (t.fee if t.fee and t.fee > 0 else t.krw_amount * 0.0025)
                for t in today_trades
            )

            report = self._repo.upsert_daily_report(
                date_str=today,
                starting_krw=starting_krw,
                ending_krw=ending_krw,
                trade_count=len(today_trades),
                win_count=win_count,
                total_fee=round(total_fee, 0),
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

    # ------------------------------------------------------------------ #
    #  총괄 전문가 평가 (3시간 주기)                                         #
    # ------------------------------------------------------------------ #
    def _job_meta_evaluation(self) -> None:
        """3시간 주기(0,3,6,...,21시) 실행 — 5개 전문가를 종합 평가"""
        try:
            feedbacks = self._coordinator.run_meta_evaluation()
            logger.info(f"[총괄 평가] {len(feedbacks)}개 Agent 평가 완료")
            for fb in feedbacks:
                logger.info(
                    f"  {fb.agent_role}: {fb.score:.0f}점 ({fb.priority})"
                )

            if self._notifier and feedbacks:
                try:
                    lines = [f"  {fb.agent_role}: {fb.score:.0f}점" for fb in feedbacks]
                    self._notifier.send(
                        f"📋 <b>총괄 전문가 평가 완료</b>\n" + "\n".join(lines)
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[총괄 평가 오류] {e}", exc_info=True)
