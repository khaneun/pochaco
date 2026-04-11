"""핵심 매매 루프 엔진 (v4.0 — 포트폴리오 기반)

기동 시점부터 아래 사이클을 무한 반복합니다:
  1. CoinSelector 사전 필터링 → AI 8개 코인 포트폴리오 선정
  2. 가용 KRW를 8등분하여 각 코인 매수
  3. 포트폴리오 종합 P&L 기반 스마트 매도 감시
     - 낙폭별 분할 매도: -1.0% → 33%, -1.5% → 33%, -2.0% → 전량
     - 트레일링 익절: TP 도달 시 고점 추적
  4. 매도 완료 → 성과 평가 → 1번으로
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config import settings
from core import BithumbClient
from database import TradeRepository
from database.models import Portfolio, Position
from strategy.ai_agent import PortfolioDecision
from strategy.agent_coordinator import AgentCoordinator
from strategy.market_analyzer import MarketAnalyzer
from strategy.strategy_optimizer import StrategyOptimizer
from strategy.coin_selector import CoinSelector
from strategy.portfolio_names import generate_name as generate_portfolio_name
from . import cooldown as cooldown_registry

logger = logging.getLogger(__name__)

# 보유 중 전략 조정 주기 (초) — 30분마다
_ADJUST_INTERVAL_SEC = 30 * 60
# 최대 보유 시간 (분) — 초과 시 강제 매도
_MAX_HOLD_MINUTES = 720  # 12시간
# 코인 매수 간격 (초) — API 레이트 리밋 방지
_BUY_INTERVAL_SEC = 0.5
# 포트폴리오 최소 코인 수 (이 이하면 생성 실패)
_MIN_PORTFOLIO_COINS = 3


# ================================================================== #
#  포트폴리오 매도 상태 머신                                              #
# ================================================================== #
class _ExitPhase(Enum):
    """매도 감시 상태"""
    MONITORING = "monitoring"        # 일반 감시 (낙폭별 분할 매도)
    TRAILING_TP = "trailing_tp"      # 익절 돌파 → 트레일링 추적


@dataclass
class _PortfolioExitTracker:
    """포트폴리오 매도 상태 추적기"""
    phase: _ExitPhase = _ExitPhase.MONITORING

    # ── 트레일링 익절 ──
    peak_pnl_pct: float = 0.0
    trail_offset_pct: float = 0.8
    trailing_since: float = 0.0
    trailing_timeout: float = 1800.0   # 30분

    # ── 낙폭별 분할 매도 상태 ──
    tier1_sold: bool = False    # -1.0% → 33% 매도 완료
    tier2_sold: bool = False    # -1.5% → 33% 추가 매도 완료
    # -2.0% → 잔여 전량 매도 (최종 손절)


class TradingEngine:
    """기동부터 종료까지 포트폴리오 매매 사이클을 관리"""

    def __init__(
        self,
        client: BithumbClient,
        repo: TradeRepository,
        agent: AgentCoordinator,
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
        self._price_fail_count: dict[str, int] = {}        # symbol → 연속 실패 횟수
        self._exit_tracker: _PortfolioExitTracker | None = None
        self._last_adjust_time: float = 0.0
        self._last_adjustment: dict | None = None
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
        logger.info("=== TradingEngine 시작 (포트폴리오 모드) ===")

        # 시작 총자산 계산
        self.daily_start_krw = self._calc_total_assets()

        # StrategyOptimizer 초기화
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
            except Exception as e:
                logger.error(f"[StrategyOptimizer 초기화 오류] {e}")

        while self._running:
            try:
                portfolio = self._repo.get_open_portfolio()

                if portfolio is None:
                    self._select_and_buy_portfolio()
                else:
                    self._check_portfolio_exit(portfolio)

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
    #  총자산 계산 헬퍼                                                     #
    # ------------------------------------------------------------------ #
    def _calc_total_assets(self) -> float:
        """KRW 잔고 + 보유 코인 평가액"""
        total = self._client.get_krw_balance()
        portfolio = self._repo.get_open_portfolio()
        if portfolio:
            positions = self._repo.get_portfolio_positions(portfolio.id)
            for pos in positions:
                try:
                    price = self._client.get_current_price(pos.symbol)
                    total += pos.units * price
                except Exception:
                    total += pos.buy_krw  # 시세 조회 실패 시 매수가 기준
        return total

    # ------------------------------------------------------------------ #
    #  미체결 주문 정리                                                     #
    # ------------------------------------------------------------------ #
    def _cancel_stuck_orders(self) -> None:
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
    #  전체 현금화                                                          #
    # ------------------------------------------------------------------ #
    def _liquidate_all(self, note: str = "현금화") -> None:
        logger.info(f"[현금화 시작] {note}")
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

        # 모든 포트폴리오/포지션 종료
        portfolio = self._repo.get_open_portfolio()
        if portfolio:
            self._repo.close_portfolio(portfolio.id)

        krw = self._client.get_krw_balance()
        logger.info(f"[현금화 완료] {'매도 없음' if not sold_any else '완료'} KRW={krw:,.0f}원")

    # ------------------------------------------------------------------ #
    #  포트폴리오 선정 및 매수                                               #
    # ------------------------------------------------------------------ #
    def _select_and_buy_portfolio(self) -> None:
        if self._paused:
            logger.info("[매수 스킵] 일시 중지 상태")
            return

        krw = self._client.get_krw_balance()
        if krw < settings.MIN_ORDER_KRW:
            logger.warning(f"[매수 스킵] KRW 잔고 부족: {krw:,.0f}원")
            self._cancel_stuck_orders()
            time.sleep(30)
            return

        logger.info("=== 포트폴리오 구성 시작 ===")
        snapshots = self._analyzer.build_market_summary(top_n=30)
        if not snapshots:
            logger.error("시장 데이터 수집 실패, 30초 후 재시도")
            time.sleep(30)
            return

        eval_stats = self._repo.get_evaluation_stats(last_n=10)

        # StrategyOptimizer 파라미터 주입
        target_tp = 2.0
        if self._optimizer:
            opt = self._optimizer.get_params()
            target_tp = opt.target_tp
            if not eval_stats:
                eval_stats = {
                    "count": 0,
                    "tp_clamp_min": opt.tp_clamp_min,
                    "tp_clamp_max": opt.tp_clamp_max,
                    "sl_clamp_min": opt.sl_clamp_min,
                    "sl_clamp_max": opt.sl_clamp_max,
                }
            else:
                eval_stats["tp_clamp_min"] = min(
                    eval_stats.get("tp_clamp_min", 1.0), opt.tp_clamp_min)
                eval_stats["tp_clamp_max"] = max(
                    eval_stats.get("tp_clamp_max", 3.5), opt.tp_clamp_max)
                eval_stats["sl_clamp_min"] = min(
                    eval_stats.get("sl_clamp_min", -2.0), opt.sl_clamp_min)
                eval_stats["sl_clamp_max"] = max(
                    eval_stats.get("sl_clamp_max", -1.0), opt.sl_clamp_max)

        # 쿨다운 심볼
        cooldown_symbols = cooldown_registry.get_cooldown_symbols()

        # CoinSelector: 사전 필터링
        filtered, coin_scores = self._selector.filter_and_rank(
            snapshots, target_tp=target_tp, cooldown_symbols=cooldown_symbols
        )
        if not filtered:
            logger.warning("[CoinSelector] 조건 충족 코인 없음 — 전체 목록으로 폴백")
            filtered = snapshots
            coin_scores = []

        # AI 포트폴리오 선정
        decision: PortfolioDecision = self._agent.select_portfolio(
            filtered, eval_stats=eval_stats, coin_scores=coin_scores,
            krw_balance=krw,
        )
        symbols_str = ", ".join(c.symbol for c in decision.coins)
        logger.info(
            f"[포트폴리오 선정] [{symbols_str}] "
            f"TP=+{decision.take_profit_pct}% SL={decision.stop_loss_pct}% "
            f"확신도={decision.confidence:.0%}"
        )

        # 투자 비율
        invest_ratio = getattr(self._agent, "last_invest_ratio", 0.95)
        total_invest = krw * invest_ratio
        per_coin_amount = total_invest / len(decision.coins)

        # 포트폴리오 이름 생성
        portfolio_name = generate_portfolio_name()

        # 포트폴리오 DB 생성
        portfolio = self._repo.open_portfolio(
            name=portfolio_name,
            total_buy_krw=total_invest,
            take_profit_pct=decision.take_profit_pct,
            stop_loss_pct=decision.stop_loss_pct,
            agent_reason=decision.portfolio_reason,
            llm_provider=decision.llm_provider,
        )

        # ── 8개 코인 순차 매수 ──
        bought_count = 0
        for coin in decision.coins:
            try:
                result = self._client.market_buy(coin.symbol, per_coin_amount)
                if result.get("status") != "0000":
                    logger.warning(f"[매수 실패] {coin.symbol}: {result}")
                    continue

                time.sleep(_BUY_INTERVAL_SEC)
                units = self._client.get_coin_balance(coin.symbol)
                current_price = self._client.get_current_price(coin.symbol)

                self._repo.save_trade(
                    symbol=coin.symbol, side="buy",
                    price=current_price, units=units,
                    krw_amount=per_coin_amount, note=coin.reason,
                    portfolio_id=portfolio.id,
                )
                self._repo.open_position(
                    portfolio_id=portfolio.id,
                    symbol=coin.symbol,
                    units=units,
                    buy_price=current_price,
                    buy_krw=per_coin_amount,
                    agent_reason=coin.reason,
                )
                bought_count += 1
                logger.info(
                    f"  [{bought_count}/{len(decision.coins)}] "
                    f"{coin.symbol} {units}개 @ {current_price:,.0f}원"
                )
            except Exception as e:
                logger.error(f"[매수 오류] {coin.symbol}: {e}")

        if bought_count < _MIN_PORTFOLIO_COINS:
            logger.error(
                f"[포트폴리오 실패] {bought_count}개만 매수 (최소 {_MIN_PORTFOLIO_COINS}개) "
                f"— 전체 청산 후 재시도"
            )
            self._liquidate_all("포트폴리오 구성 실패")
            time.sleep(30)
            return

        logger.info(
            f"[포트폴리오 매수 완료] '{portfolio_name}' "
            f"{bought_count}/{len(decision.coins)}개 코인 | "
            f"총 투입={total_invest:,.0f}원"
        )

        # 상태 초기화
        self._last_adjust_time = time.time()
        self._last_adjustment = None
        self._exit_tracker = _PortfolioExitTracker()

        if self._notifier:
            try:
                coin_list = "\n".join(
                    f"  • {c.symbol} ({c.reason})" for c in decision.coins[:bought_count]
                )
                self._notifier.send(
                    f"📦 <b>포트폴리오 매수 완료</b> '{portfolio_name}'\n"
                    f"코인 {bought_count}개 | 투입 {total_invest:,.0f}원\n"
                    f"TP +{decision.take_profit_pct}% / SL {decision.stop_loss_pct}%\n"
                    f"{coin_list}"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  포트폴리오 종합 P&L 계산                                              #
    # ------------------------------------------------------------------ #
    def _calc_portfolio_pnl(
        self, positions: list[Position],
    ) -> tuple[float, float, list[dict]]:
        """포트폴리오 종합 P&L 계산

        Returns:
            (pnl_pct, pnl_krw, coin_details)
            coin_details: [{symbol, buy_price, buy_krw, current_price, current_value, pnl_pct, units}]
        """
        total_buy = 0.0
        total_current = 0.0
        coin_details = []

        for pos in positions:
            try:
                current_price = self._client.get_current_price(pos.symbol)
                current_value = pos.units * current_price
                self._price_fail_count.pop(pos.symbol, None)
            except Exception as e:
                fail_count = self._price_fail_count.get(pos.symbol, 0) + 1
                self._price_fail_count[pos.symbol] = fail_count
                logger.warning(f"[시세 조회 실패] {pos.symbol} ({fail_count}회): {e}")
                current_price = pos.buy_price
                current_value = pos.buy_krw

            coin_pnl_pct = (
                (current_price - pos.buy_price) / pos.buy_price * 100
                if pos.buy_price > 0 else 0.0
            )

            total_buy += pos.buy_krw
            total_current += current_value

            coin_details.append({
                "symbol": pos.symbol,
                "buy_price": pos.buy_price,
                "buy_krw": pos.buy_krw,
                "current_price": current_price,
                "current_value": current_value,
                "pnl_pct": round(coin_pnl_pct, 2),
                "units": pos.units,
            })

        pnl_pct = (
            (total_current - total_buy) / total_buy * 100
            if total_buy > 0 else 0.0
        )
        pnl_krw = total_current - total_buy

        return round(pnl_pct, 4), round(pnl_krw, 0), coin_details

    # ------------------------------------------------------------------ #
    #  포트폴리오 매도 감시 (상태 머신)                                       #
    # ------------------------------------------------------------------ #
    def _check_portfolio_exit(self, portfolio: Portfolio) -> None:
        positions = self._repo.get_portfolio_positions(portfolio.id)
        if not positions:
            logger.warning(f"[포트폴리오 비어있음] '{portfolio.name}' — 종료 처리")
            self._repo.close_portfolio(portfolio.id)
            self._exit_tracker = None
            return

        pnl_pct, pnl_krw, coin_details = self._calc_portfolio_pnl(positions)
        tracker = self._exit_tracker or _PortfolioExitTracker()

        # 시간 기반 강제 탈출
        holding_minutes = (datetime.utcnow() - portfolio.opened_at).total_seconds() / 60
        if holding_minutes >= _MAX_HOLD_MINUTES:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"시간초과 강제매도 ({holding_minutes:.0f}분, {pnl_pct:+.2f}%)",
            )
            return

        # ── 상태 머신 분기 ──
        if tracker.phase == _ExitPhase.TRAILING_TP:
            self._handle_trailing_tp(portfolio, positions, pnl_pct, coin_details, tracker)
        else:
            self._handle_monitoring(portfolio, positions, pnl_pct, coin_details, tracker, holding_minutes)

        self._exit_tracker = tracker

    def _handle_monitoring(
        self, portfolio: Portfolio, positions: list[Position],
        pnl_pct: float, coin_details: list[dict],
        tracker: _PortfolioExitTracker, holding_minutes: float,
    ) -> None:
        """일반 감시 — 낙폭별 분할 매도 + 트레일링 익절 진입"""

        logger.debug(
            f"[감시] '{portfolio.name}' 종합={pnl_pct:+.2f}% "
            f"TP=+{portfolio.take_profit_pct}% SL={portfolio.stop_loss_pct}% "
            f"{'[T1]' if tracker.tier1_sold else ''}{'[T2]' if tracker.tier2_sold else ''}"
        )

        # ── 익절 돌파 → 트레일링 모드 ──
        if pnl_pct >= portfolio.take_profit_pct:
            tracker.phase = _ExitPhase.TRAILING_TP
            tracker.peak_pnl_pct = pnl_pct
            tracker.trailing_since = time.time()
            tracker.trail_offset_pct = self._calc_trail_offset(pnl_pct, portfolio.take_profit_pct)
            logger.info(
                f"[트레일링 진입] '{portfolio.name}' {pnl_pct:+.2f}% >= TP +{portfolio.take_profit_pct}%"
            )
            if self._notifier:
                try:
                    self._notifier.send(
                        f"🎣 <b>트레일링 익절 진입</b> '{portfolio.name}'\n"
                        f"종합 수익: {pnl_pct:+.2f}% (TP +{portfolio.take_profit_pct}% 돌파)\n"
                        f"고점 추적 중... 오프셋 {tracker.trail_offset_pct}%"
                    )
                except Exception:
                    pass

        # ── Tier 3: -2.0% 전량 매도 (최종 손절) ──
        elif pnl_pct <= portfolio.stop_loss_pct:
            logger.warning(
                f"[최종 손절] '{portfolio.name}' {pnl_pct:+.2f}% <= SL {portfolio.stop_loss_pct}%"
            )
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"최종 손절 ({pnl_pct:+.2f}% <= SL {portfolio.stop_loss_pct}%)",
            )

        # ── Tier 2: -1.5% → 33% 추가 매도 ──
        elif not tracker.tier2_sold and tracker.tier1_sold and pnl_pct <= -1.5:
            logger.warning(f"[2차 분할 매도] '{portfolio.name}' {pnl_pct:+.2f}% <= -1.5%")
            self._execute_portfolio_partial_sell(
                portfolio, positions, ratio=0.5,  # 잔여의 50% ≈ 원래의 33%
                reason=f"2차 분할 매도 ({pnl_pct:+.2f}% <= -1.5%)",
            )
            tracker.tier2_sold = True

        # ── Tier 1: -1.0% → 33% 매도 ──
        elif not tracker.tier1_sold and pnl_pct <= -1.0:
            logger.warning(f"[1차 분할 매도] '{portfolio.name}' {pnl_pct:+.2f}% <= -1.0%")
            self._execute_portfolio_partial_sell(
                portfolio, positions, ratio=0.33,
                reason=f"1차 분할 매도 ({pnl_pct:+.2f}% <= -1.0%)",
            )
            tracker.tier1_sold = True

        else:
            # 보유 중 — 주기적으로 전략 동적 조정
            self._maybe_adjust_strategy(
                portfolio, pnl_pct, coin_details, tracker, holding_minutes
            )

    def _handle_trailing_tp(
        self, portfolio: Portfolio, positions: list[Position],
        pnl_pct: float, coin_details: list[dict],
        tracker: _PortfolioExitTracker,
    ) -> None:
        """트레일링 익절 — 고점 추적, 하락 시 매도"""
        if pnl_pct > tracker.peak_pnl_pct:
            tracker.peak_pnl_pct = pnl_pct
            tracker.trail_offset_pct = self._calc_trail_offset(pnl_pct, portfolio.take_profit_pct)

        drop_from_peak = tracker.peak_pnl_pct - pnl_pct
        elapsed = time.time() - tracker.trailing_since

        logger.debug(
            f"[트레일링] '{portfolio.name}' 현재={pnl_pct:+.2f}% "
            f"고점={tracker.peak_pnl_pct:+.2f}% 하락={drop_from_peak:.2f}%"
        )

        if drop_from_peak >= tracker.trail_offset_pct:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"트레일링 익절 (고점 {tracker.peak_pnl_pct:+.2f}% → {pnl_pct:+.2f}%)",
            )
        elif elapsed >= tracker.trailing_timeout:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"트레일링 타임아웃 ({elapsed:.0f}초, {pnl_pct:+.2f}%)",
            )
        elif pnl_pct < portfolio.take_profit_pct * 0.2:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"트레일링 모멘텀 상실 ({pnl_pct:+.2f}% < TP의 20%)",
            )

    # ------------------------------------------------------------------ #
    #  포트폴리오 분할 매도                                                  #
    # ------------------------------------------------------------------ #
    def _execute_portfolio_partial_sell(
        self,
        portfolio: Portfolio,
        positions: list[Position],
        ratio: float,
        reason: str,
    ) -> None:
        """8개 코인 각각 ratio만큼 분할 매도"""
        logger.info(f"[분할 매도] '{portfolio.name}' {ratio*100:.0f}% | {reason}")

        for pos in positions:
            try:
                actual_units = self._client.get_coin_balance(pos.symbol)
                if actual_units <= 0:
                    continue

                sell_units = actual_units * ratio
                current_price = self._client.get_current_price(pos.symbol)
                krw_value = sell_units * current_price

                if krw_value < settings.MIN_ORDER_KRW:
                    logger.debug(f"  {pos.symbol} 소액({krw_value:.0f}원) 스킵")
                    continue

                result = self._client.market_sell(pos.symbol, sell_units)
                if result.get("status") == "0000":
                    self._repo.save_trade(
                        symbol=pos.symbol, side="sell",
                        price=current_price, units=sell_units,
                        krw_amount=krw_value, note=reason,
                        portfolio_id=portfolio.id,
                    )
                    # 잔여 수량·투입금액 업데이트 — P&L 기준 보정
                    remaining_units = actual_units - sell_units
                    remaining_ratio = remaining_units / actual_units if actual_units > 0 else 0.0
                    remaining_buy_krw = pos.buy_krw * remaining_ratio
                    self._repo.update_position_after_partial_sell(
                        pos.id, remaining_units, remaining_buy_krw,
                    )
                    logger.info(
                        f"  {pos.symbol} {sell_units:.6f}개 매도 ({krw_value:,.0f}원) "
                        f"잔여={remaining_units:.6f}개 ({remaining_buy_krw:,.0f}원 기준)"
                    )
                else:
                    logger.warning(f"  {pos.symbol} 분할 매도 실패: {result}")
            except Exception as e:
                logger.error(f"  {pos.symbol} 분할 매도 오류: {e}")

        if self._notifier:
            try:
                self._notifier.send(
                    f"⚠️ <b>분할 매도</b> '{portfolio.name}'\n"
                    f"{reason}\n보유량 {ratio*100:.0f}% 매도 완료"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  포트폴리오 전량 매도 + 종료                                           #
    # ------------------------------------------------------------------ #
    def _execute_portfolio_sell(
        self,
        portfolio: Portfolio,
        positions: list[Position],
        pnl_pct: float,
        coin_details: list[dict],
        reason: str,
    ) -> None:
        """8개 코인 전량 매도 → 포트폴리오 종료"""
        logger.info(f"[포트폴리오 매도] '{portfolio.name}' | {reason}")

        total_sell_krw = 0.0
        coin_results = []

        for pos in positions:
            try:
                actual_units = self._client.get_coin_balance(pos.symbol)
                if actual_units <= 0:
                    # 이미 분할 매도로 전부 팔림
                    coin_results.append({
                        "symbol": pos.symbol,
                        "buy_price": pos.buy_price,
                        "buy_krw": pos.buy_krw,
                        "sell_price": pos.buy_price,
                        "sell_krw": 0,
                        "pnl_pct": 0,
                        "reason": pos.agent_reason or "",
                    })
                    self._repo.close_position(pos.id)
                    continue

                current_price = self._client.get_current_price(pos.symbol)
                krw_value = actual_units * current_price

                result = self._client.market_sell(pos.symbol, actual_units)
                if result.get("status") == "0000":
                    self._repo.save_trade(
                        symbol=pos.symbol, side="sell",
                        price=current_price, units=actual_units,
                        krw_amount=krw_value, note=reason,
                        portfolio_id=portfolio.id,
                    )
                    total_sell_krw += krw_value
                    logger.info(f"  {pos.symbol} 전량 매도 ({krw_value:,.0f}원)")
                else:
                    logger.warning(f"  {pos.symbol} 매도 실패: {result}")
                    total_sell_krw += pos.buy_krw  # 실패 시 매수가 기준

                coin_pnl = (
                    (current_price - pos.buy_price) / pos.buy_price * 100
                    if pos.buy_price > 0 else 0
                )
                coin_results.append({
                    "symbol": pos.symbol,
                    "buy_price": pos.buy_price,
                    "buy_krw": pos.buy_krw,
                    "sell_price": current_price,
                    "sell_krw": krw_value,
                    "pnl_pct": round(coin_pnl, 2),
                    "reason": pos.agent_reason or "",
                })
                self._repo.close_position(pos.id)
                time.sleep(_BUY_INTERVAL_SEC)  # API 간격
            except Exception as e:
                logger.error(f"  {pos.symbol} 매도 오류: {e}")
                self._repo.close_position(pos.id)

        # 포트폴리오 종료
        self._repo.close_portfolio(portfolio.id)
        self._exit_tracker = None

        held_min = (datetime.utcnow() - portfolio.opened_at).total_seconds() / 60

        # ── 쿨다운 등록 (8개 코인 모두) ──
        exit_type_for_cd = "take_profit" if "익절" in reason else "stop_loss"
        for pos in positions:
            cooldown_registry.record_sell(pos.symbol, exit_type_for_cd)

        pnl_krw = total_sell_krw - portfolio.total_buy_krw
        logger.info(
            f"[포트폴리오 매도 완료] '{portfolio.name}' "
            f"수익={pnl_pct:+.2f}% ({pnl_krw:+,.0f}원) | {reason}"
        )

        if self._notifier:
            try:
                coin_summary = "\n".join(
                    f"  {'✅' if cr['pnl_pct'] >= 0 else '❌'} {cr['symbol']} {cr['pnl_pct']:+.2f}%"
                    for cr in coin_results
                )
                self._notifier.send(
                    f"{'💰' if pnl_pct >= 0 else '📉'} <b>포트폴리오 매도</b> '{portfolio.name}'\n"
                    f"종합: {pnl_pct:+.2f}% ({pnl_krw:+,.0f}원)\n"
                    f"{coin_summary}\n"
                    f"사유: {reason}"
                )
            except Exception:
                pass

        # ── 성과 평가 ──
        self._run_post_trade_evaluation(
            portfolio, total_sell_krw, pnl_pct, held_min, reason, coin_results,
        )

    # ------------------------------------------------------------------ #
    #  트레일링 오프셋 계산                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _calc_trail_offset(current_pnl: float, original_tp: float) -> float:
        if current_pnl >= 15.0:
            return 2.5
        elif current_pnl >= 10.0:
            return 1.8
        elif current_pnl >= 7.0:
            return 1.2
        elif current_pnl >= 5.0:
            return 0.8
        else:
            return 0.5

    # ------------------------------------------------------------------ #
    #  전략 동적 조정 (30분 간격)                                           #
    # ------------------------------------------------------------------ #
    def _maybe_adjust_strategy(
        self, portfolio: Portfolio, pnl_pct: float,
        coin_details: list[dict], tracker: _PortfolioExitTracker,
        holding_minutes: float,
    ) -> None:
        now = time.time()
        if now - self._last_adjust_time < _ADJUST_INTERVAL_SEC:
            return
        if holding_minutes < 30:
            return

        self._last_adjust_time = now

        try:
            result = self._agent.should_adjust_strategy(
                portfolio_name=portfolio.name,
                combined_pnl_pct=pnl_pct,
                holding_minutes=int(holding_minutes),
                original_tp=portfolio.take_profit_pct,
                original_sl=portfolio.stop_loss_pct,
                coin_details=coin_details,
                tier1_sold=tracker.tier1_sold,
                tier2_sold=tracker.tier2_sold,
            )

            if result.get("adjust"):
                new_tp = result["new_take_profit_pct"]
                new_sl = result["new_stop_loss_pct"]
                reason = result.get("reason", "AI 동적 조정")

                logger.info(
                    f"[전략 조정] '{portfolio.name}' "
                    f"TP +{portfolio.take_profit_pct}% → +{new_tp}%, "
                    f"SL {portfolio.stop_loss_pct}% → {new_sl}% ({reason})"
                )

                self._repo.update_portfolio_targets(portfolio.id, new_tp, new_sl)
                self._last_adjustment = {
                    "adjusted_tp_pct": new_tp,
                    "adjusted_sl_pct": new_sl,
                    "adjustment_reason": reason,
                }

                if self._notifier:
                    try:
                        self._notifier.send(
                            f"🔄 <b>전략 조정</b> '{portfolio.name}'\n"
                            f"TP: +{portfolio.take_profit_pct}% → +{new_tp}%\n"
                            f"SL: {portfolio.stop_loss_pct}% → {new_sl}%\n"
                            f"사유: {reason}"
                        )
                    except Exception:
                        pass
            else:
                logger.debug(f"[전략 유지] '{portfolio.name}' ({result.get('reason', '')})")

        except Exception as e:
            logger.error(f"[전략 조정 오류] {e}")

    # ------------------------------------------------------------------ #
    #  Post-Trade Evaluation                                               #
    # ------------------------------------------------------------------ #
    def _run_post_trade_evaluation(
        self,
        portfolio: Portfolio,
        total_sell_krw: float,
        pnl_pct: float,
        held_minutes: float,
        reason: str,
        coin_results: list[dict],
    ) -> None:
        try:
            if "익절" in reason:
                exit_type = "take_profit"
            elif "시간초과" in reason:
                exit_type = "timeout"
            else:
                exit_type = "stop_loss"

            eval_stats = self._repo.get_evaluation_stats(last_n=10)

            evaluation = self._agent.evaluate_trade(
                portfolio_name=portfolio.name,
                total_buy_krw=portfolio.total_buy_krw,
                total_sell_krw=total_sell_krw,
                combined_pnl_pct=pnl_pct,
                held_minutes=held_minutes,
                exit_type=exit_type,
                original_tp=portfolio.take_profit_pct,
                original_sl=portfolio.stop_loss_pct,
                coin_results=coin_results,
                portfolio_reason=portfolio.agent_reason or "",
                eval_stats=eval_stats,
            )

            coins_summary_json = json.dumps(coin_results, ensure_ascii=False)

            adj = self._last_adjustment or {}
            self._repo.save_evaluation(
                portfolio_id=portfolio.id,
                portfolio_name=portfolio.name,
                total_buy_krw=portfolio.total_buy_krw,
                total_sell_krw=total_sell_krw,
                pnl_pct=pnl_pct,
                held_minutes=held_minutes,
                exit_type=exit_type,
                original_tp_pct=portfolio.take_profit_pct,
                original_sl_pct=portfolio.stop_loss_pct,
                evaluation=evaluation.evaluation,
                suggested_tp_pct=evaluation.suggested_tp_pct,
                suggested_sl_pct=evaluation.suggested_sl_pct,
                coins_summary=coins_summary_json,
                lesson=evaluation.lesson,
                adjusted_tp_pct=adj.get("adjusted_tp_pct"),
                adjusted_sl_pct=adj.get("adjusted_sl_pct"),
                adjustment_reason=adj.get("adjustment_reason", ""),
            )

            logger.info(
                f"[성과 평가] '{portfolio.name}' | {evaluation.evaluation} | "
                f"제안: TP +{evaluation.suggested_tp_pct}% SL {evaluation.suggested_sl_pct}% | "
                f"교훈: {evaluation.lesson}"
            )

            if self._notifier:
                try:
                    self._notifier.send(
                        f"📊 <b>포트폴리오 평가</b> '{portfolio.name}' ({pnl_pct:+.2f}%)\n"
                        f"{evaluation.evaluation}\n"
                        f"다음 제안: TP +{evaluation.suggested_tp_pct}% / SL {evaluation.suggested_sl_pct}%\n"
                        f"💡 {evaluation.lesson}"
                    )
                except Exception:
                    pass

            # StrategyOptimizer 즉시 재실행
            if self._optimizer:
                try:
                    updated_stats = self._repo.get_evaluation_stats(last_n=10)
                    self._optimizer.optimize(updated_stats)
                except Exception as opt_err:
                    logger.error(f"[StrategyOptimizer 재실행 오류] {opt_err}")

        except Exception as e:
            logger.error(f"[성과 평가 오류] {e}", exc_info=True)
