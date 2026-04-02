"""포지션 감시 - 익절/손절 트리거"""
import logging
import time
from datetime import datetime, timezone

from core import BithumbClient
from database import TradeRepository
from database.models import Position

logger = logging.getLogger(__name__)


class PositionManager:
    """보유 포지션의 현재가를 주기적으로 확인해 익절/손절 실행"""

    def __init__(self, client: BithumbClient, repo: TradeRepository):
        self._client = client
        self._repo = repo
        self._running = False

    def check_and_execute(self, position: Position) -> str | None:
        """포지션 상태 점검. 매도 실행 시 사유 반환, 유지 시 None"""
        current_price = self._client.get_current_price(position.symbol)
        pnl_pct = (current_price - position.buy_price) / position.buy_price * 100

        logger.debug(
            f"[포지션 점검] {position.symbol} "
            f"매수가={position.buy_price:,.0f} 현재가={current_price:,.0f} "
            f"수익={pnl_pct:+.2f}% "
            f"익절기준=+{position.take_profit_pct}% "
            f"손절기준={position.stop_loss_pct}%"
        )

        reason = None
        if pnl_pct >= position.take_profit_pct:
            reason = f"익절 ({pnl_pct:+.2f}% ≥ +{position.take_profit_pct}%)"
        elif pnl_pct <= position.stop_loss_pct:
            reason = f"손절 ({pnl_pct:+.2f}% ≤ {position.stop_loss_pct}%)"

        if reason:
            self._execute_sell(position, current_price, pnl_pct, reason)

        return reason

    def _execute_sell(
        self,
        position: Position,
        current_price: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        logger.info(f"[매도 실행] {reason}")
        try:
            result = self._client.market_sell(position.symbol, position.units)
            if result.get("status") != "0000":
                logger.error(f"매도 실패: {result}")
                return

            krw_received = current_price * position.units
            self._repo.save_trade(
                symbol=position.symbol,
                side="sell",
                price=current_price,
                units=position.units,
                krw_amount=krw_received,
                note=reason,
            )
            self._repo.close_position(position.id)
            logger.info(
                f"[매도 완료] {position.symbol} "
                f"수량={position.units} 수익={pnl_pct:+.2f}% 사유={reason}"
            )
        except Exception as e:
            logger.error(f"매도 실행 중 오류: {e}")

    def run_loop(self, interval_seconds: int = 10) -> None:
        """포지션 감시 루프 (블로킹). 별도 스레드에서 실행 권장"""
        self._running = True
        logger.info("포지션 감시 루프 시작")
        while self._running:
            try:
                pos = self._repo.get_open_position()
                if pos:
                    self.check_and_execute(pos)
            except Exception as e:
                logger.error(f"포지션 감시 오류: {e}")
            time.sleep(interval_seconds)
        logger.info("포지션 감시 루프 종료")

    def stop(self) -> None:
        self._running = False
