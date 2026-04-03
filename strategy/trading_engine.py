"""핵심 매매 루프 엔진

기동 시점부터 아래 사이클을 무한 반복합니다:
  1. 전체 현금화 (보유 코인 전부 시장가 매도 + 미체결 주문 취소)
  2. AI Agent 코인 선정 (변동성·거래량·등락폭 + 과거 성과 기반)
  3. 가용 KRW 전액 매수
  4. 익절 또는 손절 도달까지 감시 (보유 중 동적 조정 포함)
  5. 조건 충족 시 전량 시장가 매도 → 성과 평가 → 1번으로
"""
import logging
import time
from datetime import datetime

from config import settings
from core import BithumbClient
from database import TradeRepository
from database.models import Position
from strategy.ai_agent import TradingAgent
from strategy.market_analyzer import MarketAnalyzer

logger = logging.getLogger(__name__)

# 보유 중 전략 조정 주기 (초) — 30분마다
_ADJUST_INTERVAL_SEC = 30 * 60
# 최대 보유 시간 (분) — 초과 시 강제 매도
_MAX_HOLD_MINUTES = 720  # 12시간


class TradingEngine:
    """기동부터 종료까지 매매 사이클을 단일 루프로 관리"""

    def __init__(
        self,
        client: BithumbClient,
        repo: TradeRepository,
        agent: TradingAgent,
        analyzer: MarketAnalyzer,
    ):
        self._client = client
        self._repo = repo
        self._agent = agent
        self._analyzer = analyzer
        self._running = False
        self._paused = False
        self._notifier = None
        self._price_fail_count: dict[int, int] = {}  # position_id → 연속 실패 횟수
        self._last_adjust_time: float = 0.0           # 마지막 전략 조정 시각 (epoch)
        self._last_adjustment: dict | None = None     # 가장 최근 동적 조정 기록
        self.daily_start_krw: float = 0.0

    # ------------------------------------------------------------------ #
    #  퍼블릭 인터페이스                                                    #
    # ------------------------------------------------------------------ #
    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def pause(self) -> None:
        self._paused = True
        logger.info("TradingEngine: 매수 일시 중지")

    def resume(self) -> None:
        self._paused = False
        logger.info("TradingEngine: 매수 재개")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def run(self) -> None:
        """매매 루프 (블로킹). 별도 스레드에서 호출하세요."""
        self._running = True
        logger.info("=== TradingEngine 시작 ===")

        self.daily_start_krw = self._client.get_krw_balance()

        while self._running:
            try:
                pos = self._repo.get_open_position()

                if pos is None:
                    self._select_and_buy()
                else:
                    self._check_exit(pos)

            except Exception as e:
                logger.error(f"[엔진 오류] {e}", exc_info=True)
                if self._notifier:
                    try:
                        self._notifier.notify_error(str(e))
                    except Exception:
                        pass
                time.sleep(5)

            if self._running:
                time.sleep(settings.POSITION_CHECK_INTERVAL)

        logger.info("=== TradingEngine 종료 ===")

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ #
    #  미체결 주문 정리                                                     #
    # ------------------------------------------------------------------ #
    def _cancel_stuck_orders(self) -> None:
        """in_use_krw > 0이면 미체결 주문이 KRW를 묶고 있으므로 일괄 취소"""
        try:
            detail = self._client.get_krw_balance_detail()
            if detail["in_use"] <= 0:
                return
            logger.warning(f"[미체결 감지] in_use_krw={detail['in_use']:,.0f}원 — 일괄 취소")
            balance_data = self._client.get_balance("ALL")
            if balance_data.get("status") != "0000":
                return
            for key, value in balance_data["data"].items():
                if key.startswith("in_use_") and key != "in_use_krw":
                    symbol = key.replace("in_use_", "").upper()
                    if float(value) > 0:
                        self._client.cancel_all_orders(symbol)
            time.sleep(1)
        except Exception as e:
            logger.error(f"[미체결 취소 오류] {e}")

    # ------------------------------------------------------------------ #
    #  Step 1: 전체 현금화                                                  #
    # ------------------------------------------------------------------ #
    def _liquidate_all(self, note: str = "현금화") -> None:
        logger.info(f"[현금화 시작] {note}")

        # 먼저 미체결 주문 정리
        self._cancel_stuck_orders()

        balance_data = self._client.get_balance("ALL")
        if balance_data.get("status") != "0000":
            raise RuntimeError(f"잔고 조회 실패: {balance_data}")

        sold_any = False
        for key, value in balance_data["data"].items():
            if not key.startswith("available_"):
                continue
            symbol = key.replace("available_", "").upper()
            if symbol == "KRW":
                continue
            amount = float(value)
            if amount <= 0:
                continue

            try:
                current_price = self._client.get_current_price(symbol)
                krw_value = amount * current_price
                if krw_value < settings.MIN_ORDER_KRW:
                    logger.info(f"  {symbol} 소액({krw_value:.0f}원) 스킵")
                    continue

                result = self._client.market_sell(symbol, amount)
                if result.get("status") == "0000":
                    self._repo.save_trade(
                        symbol=symbol, side="sell",
                        price=current_price, units=amount,
                        krw_amount=krw_value, note=note,
                    )
                    sold_any = True
                    logger.info(f"  {symbol} {amount}개 → {krw_value:,.0f}원 매도 완료")
                else:
                    logger.warning(f"  {symbol} 매도 실패: {result}")
            except Exception as e:
                logger.error(f"  {symbol} 현금화 오류: {e}")

        self._repo.close_all_positions()

        krw = self._client.get_krw_balance()
        logger.info(f"[현금화 완료] {'매도 없음' if not sold_any else '완료'} KRW={krw:,.0f}원")

    # ------------------------------------------------------------------ #
    #  Step 2-3: 코인 선정 및 매수                                          #
    # ------------------------------------------------------------------ #
    def _select_and_buy(self) -> None:
        if self._paused:
            logger.info("[매수 스킵] 일시 중지 상태")
            return

        krw = self._client.get_krw_balance()
        if krw < settings.MIN_ORDER_KRW:
            logger.warning(f"[매수 스킵] KRW 잔고 부족: {krw:,.0f}원")
            self._cancel_stuck_orders()
            time.sleep(30)
            return

        logger.info("=== AI Agent 코인 선정 ===")
        snapshots = self._analyzer.build_market_summary(top_n=30)
        if not snapshots:
            logger.error("시장 데이터 수집 실패, 30초 후 재시도")
            time.sleep(30)
            return

        # 과거 성과 통계를 가져와서 Agent에게 전달
        eval_stats = self._repo.get_evaluation_stats(last_n=10)

        decision = self._agent.select_coin(snapshots, eval_stats=eval_stats)
        logger.info(
            f"[AI 선정] {decision.symbol} "
            f"익절=+{decision.take_profit_pct}% 손절={decision.stop_loss_pct}% "
            f"확신도={decision.confidence:.0%} | {decision.reason}"
        )

        buy_amount = krw * 0.95
        result = self._client.market_buy(decision.symbol, buy_amount)
        if result.get("status") != "0000":
            logger.error(f"[매수 실패] {result}")
            self._cancel_stuck_orders()
            time.sleep(2)
            krw = self._client.get_krw_balance()
            if krw >= settings.MIN_ORDER_KRW:
                buy_amount = krw * 0.95
                result = self._client.market_buy(decision.symbol, buy_amount)
                if result.get("status") != "0000":
                    logger.error(f"[매수 재시도 실패] {result} — 30초 후 재시도")
                    time.sleep(30)
                    return
            else:
                time.sleep(30)
                return

        # 체결 반영 대기
        time.sleep(1)
        units = self._client.get_coin_balance(decision.symbol)
        current_price = self._client.get_current_price(decision.symbol)

        self._repo.save_trade(
            symbol=decision.symbol, side="buy",
            price=current_price, units=units,
            krw_amount=buy_amount, note=decision.reason,
        )
        self._repo.open_position(
            symbol=decision.symbol,
            units=units,
            buy_price=current_price,
            buy_krw=buy_amount,
            take_profit_pct=decision.take_profit_pct,
            stop_loss_pct=decision.stop_loss_pct,
            agent_reason=decision.reason,
            llm_provider=decision.llm_provider,
        )
        logger.info(
            f"[매수 완료] {decision.symbol} {units}개 @ {current_price:,.0f}원 "
            f"투입={buy_amount:,.0f}원"
        )

        # 전략 조정 타이머 + 기록 리셋
        self._last_adjust_time = time.time()
        self._last_adjustment = None

        if self._notifier:
            try:
                self._notifier.notify_buy(
                    symbol=decision.symbol,
                    price=current_price,
                    units=units,
                    krw_amount=buy_amount,
                    reason=decision.reason,
                    take_profit_pct=decision.take_profit_pct,
                    stop_loss_pct=decision.stop_loss_pct,
                    llm_provider=decision.llm_provider,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Step 4: 익절/손절 감시 + 동적 전략 조정                               #
    # ------------------------------------------------------------------ #
    def _check_exit(self, position: Position) -> None:
        try:
            current_price = self._client.get_current_price(position.symbol)
            self._price_fail_count.pop(position.id, None)
        except Exception as e:
            fail_count = self._price_fail_count.get(position.id, 0) + 1
            self._price_fail_count[position.id] = fail_count
            logger.error(f"[시세 조회 실패] {position.symbol} ({fail_count}/5): {e}")
            if fail_count >= 5:
                logger.warning(f"[강제 청산 시도] {position.symbol} 시세 5회 연속 조회 실패")
                try:
                    units = self._client.get_coin_balance(position.symbol)
                    if units > 0:
                        self._client.market_sell(position.symbol, units)
                except Exception as sell_err:
                    logger.error(f"[강제 청산 실패] {position.symbol}: {sell_err}")
                self._repo.close_position(position.id)
                self._price_fail_count.pop(position.id, None)
            return

        pnl_pct = (current_price - position.buy_price) / position.buy_price * 100

        logger.debug(
            f"[감시] {position.symbol} "
            f"매수가={position.buy_price:,.0f} 현재가={current_price:,.0f} "
            f"수익={pnl_pct:+.2f}% "
            f"익절=+{position.take_profit_pct}% 손절={position.stop_loss_pct}%"
        )

        # 시간 기반 강제 탈출 체크
        holding_minutes = (datetime.utcnow() - position.opened_at).total_seconds() / 60
        if holding_minutes >= _MAX_HOLD_MINUTES:
            self._execute_sell(
                position, current_price, pnl_pct,
                f"시간초과 강제매도 ({holding_minutes:.0f}분 >= {_MAX_HOLD_MINUTES}분, {pnl_pct:+.2f}%)",
            )
            return

        # 익절/손절 체크
        if pnl_pct >= position.take_profit_pct:
            self._execute_sell(position, current_price, pnl_pct,
                               f"익절 ({pnl_pct:+.2f}% >= +{position.take_profit_pct}%)")
        elif pnl_pct <= position.stop_loss_pct:
            self._execute_sell(position, current_price, pnl_pct,
                               f"손절 ({pnl_pct:+.2f}% <= {position.stop_loss_pct}%)")
        else:
            # 보유 중 — 주기적으로 전략 동적 조정
            self._maybe_adjust_strategy(position, current_price, pnl_pct)

    def _maybe_adjust_strategy(
        self, position: Position, current_price: float, pnl_pct: float,
    ) -> None:
        """30분 간격으로 AI에게 전략 조정 질의"""
        now = time.time()
        if now - self._last_adjust_time < _ADJUST_INTERVAL_SEC:
            return

        holding_minutes = int(
            (datetime.utcnow() - position.opened_at).total_seconds() / 60
        )

        # 보유 30분 미만이면 조정 스킵
        if holding_minutes < 30:
            return

        self._last_adjust_time = now

        try:
            result = self._agent.should_adjust_strategy(
                symbol=position.symbol,
                buy_price=position.buy_price,
                current_price=current_price,
                current_pnl_pct=pnl_pct,
                holding_minutes=holding_minutes,
                original_tp=position.take_profit_pct,
                original_sl=position.stop_loss_pct,
            )

            if result.get("adjust"):
                new_tp = result["new_take_profit_pct"]
                new_sl = result["new_stop_loss_pct"]
                reason = result.get("reason", "AI 동적 조정")

                logger.info(
                    f"[전략 조정] {position.symbol} "
                    f"익절 +{position.take_profit_pct}% → +{new_tp}%, "
                    f"손절 {position.stop_loss_pct}% → {new_sl}% "
                    f"({reason})"
                )

                # Position 업데이트 + 조정 기록 보존
                self._update_position_targets(
                    position.id, new_tp, new_sl, reason,
                )
                self._last_adjustment = {
                    "adjusted_tp_pct": new_tp,
                    "adjusted_sl_pct": new_sl,
                    "adjustment_reason": reason,
                }

                if self._notifier:
                    try:
                        self._notifier.send(
                            f"🔄 <b>전략 조정</b> {position.symbol}\n"
                            f"익절: +{position.take_profit_pct}% → +{new_tp}%\n"
                            f"손절: {position.stop_loss_pct}% → {new_sl}%\n"
                            f"사유: {reason}"
                        )
                    except Exception:
                        pass
            else:
                logger.debug(f"[전략 유지] {position.symbol} ({result.get('reason', '')})")

        except Exception as e:
            logger.error(f"[전략 조정 오류] {e}")

    def _update_position_targets(
        self, position_id: int, new_tp: float, new_sl: float, reason: str,
    ) -> None:
        """포지션의 익절/손절 기준을 DB에서 업데이트"""
        from database.models import SessionLocal, Position as PositionModel
        from contextlib import contextmanager

        db = SessionLocal()
        try:
            pos = db.query(PositionModel).filter(PositionModel.id == position_id).first()
            if pos:
                pos.take_profit_pct = new_tp
                pos.stop_loss_pct = new_sl
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    #  매도 실행 + 성과 평가                                                #
    # ------------------------------------------------------------------ #
    def _execute_sell(
        self,
        position: Position,
        current_price: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        logger.info(f"[매도 실행] {reason}")

        actual_units = self._client.get_coin_balance(position.symbol)
        if actual_units <= 0:
            logger.warning(f"[매도 스킵] {position.symbol} 실제 잔고 0 — 포지션만 종료")
            self._repo.close_position(position.id)
            return

        sell_units = actual_units
        result = None
        for attempt in range(1, 4):
            result = self._client.market_sell(position.symbol, sell_units)
            if result.get("status") == "0000":
                break
            logger.warning(f"[매도 실패 {attempt}/3] {result}")
            if attempt < 3:
                time.sleep(2)
                sell_units = self._client.get_coin_balance(position.symbol)
                if sell_units <= 0:
                    logger.info(f"[매도 스킵] {position.symbol} 잔고 0 — 이미 체결됨")
                    break

        if result is None or result.get("status") != "0000":
            logger.error(f"[매도 최종 실패] {position.symbol} — 수동 확인 필요")
            if self._notifier:
                try:
                    self._notifier.notify_error(
                        f"매도 3회 실패: {position.symbol} {sell_units}개"
                    )
                except Exception:
                    pass
            return

        krw_received = current_price * actual_units
        pnl_krw = (current_price - position.buy_price) * actual_units
        held_min = (datetime.utcnow() - position.opened_at).total_seconds() / 60

        self._repo.save_trade(
            symbol=position.symbol, side="sell",
            price=current_price, units=actual_units,
            krw_amount=krw_received, note=reason,
        )
        self._repo.close_position(position.id)
        logger.info(
            f"[매도 완료] {position.symbol} 수익={pnl_pct:+.2f}% "
            f"회수={krw_received:,.0f}원 사유={reason}"
        )
        if self._notifier:
            try:
                self._notifier.notify_sell(
                    symbol=position.symbol,
                    price=current_price,
                    pnl_pct=pnl_pct,
                    pnl_krw=pnl_krw,
                    reason=reason,
                    held_minutes=held_min,
                )
            except Exception:
                pass

        # ── 매매 후 성과 평가 (Post-Trade Review) ──
        self._run_post_trade_evaluation(position, current_price, pnl_pct, held_min, reason)

    # ------------------------------------------------------------------ #
    #  Post-Trade Evaluation                                               #
    # ------------------------------------------------------------------ #
    def _run_post_trade_evaluation(
        self,
        position: Position,
        sell_price: float,
        pnl_pct: float,
        held_minutes: float,
        reason: str,
    ) -> None:
        """매도 후 AI에게 성과 평가를 요청하고 DB에 저장"""
        try:
            if "익절" in reason:
                exit_type = "take_profit"
            elif "시간초과" in reason:
                exit_type = "timeout"
            else:
                exit_type = "stop_loss"
            eval_stats = self._repo.get_evaluation_stats(last_n=10)

            evaluation = self._agent.evaluate_trade(
                symbol=position.symbol,
                buy_price=position.buy_price,
                sell_price=sell_price,
                pnl_pct=pnl_pct,
                held_minutes=held_minutes,
                exit_type=exit_type,
                original_tp=position.take_profit_pct,
                original_sl=position.stop_loss_pct,
                agent_reason=position.agent_reason or "",
                eval_stats=eval_stats,
            )

            # 동적 조정 기록이 있으면 함께 저장
            adj = self._last_adjustment or {}
            self._repo.save_evaluation(
                position_id=position.id,
                symbol=position.symbol,
                buy_price=position.buy_price,
                sell_price=sell_price,
                pnl_pct=pnl_pct,
                held_minutes=held_minutes,
                exit_type=exit_type,
                original_tp_pct=position.take_profit_pct,
                original_sl_pct=position.stop_loss_pct,
                evaluation=evaluation.evaluation,
                suggested_tp_pct=evaluation.suggested_tp_pct,
                suggested_sl_pct=evaluation.suggested_sl_pct,
                lesson=evaluation.lesson,
                adjusted_tp_pct=adj.get("adjusted_tp_pct"),
                adjusted_sl_pct=adj.get("adjusted_sl_pct"),
                adjustment_reason=adj.get("adjustment_reason", ""),
            )

            logger.info(
                f"[성과 평가] {position.symbol} | {evaluation.evaluation} | "
                f"제안: 익절 +{evaluation.suggested_tp_pct}% 손절 {evaluation.suggested_sl_pct}% | "
                f"교훈: {evaluation.lesson}"
            )

            if self._notifier:
                try:
                    self._notifier.send(
                        f"📊 <b>매매 평가</b> {position.symbol} ({pnl_pct:+.2f}%)\n"
                        f"{evaluation.evaluation}\n"
                        f"다음 제안: 익절 +{evaluation.suggested_tp_pct}% / 손절 {evaluation.suggested_sl_pct}%\n"
                        f"💡 {evaluation.lesson}"
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[성과 평가 오류] {e}", exc_info=True)
