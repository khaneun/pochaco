"""핵심 매매 루프 엔진

기동 시점부터 아래 사이클을 무한 반복합니다:
  1. CoinSelector 사전 필터링 → AI Agent 코인 선정
  2. 가용 KRW 전액 매수
  3. 스마트 익절/손절 감시 (트레일링 + 손절 관찰 + 동적 조정)
  4. 조건 충족 시 전량 시장가 매도 → 성과 평가 → 1번으로
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config import settings
from core import BithumbClient
from database import TradeRepository
from database.models import Position
from strategy.ai_agent import TradingAgent
from strategy.market_analyzer import MarketAnalyzer
from strategy.strategy_optimizer import StrategyOptimizer
from strategy.coin_selector import CoinSelector
from . import cooldown as cooldown_registry

logger = logging.getLogger(__name__)

# 보유 중 전략 조정 주기 (초) — 30분마다
_ADJUST_INTERVAL_SEC = 30 * 60
# 최대 보유 시간 (분) — 초과 시 강제 매도
_MAX_HOLD_MINUTES = 720  # 12시간


# ================================================================== #
#  스마트 매도 상태 머신                                                #
# ================================================================== #
class _ExitPhase(Enum):
    """매도 감시 상태"""
    MONITORING = "monitoring"        # 일반 감시 중 (2단계 손절 포함)
    TRAILING_TP = "trailing_tp"      # 익절 돌파 → 트레일링 추적


@dataclass
class _ExitTracker:
    """포지션별 매도 상태 추적기"""
    phase: _ExitPhase = _ExitPhase.MONITORING

    # ── 트레일링 익절 ──
    peak_pnl_pct: float = 0.0          # 트레일링 진입 후 최고 수익률
    trail_offset_pct: float = 0.4      # 고점 대비 이만큼 하락하면 매도
    trailing_since: float = 0.0        # 트레일링 진입 시각 (epoch)
    trailing_timeout: float = 600.0    # 트레일링 최대 유지 시간 (초, 기본 10분)

    # ── 2단계 손절 ──
    sl1_executed: bool = False         # 1차 손절(50% 매도) 완료 여부


class TradingEngine:
    """기동부터 종료까지 매매 사이클을 단일 루프로 관리"""

    def __init__(
        self,
        client: BithumbClient,
        repo: TradeRepository,
        agent: TradingAgent,
        analyzer: MarketAnalyzer,
        optimizer: StrategyOptimizer | None = None,
        selector: CoinSelector | None = None,
    ):
        self._client = client
        self._repo = repo
        self._agent = agent
        self._analyzer = analyzer
        self._optimizer = optimizer
        self._selector = selector or CoinSelector()
        self._running = False
        self._paused = False
        self._notifier = None
        self._price_fail_count: dict[int, int] = {}  # position_id → 연속 실패 횟수
        self._exit_trackers: dict[int, _ExitTracker] = {}  # position_id → 매도 상태
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

        # StrategyOptimizer 초기화 (기존 평가 데이터로 즉시 파라미터 결정)
        if self._optimizer:
            try:
                init_stats = self._repo.get_evaluation_stats(last_n=10)
                if init_stats:
                    self._optimizer.optimize(init_stats)
                    p = self._optimizer.get_params()
                    logger.info(
                        f"[StrategyOptimizer] 초기 파라미터: "
                        f"익절 {p.tp_clamp_min}~{p.tp_clamp_max}% "
                        f"/ 손절 {p.sl_clamp_min}~{p.sl_clamp_max}% "
                        f"| {p.rationale}"
                    )
                else:
                    logger.info("[StrategyOptimizer] 과거 데이터 없음 — 기본 파라미터(익절 1~3.5%, 손절 -2~-6%) 사용")
            except Exception as e:
                logger.error(f"[StrategyOptimizer 초기화 오류] {e}")

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

        # StrategyOptimizer 파라미터를 clamp 범위로 주입
        # eval_stats가 있으면 merge (repository suggested + optimizer 보정)
        # eval_stats가 없으면 optimizer 기본값으로 clamp 구성
        target_tp = 2.0
        if self._optimizer:
            opt = self._optimizer.get_params()
            target_tp = opt.target_tp
            if not eval_stats:
                # 초기 상태: optimizer 기본값으로 clamp 직접 구성
                eval_stats = {
                    "count": 0,
                    "tp_clamp_min": opt.tp_clamp_min,
                    "tp_clamp_max": opt.tp_clamp_max,
                    "sl_clamp_min": opt.sl_clamp_min,
                    "sl_clamp_max": opt.sl_clamp_max,
                }
            else:
                # repository clamp과 optimizer clamp을 merge
                # optimizer의 범위와 repository의 범위 중 더 넓은 쪽 채택
                eval_stats["tp_clamp_min"] = min(
                    eval_stats.get("tp_clamp_min", 1.0), opt.tp_clamp_min)
                eval_stats["tp_clamp_max"] = max(
                    eval_stats.get("tp_clamp_max", 3.5), opt.tp_clamp_max)
                eval_stats["sl_clamp_min"] = min(
                    eval_stats.get("sl_clamp_min", -6.0), opt.sl_clamp_min)
                eval_stats["sl_clamp_max"] = max(
                    eval_stats.get("sl_clamp_max", -2.0), opt.sl_clamp_max)
            logger.info(
                f"[StrategyOptimizer] 파라미터 주입: "
                f"익절 {eval_stats['tp_clamp_min']}~{eval_stats['tp_clamp_max']}% "
                f"/ 손절 {eval_stats['sl_clamp_min']}~{eval_stats['sl_clamp_max']}%"
            )

        # ── 쿨다운 심볼 조회 (자동 익손절 + 수동 청산 모두 포함) ──
        cooldown_symbols = cooldown_registry.get_cooldown_symbols()

        # CoinSelector: 변동성·모멘텀 기반 사전 필터링
        filtered, coin_scores = self._selector.filter_and_rank(
            snapshots, target_tp=target_tp, cooldown_symbols=cooldown_symbols
        )
        if not filtered:
            logger.warning("[CoinSelector] 조건 충족 코인 없음 — 전체 목록으로 폴백")
            filtered = snapshots
            coin_scores = []

        decision = self._agent.select_coin(filtered, eval_stats=eval_stats, coin_scores=coin_scores)
        logger.info(
            f"[AI 선정] {decision.symbol} "
            f"익절=+{decision.take_profit_pct}% "
            f"1차SL={decision.stop_loss_1st_pct}% 2차SL={decision.stop_loss_pct}% "
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
            stop_loss_1st_pct=decision.stop_loss_1st_pct,
            stop_loss_pct=decision.stop_loss_pct,
            agent_reason=decision.reason,
            llm_provider=decision.llm_provider,
        )
        logger.info(
            f"[매수 완료] {decision.symbol} {units}개 @ {current_price:,.0f}원 "
            f"투입={buy_amount:,.0f}원"
        )

        # 전략 조정 타이머 + 매도 상태 리셋
        self._last_adjust_time = time.time()
        self._last_adjustment = None
        self._exit_trackers.clear()

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
                    stop_loss_1st_pct=decision.stop_loss_1st_pct,
                    llm_provider=decision.llm_provider,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Step 4: 스마트 익절/손절 감시 (상태 머신)                              #
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
                self._exit_trackers.pop(position.id, None)
            return

        pnl_pct = (current_price - position.buy_price) / position.buy_price * 100
        tracker = self._exit_trackers.get(position.id, _ExitTracker())

        # 시간 기반 강제 탈출 체크 (모든 상태에서 적용)
        holding_minutes = (datetime.utcnow() - position.opened_at).total_seconds() / 60
        if holding_minutes >= _MAX_HOLD_MINUTES:
            self._execute_sell(
                position, current_price, pnl_pct,
                f"시간초과 강제매도 ({holding_minutes:.0f}분 >= {_MAX_HOLD_MINUTES}분, {pnl_pct:+.2f}%)",
            )
            self._exit_trackers.pop(position.id, None)
            return

        # ── 상태 머신 분기 ──
        if tracker.phase == _ExitPhase.TRAILING_TP:
            self._handle_trailing_tp(position, current_price, pnl_pct, tracker)
        else:
            self._handle_monitoring(position, current_price, pnl_pct, tracker)

        # 상태 저장
        self._exit_trackers[position.id] = tracker

    def _handle_monitoring(
        self, position: Position, current_price: float, pnl_pct: float, tracker: _ExitTracker,
    ) -> None:
        """일반 감시 상태 — 2단계 손절 + 트레일링 익절"""
        # 1차 손절 기준: position.stop_loss_1st_pct, 없으면 SL2의 80% (더 위)
        sl_1st = (
            position.stop_loss_1st_pct
            if position.stop_loss_1st_pct
            else round(position.stop_loss_pct * 0.8, 2)
        )

        logger.debug(
            f"[감시] {position.symbol} "
            f"매수가={position.buy_price:,.0f} 현재가={current_price:,.0f} "
            f"수익={pnl_pct:+.2f}% "
            f"익절=+{position.take_profit_pct}% "
            f"1차SL={sl_1st}% 2차SL={position.stop_loss_pct}% "
            f"{'[1차실행]' if tracker.sl1_executed else ''}"
        )

        if pnl_pct >= position.take_profit_pct:
            # ── 익절 돌파 → 트레일링 모드 진입 ──
            tracker.phase = _ExitPhase.TRAILING_TP
            tracker.peak_pnl_pct = pnl_pct
            tracker.trailing_since = time.time()
            tracker.trail_offset_pct = self._calc_trail_offset(pnl_pct, position.take_profit_pct)
            logger.info(
                f"[트레일링 진입] {position.symbol} {pnl_pct:+.2f}% >= TP +{position.take_profit_pct}% "
                f"| 오프셋={tracker.trail_offset_pct}% (고점 대비 이만큼 하락 시 매도)"
            )
            if self._notifier:
                try:
                    self._notifier.send(
                        f"🎣 <b>트레일링 익절 진입</b> {position.symbol}\n"
                        f"수익: {pnl_pct:+.2f}% (TP +{position.take_profit_pct}% 돌파)\n"
                        f"고점 추적 중... 하락 시 매도 (오프셋 {tracker.trail_offset_pct}%)"
                    )
                except Exception:
                    pass

        elif tracker.sl1_executed and pnl_pct <= position.stop_loss_pct:
            # ── 2차 손절 도달 → 나머지 전량 매도 ──
            logger.warning(
                f"[2차 손절] {position.symbol} {pnl_pct:+.2f}% <= SL2 {position.stop_loss_pct}%"
            )
            self._execute_sell(
                position, current_price, pnl_pct,
                f"2차 손절 ({pnl_pct:+.2f}% <= SL2 {position.stop_loss_pct}%)",
            )
            self._exit_trackers.pop(position.id, None)

        elif not tracker.sl1_executed and pnl_pct <= sl_1st:
            # ── 1차 손절 도달 → 50% 매도, 나머지 대기 ──
            logger.warning(
                f"[1차 손절] {position.symbol} {pnl_pct:+.2f}% <= SL1 {sl_1st}%"
            )
            self._execute_partial_sell(position, current_price, pnl_pct, ratio=0.5)
            tracker.sl1_executed = True

        else:
            # 보유 중 — 주기적으로 전략 동적 조정
            self._maybe_adjust_strategy(position, current_price, pnl_pct)

    def _handle_trailing_tp(
        self, position: Position, current_price: float, pnl_pct: float, tracker: _ExitTracker,
    ) -> None:
        """트레일링 익절 상태 — 고점 추적, 하락 시 매도"""
        # 고점 갱신
        if pnl_pct > tracker.peak_pnl_pct:
            tracker.peak_pnl_pct = pnl_pct
            # 수익 커지면 오프셋도 동적 조정 (더 여유)
            tracker.trail_offset_pct = self._calc_trail_offset(pnl_pct, position.take_profit_pct)

        drop_from_peak = tracker.peak_pnl_pct - pnl_pct
        elapsed = time.time() - tracker.trailing_since

        logger.debug(
            f"[트레일링] {position.symbol} 현재={pnl_pct:+.2f}% "
            f"고점={tracker.peak_pnl_pct:+.2f}% 하락={drop_from_peak:.2f}% "
            f"오프셋={tracker.trail_offset_pct}% 경과={elapsed:.0f}초"
        )

        if drop_from_peak >= tracker.trail_offset_pct:
            # ── 고점에서 하락 → 매도 (낚시: 줄을 끊음) ──
            self._execute_sell(
                position, current_price, pnl_pct,
                f"트레일링 익절 (고점 {tracker.peak_pnl_pct:+.2f}% → {pnl_pct:+.2f}%, "
                f"하락 {drop_from_peak:.2f}% >= 오프셋 {tracker.trail_offset_pct}%)",
            )
            self._exit_trackers.pop(position.id, None)

        elif elapsed >= tracker.trailing_timeout:
            # ── 타임아웃 → 현재 가격으로 매도 ──
            self._execute_sell(
                position, current_price, pnl_pct,
                f"트레일링 타임아웃 ({elapsed:.0f}초, {pnl_pct:+.2f}%, "
                f"고점 {tracker.peak_pnl_pct:+.2f}%)",
            )
            self._exit_trackers.pop(position.id, None)

        elif pnl_pct < position.take_profit_pct * 0.5:
            # ── TP 이하로 크게 하락 → 즉시 매도 (모멘텀 상실) ──
            self._execute_sell(
                position, current_price, pnl_pct,
                f"트레일링 모멘텀 상실 ({pnl_pct:+.2f}% < TP의 50%)",
            )
            self._exit_trackers.pop(position.id, None)

    def _execute_partial_sell(
        self,
        position: Position,
        current_price: float,
        pnl_pct: float,
        ratio: float = 0.5,
    ) -> None:
        """포지션 일부(ratio 비율) 시장가 매도 — 1차 손절 전용"""
        logger.info(
            f"[1차 손절 실행] {position.symbol} "
            f"수익={pnl_pct:+.2f}% | {ratio*100:.0f}% 매도"
        )

        actual_units = self._client.get_coin_balance(position.symbol)
        if actual_units <= 0:
            logger.warning(f"[1차 손절 스킵] {position.symbol} 잔고 0")
            return

        sell_units = actual_units * ratio
        result = None
        for attempt in range(1, 4):
            result = self._client.market_sell(position.symbol, sell_units)
            if result.get("status") == "0000":
                break
            logger.warning(f"[1차 손절 실패 {attempt}/3] {result}")
            if attempt < 3:
                time.sleep(2)
                sell_units = self._client.get_coin_balance(position.symbol) * ratio

        if result is None or result.get("status") != "0000":
            logger.error(f"[1차 손절 최종 실패] {position.symbol} — 수동 확인 필요")
            if self._notifier:
                try:
                    self._notifier.notify_error(
                        f"1차 손절 3회 실패: {position.symbol} {sell_units}개"
                    )
                except Exception:
                    pass
            return

        krw_sold = current_price * sell_units
        self._repo.save_trade(
            symbol=position.symbol, side="sell",
            price=current_price, units=sell_units,
            krw_amount=krw_sold,
            note=f"1차 손절 ({pnl_pct:+.2f}%, {ratio*100:.0f}% 매도)",
        )

        logger.info(
            f"[1차 손절 완료] {position.symbol} {sell_units:.6f}개 @ {current_price:,.0f}원 "
            f"| 수익: {pnl_pct:+.2f}% | 회수: {krw_sold:,.0f}원"
        )
        if self._notifier:
            try:
                self._notifier.send(
                    f"⚠️ <b>1차 손절 실행</b> {position.symbol}\n"
                    f"현재 수익: {pnl_pct:+.2f}% (1차 SL 도달)\n"
                    f"보유량 50% 매도 완료 — 나머지 50% 반등 대기\n"
                    f"2차 손절: {position.stop_loss_pct}%"
                )
            except Exception:
                pass

    @staticmethod
    def _calc_trail_offset(current_pnl: float, original_tp: float) -> float:
        """트레일링 오프셋 계산 — 오버슈트가 클수록 여유를 줌"""
        overshoot = current_pnl - original_tp
        if overshoot > 2.0:
            return 1.0    # 2%+ 초과 → 넉넉하게 (큰 수익 보존)
        elif overshoot > 1.0:
            return 0.7
        elif overshoot > 0.3:
            return 0.5
        else:
            return 0.3    # 갓 돌파 → 타이트하게 (수익 확보)

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
            tracker = self._exit_trackers.get(position.id, _ExitTracker())
            result = self._agent.should_adjust_strategy(
                symbol=position.symbol,
                buy_price=position.buy_price,
                current_price=current_price,
                current_pnl_pct=pnl_pct,
                holding_minutes=holding_minutes,
                original_tp=position.take_profit_pct,
                original_sl=position.stop_loss_pct,
                original_sl_1st=position.stop_loss_1st_pct,
                sl1_executed=tracker.sl1_executed,
            )

            if result.get("adjust"):
                new_tp = result["new_take_profit_pct"]
                new_sl = result["new_stop_loss_pct"]
                new_sl_1st = result.get("new_stop_loss_1st_pct")
                reason = result.get("reason", "AI 동적 조정")

                logger.info(
                    f"[전략 조정] {position.symbol} "
                    f"익절 +{position.take_profit_pct}% → +{new_tp}%, "
                    f"1차SL {position.stop_loss_1st_pct}% → {new_sl_1st}%, "
                    f"2차SL {position.stop_loss_pct}% → {new_sl}% "
                    f"({reason})"
                )

                # Position 업데이트 + 조정 기록 보존
                self._update_position_targets(
                    position.id, new_tp, new_sl, reason,
                    new_sl_1st=new_sl_1st,
                )
                self._last_adjustment = {
                    "adjusted_tp_pct": new_tp,
                    "adjusted_sl_pct": new_sl,
                    "adjustment_reason": reason,
                }

                if self._notifier:
                    try:
                        sl1_str = f"\n1차SL: {position.stop_loss_1st_pct}% → {new_sl_1st}%" if new_sl_1st else ""
                        self._notifier.send(
                            f"🔄 <b>전략 조정</b> {position.symbol}\n"
                            f"익절: +{position.take_profit_pct}% → +{new_tp}%"
                            f"{sl1_str}\n"
                            f"2차SL: {position.stop_loss_pct}% → {new_sl}%\n"
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
        new_sl_1st: float | None = None,
    ) -> None:
        """포지션의 익절/손절 기준을 DB에서 업데이트"""
        from database.models import SessionLocal, Position as PositionModel

        db = SessionLocal()
        try:
            pos = db.query(PositionModel).filter(PositionModel.id == position_id).first()
            if pos:
                pos.take_profit_pct = new_tp
                pos.stop_loss_pct = new_sl
                if new_sl_1st is not None:
                    pos.stop_loss_1st_pct = new_sl_1st
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
        # sl1_executed 상태를 먼저 저장 후 tracker 제거
        _tracker_snap = self._exit_trackers.get(position.id, _ExitTracker())
        _sl1_was_executed = _tracker_snap.sl1_executed
        self._exit_trackers.pop(position.id, None)

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

        # ── 쿨다운 등록 ──
        exit_type_for_cd = "take_profit" if "익절" in reason else "stop_loss"
        cooldown_registry.record_sell(position.symbol, exit_type_for_cd)

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
        self._run_post_trade_evaluation(
            position, current_price, pnl_pct, held_min, reason,
            sl1_was_executed=_sl1_was_executed,
        )

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
        sl1_was_executed: bool = False,
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
            partial_executed = sl1_was_executed

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
                original_sl_1st=position.stop_loss_1st_pct,
                partial_sl_executed=partial_executed,
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
                original_sl_1st_pct=position.stop_loss_1st_pct,
                evaluation=evaluation.evaluation,
                suggested_tp_pct=evaluation.suggested_tp_pct,
                suggested_sl_pct=evaluation.suggested_sl_pct,
                suggested_sl_1st_pct=evaluation.suggested_sl_1st_pct,
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

            # ── 평가 완료 즉시 StrategyOptimizer 재실행 → 다음 사이클에 즉시 반영 ──
            if self._optimizer:
                try:
                    updated_stats = self._repo.get_evaluation_stats(last_n=10)
                    new_params = self._optimizer.optimize(updated_stats)
                    if self._notifier:
                        try:
                            self._notifier.send(
                                f"⚙️ <b>전략 최적화 완료</b>\n"
                                f"익절 목표: +{new_params.target_tp}% "
                                f"({new_params.tp_clamp_min}~{new_params.tp_clamp_max}%)\n"
                                f"손절 목표: {new_params.target_sl}% "
                                f"({new_params.sl_clamp_min}~{new_params.sl_clamp_max}%)\n"
                                f"📝 {new_params.rationale}"
                            )
                        except Exception:
                            pass
                except Exception as opt_err:
                    logger.error(f"[StrategyOptimizer 재실행 오류] {opt_err}")

        except Exception as e:
            logger.error(f"[성과 평가 오류] {e}", exc_info=True)
