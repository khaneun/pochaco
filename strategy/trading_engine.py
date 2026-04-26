"""핵심 매매 루프 엔진 (v4.0 — 포트폴리오 기반)

기동 시점부터 아래 사이클을 무한 반복합니다:
  1. CoinSelector 사전 필터링 → AI 8개 코인 포트폴리오 선정
  2. 가용 KRW를 8등분하여 각 코인 매수
  3. 포트폴리오 종합 P&L 기반 스마트 매도 감시
     - 낙폭별 분할 매도: -1.0% → AI 평가 기반 비율(33~67%), -1.5% → 잔여 전량
     - 트레일링 익절: TP 도달 시 고점 추적 (0.3% 하락 시 실현)
  4. 매도 완료 → 성과 평가 → 1번으로
"""
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from config import settings
from core import BaseExchangeClient
from database import TradeRepository
from database.models import Portfolio, Position
from strategy.ai_agent import PortfolioDecision
from strategy.agent_coordinator import AgentCoordinator, InvestmentHoldError, InsufficientCandidatesError
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
# 투자 보류 시 대기 시간 (분) — 자산 운용가가 보류 결정 후 재평가까지 대기
_HOLD_WAIT_MINUTES = 30
# 지정가 매도: 시도별 가격 조정 배율 (1→0.998→0.995), 실패 시 시장가
_LIMIT_SELL_PRICE_ADJ = [1.0, 0.998, 0.995]
_LIMIT_SELL_WAIT_SEC = 5.0
# 분할 매도 손절 라인
_TIER1_SL_PCT = -1.0    # 1차 분할 매도 진입점 (AI 평가 기반 비율)
_FINAL_SL_PCT = -1.5    # 최대 손절 하드캡 (잔여 전량 매도)


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
    trough_pnl_pct: float = 0.0
    trail_offset_pct: float = 0.8
    trailing_since: float = 0.0
    trailing_timeout: float = 1800.0   # 30분

    # ── 낙폭별 분할 매도 상태 ──
    tier1_sold: bool = False    # -1.0% → AI 평가 비율 매도 완료
    # -1.5% → 잔여 전량 매도 (최종 손절 하드캡)

    # ── 피라미딩 추가 매수 ──
    pyramid_done: bool = False          # 이미 실행했거나 포기 결정된 경우
    pyramid_checked_at: float = 0.0     # 마지막 판단 시각 (time.time())


class TradingEngine:
    """기동부터 종료까지 포트폴리오 매매 사이클을 관리"""

    def __init__(
        self,
        client: BaseExchangeClient,
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
        self._last_reconcile_time: float = 0.0
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
        """KRW 잔고(available+locked) + 실제 보유 코인 평가액 (API 기준)"""
        # available+locked KRW 합산 — 미체결 주문에 묶인 KRW 포함
        try:
            detail = self._client.get_krw_balance_detail()
            total = detail["total"]
        except Exception:
            total = self._client.get_krw_balance()

        # 실제 API 잔고 기준 코인 평가액
        try:
            bal_data = self._client.get_balance("ALL")
            if bal_data.get("status") == "0000":
                for key, value in bal_data["data"].items():
                    if not key.startswith("total_"):
                        continue
                    sym = key.replace("total_", "").upper()
                    if sym == "KRW":
                        continue
                    amt = float(value)
                    if amt <= 0:
                        continue
                    try:
                        price = self._client.get_current_price(sym)
                        total += amt * price
                    except Exception:
                        pass
                return total
        except Exception:
            pass

        # fallback: DB 포지션 기준
        portfolio = self._repo.get_open_portfolio()
        if portfolio:
            positions = self._repo.get_portfolio_positions(portfolio.id)
            for pos in positions:
                try:
                    price = self._client.get_current_price(pos.symbol)
                    total += pos.units * price
                except Exception:
                    total += pos.buy_krw
        return total

    # ------------------------------------------------------------------ #
    #  지정가 매도 (retry + market fallback)                                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _floor_to_tick(price: float) -> float:
        """빗썸 KRW 마켓 호가 단위로 내림 (매도 시 체결 확률 최대화)"""
        if price >= 1_000_000:
            return math.floor(price / 1000) * 1000
        elif price >= 100_000:
            return math.floor(price / 100) * 100
        elif price >= 10_000:
            return math.floor(price / 10) * 10
        elif price >= 1_000:
            return math.floor(price)
        elif price >= 100:
            return math.floor(price * 10) / 10
        elif price >= 10:
            return math.floor(price * 100) / 100
        else:
            return math.floor(price * 1000) / 1000

    def _limit_sell_with_retry(
        self, symbol: str, target_price: float, units: float,
    ) -> dict:
        """지정가 매도 최대 3회 → 실패 시 시장가 전환

        Args:
            symbol: 코인 심볼
            target_price: 목표 매도 가격 (TP/SL 트리거 시점 시세)
            units: 매도 수량

        Returns:
            {"status", "order_uuid", "filled_price", "filled_units",
             "filled_krw", "method"}
        """
        remaining = units

        for attempt, multiplier in enumerate(_LIMIT_SELL_PRICE_ADJ, 1):
            limit_price = self._floor_to_tick(target_price * multiplier)
            if limit_price <= 0:
                break

            logger.info(
                f"  [지정가 매도 {attempt}/{len(_LIMIT_SELL_PRICE_ADJ)}] "
                f"{symbol} {remaining:.6f}개 @ {limit_price:,.0f}원"
            )

            result = self._client.limit_sell(symbol, limit_price, remaining)
            if result.get("status") != "0000":
                logger.warning(f"  [지정가 주문 실패] {symbol}: {result}")
                continue

            order_data = result.get("data", {})
            order_uuid = (
                order_data.get("uuid", "") if isinstance(order_data, dict) else ""
            )
            if not order_uuid:
                logger.warning(f"  [UUID 없음] {symbol} — 시장가 전환")
                break

            # 체결 대기
            time.sleep(_LIMIT_SELL_WAIT_SEC)

            # 주문 상태 확인
            order_info = self._client.get_order_by_uuid(order_uuid)
            if order_info and order_info.get("state") == "done":
                logger.info(
                    f"  [지정가 체결] {symbol} "
                    f"@{order_info['avg_price']:,.0f}원 "
                    f"({order_info['executed_funds']:,.0f}원)"
                )
                return {
                    "status": "0000",
                    "order_uuid": order_uuid,
                    "filled_price": order_info["avg_price"],
                    "filled_units": order_info["executed_volume"],
                    "filled_krw": order_info["executed_funds"],
                    "method": f"limit_{attempt}",
                }

            # 미체결 → 취소
            logger.info(f"  [미체결 취소] {symbol} attempt {attempt}")
            self._client.cancel_order("ask", order_uuid, symbol)
            time.sleep(0.3)

            # 부분 체결 확인
            order_info = self._client.get_order_by_uuid(order_uuid)
            if order_info and order_info.get("executed_volume", 0) > 0:
                filled_vol = order_info["executed_volume"]
                remaining -= filled_vol
                if remaining <= 0:
                    return {
                        "status": "0000",
                        "order_uuid": order_uuid,
                        "filled_price": order_info["avg_price"],
                        "filled_units": filled_vol,
                        "filled_krw": order_info["executed_funds"],
                        "method": f"limit_partial_{attempt}",
                    }
                logger.info(
                    f"  [부분 체결] {symbol} {filled_vol:.6f}개 체결, "
                    f"잔여 {remaining:.6f}개"
                )

        # ── 시장가 전환 ──
        logger.warning(f"  [시장가 전환] {symbol} {remaining:.6f}개")
        market_result = self._client.market_sell(symbol, remaining)
        order_data = market_result.get("data", {})
        order_uuid = (
            order_data.get("uuid", "") if isinstance(order_data, dict) else ""
        )

        filled_price = target_price  # 기본 fallback
        filled_krw = remaining * target_price
        if order_uuid:
            # 업비트 시장가 주문: state=wait → done 전환까지 최대 5초 폴링
            filled_price, filled_krw = self._wait_market_fill(
                order_uuid, filled_price, filled_krw
            )

        return {
            "status": market_result.get("status", "9999"),
            "order_uuid": order_uuid,
            "filled_price": filled_price,
            "filled_units": remaining,
            "filled_krw": filled_krw,
            "method": "market_fallback",
        }

    # ------------------------------------------------------------------ #
    #  시장가 체결 대기 헬퍼                                                 #
    # ------------------------------------------------------------------ #
    def _wait_market_fill(
        self, order_uuid: str, fallback_price: float, fallback_krw: float,
        max_wait: float = 5.0, interval: float = 1.0,
    ) -> tuple[float, float]:
        """시장가 주문 체결 확인 — state=done 될 때까지 최대 max_wait초 폴링.

        업비트는 시장가 주문 제출 직후 state='wait'를 반환하므로
        체결 완료(state='done') 확인 없이 진행하면 DB와 실제 잔고가 불일치함.

        Returns:
            (filled_price, filled_krw): 체결가/체결금액, 실패 시 fallback 값
        """
        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            try:
                info = self._client.get_order_by_uuid(order_uuid)
                if not info:
                    continue
                if info.get("executed_funds", 0) > 0:
                    logger.debug(
                        f"  [시장가 체결 확인] {order_uuid[:8]} "
                        f"@{info.get('avg_price', 0):,.0f}원 "
                        f"({info['executed_funds']:,.0f}원, {elapsed:.0f}초)"
                    )
                    return info.get("avg_price") or fallback_price, info["executed_funds"]
            except Exception as e:
                logger.warning(f"  [체결 확인 오류] {order_uuid[:8]}: {e}")
        logger.warning(
            f"  [시장가 체결 타임아웃] {order_uuid[:8]} {max_wait:.0f}초 후 fallback 사용"
        )
        return fallback_price, fallback_krw

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
                    # 업비트 시장가: state=wait → done 체결 확인 후 실제 금액 기록
                    order_data = result.get("data", {})
                    order_uuid = (
                        order_data.get("uuid", "") if isinstance(order_data, dict) else ""
                    )
                    filled_price, filled_krw = current_price, krw_value
                    if order_uuid:
                        filled_price, filled_krw = self._wait_market_fill(
                            order_uuid, current_price, krw_value
                        )
                    self._repo.save_trade(
                        symbol=symbol, side="sell",
                        price=filled_price, units=amount,
                        krw_amount=filled_krw, note=note,
                    )
                    sold_any = True
                    logger.info(f"  {symbol} {amount}개 → {filled_krw:,.0f}원 매도 완료")
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
        if len(filtered) < _MIN_PORTFOLIO_COINS:
            logger.warning(
                f"[CoinSelector] 후보 {len(filtered)}개 부족 "
                f"(최소 {_MIN_PORTFOLIO_COINS}개) — 전체 목록으로 폴백"
            )
            filtered = snapshots
            coin_scores = []

        # AI 포트폴리오 선정
        try:
            decision: PortfolioDecision = self._agent.select_portfolio(
                filtered, eval_stats=eval_stats, coin_scores=coin_scores,
                krw_balance=krw,
            )
        except InvestmentHoldError as e:
            hold_reason = str(e)
            logger.info(f"[투자 보류] {_HOLD_WAIT_MINUTES}분 대기: {hold_reason}")
            if self._notifier:
                try:
                    self._notifier.send(
                        f"⏸️ <b>중요 알람</b> — 투자 보류\n"
                        f"{hold_reason}\n"
                        f"⏱ {_HOLD_WAIT_MINUTES}분 후 시장 재평가"
                    )
                except Exception:
                    pass
            time.sleep(_HOLD_WAIT_MINUTES * 60)
            return
        except InsufficientCandidatesError as e:
            logger.warning(f"[후보 부족] {e} — 5분 후 재시도")
            time.sleep(5 * 60)
            return

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

        # ── 매수 전 전체 잔고 스냅샷 (before-after 차이로 실제 매수 수량 계산) ──
        try:
            balance_before = self._client.get_balance("ALL").get("data", {})
        except Exception as e:
            logger.warning(f"[매수 전 잔고 조회 실패] fallback 사용: {e}")
            balance_before = {}

        # ── 8개 코인 순차 매수 ──
        bought_count = 0
        for coin in decision.coins:
            try:
                before_units = float(
                    balance_before.get(f"available_{coin.symbol.lower()}", 0)
                )
                if before_units > 0:
                    logger.warning(
                        f"[기존 잔고 감지] {coin.symbol}: {before_units:.6f}개 보유 중 — 차이로 수량 계산"
                    )

                result = self._client.market_buy(coin.symbol, per_coin_amount)
                if result.get("status") != "0000":
                    logger.warning(f"[매수 실패] {coin.symbol}: {result}")
                    continue

                order_data = result.get("data", {})
                buy_order_uuid = order_data.get("uuid", "") if isinstance(order_data, dict) else ""

                time.sleep(_BUY_INTERVAL_SEC)
                after_units = self._client.get_coin_balance(coin.symbol)
                # 비동기 체결: 0이면 최대 3회 재시도 (체결 대기)
                if after_units <= 0:
                    for _retry in range(3):
                        time.sleep(1.0)
                        after_units = self._client.get_coin_balance(coin.symbol)
                        if after_units > 0:
                            break
                    if after_units <= 0:
                        logger.warning(
                            f"[매수 체결 미확인] {coin.symbol} — units=0, 스킵"
                        )
                        continue

                # 기존 보유분 차감으로 실제 매수된 수량만 추출
                units = after_units - before_units
                if units <= 0:
                    # fallback: 차감 결과가 0 이하면 after_units 전체 사용
                    logger.warning(
                        f"[수량 보정] {coin.symbol}: before={before_units:.6f} after={after_units:.6f} "
                        f"→ 차이 계산 실패, after_units 사용"
                    )
                    units = after_units

                # 거래소 체결가 조회 — order_uuid 있으면 실제 avg_price 우선, 없으면 투입금/수량 계산
                actual_buy_price = per_coin_amount / units  # fallback
                actual_krw = per_coin_amount
                if buy_order_uuid:
                    try:
                        order_info = self._client.get_order_by_uuid(buy_order_uuid)
                        # state=done + executed_volume>0 일 때만 사용
                        # wait 상태이면 avg_price=null → price(KRW 투입금액)로 fallback되어 단가 오인
                        if (order_info
                                and order_info.get("state") == "done"
                                and order_info.get("executed_volume", 0) > 0
                                and order_info.get("avg_price", 0) > 0):
                            actual_buy_price = order_info["avg_price"]
                            if order_info.get("executed_funds", 0) > 0:
                                actual_krw = order_info["executed_funds"]
                            logger.debug(
                                f"  [체결가 확인] {coin.symbol} "
                                f"avg={actual_buy_price:,.0f}원 실체결={actual_krw:,.0f}원"
                            )
                    except Exception as _e:
                        logger.debug(f"  [체결가 조회 실패] {coin.symbol}: {_e} — 계산값 사용")

                self._repo.save_trade(
                    symbol=coin.symbol, side="buy",
                    price=actual_buy_price, units=units,
                    krw_amount=actual_krw, note=coin.reason,
                    portfolio_id=portfolio.id,
                )
                self._repo.open_position(
                    portfolio_id=portfolio.id,
                    symbol=coin.symbol,
                    units=units,
                    buy_price=actual_buy_price,
                    buy_krw=actual_krw,
                    agent_reason=coin.reason,
                )
                bought_count += 1
                logger.info(
                    f"  [{bought_count}/{len(decision.coins)}] "
                    f"{coin.symbol} {units}개 @ {actual_buy_price:,.0f}원"
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

        # 실체결 기반 총 투입금 보정 — Trade 테이블 buy 합산이 예상 투입금과 다를 수 있음
        try:
            actual_total_buy = sum(
                t.krw_amount for t in self._repo.get_recent_trades(50)
                if t.portfolio_id == portfolio.id and t.side == "buy"
            )
            if actual_total_buy > 0 and abs(actual_total_buy - total_invest) > 100:
                self._repo.update_portfolio_total_buy(portfolio.id, actual_total_buy)
                logger.info(
                    f"[포트폴리오 투입금 보정] {total_invest:,.0f}원 → {actual_total_buy:,.0f}원"
                )
                total_invest = actual_total_buy
        except Exception as _e:
            logger.warning(f"[포트폴리오 투입금 보정 실패] {_e}")

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
        self, portfolio: Portfolio, positions: list[Position],
    ) -> tuple[float, float, list[dict]]:
        """포트폴리오 종합 P&L 계산

        분할 매도로 이미 실현된 손익까지 포함하여 실제 포트폴리오 수익률을 계산합니다.
        (기존 방식은 남은 포지션만의 수익률만 보여줘서 분할 손절 손실을 누락했음)

        Returns:
            (pnl_pct, pnl_krw, coin_details)
            coin_details: [{symbol, buy_price, buy_krw, current_price, current_value, pnl_pct, units}]
        """
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
                logger.warning(f"[시세 조회 실패] {pos.symbol} ({fail_count}회): {e} — 매수가 fallback")
                current_price = pos.buy_price
                current_value = pos.buy_krw  # 가격 조회 실패 시 P&L 0으로 처리 (최소 손실 추정)

            coin_pnl_pct = (
                (current_price - pos.buy_price) / pos.buy_price * 100
                if pos.buy_price > 0 else 0.0
            )

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

        # 분할 매도로 이미 실현된 금액 포함 → 원래 총 투자금 기준으로 실제 수익률 계산
        prior_sell_krw = self._repo.get_portfolio_sell_total(portfolio.id)
        total_proceeds_estimate = prior_sell_krw + total_current
        ref_buy = portfolio.total_buy_krw if portfolio.total_buy_krw > 0 else 1.0
        pnl_pct = (total_proceeds_estimate - ref_buy) / ref_buy * 100
        pnl_krw = total_proceeds_estimate - ref_buy

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

        # 거래소 실잔고와 DB 수량 주기적 대조·보정 (수익률 오차 방지)
        self._maybe_reconcile_positions(positions)

        pnl_pct, pnl_krw, coin_details = self._calc_portfolio_pnl(portfolio, positions)
        tracker = self._exit_tracker or _PortfolioExitTracker()

        # 매도 시 목표가 맵 (현재 시세 기준)
        target_prices = {
            d["symbol"]: d["current_price"]
            for d in coin_details
            if d.get("current_price", 0) > 0
        }

        # 시간 기반 강제 탈출
        holding_minutes = (datetime.utcnow() - portfolio.opened_at).total_seconds() / 60
        if holding_minutes >= _MAX_HOLD_MINUTES:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"시간초과 강제매도 ({holding_minutes:.0f}분, {pnl_pct:+.2f}%)",
                target_prices=target_prices,
            )
            return

        # ── 상태 머신 분기 ──
        if tracker.phase == _ExitPhase.TRAILING_TP:
            self._handle_trailing_tp(
                portfolio, positions, pnl_pct, coin_details, tracker, target_prices,
            )
        else:
            self._handle_monitoring(
                portfolio, positions, pnl_pct, coin_details, tracker,
                holding_minutes, target_prices,
            )

        self._exit_tracker = tracker

    def _handle_monitoring(
        self, portfolio: Portfolio, positions: list[Position],
        pnl_pct: float, coin_details: list[dict],
        tracker: _PortfolioExitTracker, holding_minutes: float,
        target_prices: dict[str, float] | None = None,
    ) -> None:
        """일반 감시 — 낙폭별 분할 매도 + 트레일링 익절 진입"""

        logger.debug(
            f"[감시] '{portfolio.name}' 종합={pnl_pct:+.2f}% "
            f"TP=+{portfolio.take_profit_pct}% SL={portfolio.stop_loss_pct}% "
            f"{'[T1]' if tracker.tier1_sold else ''}"
        )

        # 보유 기간 최고 수익률 갱신 (양수 신고점만)
        if pnl_pct > tracker.peak_pnl_pct:
            tracker.peak_pnl_pct = pnl_pct
            try:
                self._repo.update_portfolio_peak(portfolio.id, pnl_pct)
            except Exception as e:
                logger.warning(f"[peak 갱신 오류] {e}")

        # 보유 기간 최저 수익률 갱신 (음수 신저점만)
        if pnl_pct < tracker.trough_pnl_pct:
            tracker.trough_pnl_pct = pnl_pct
            try:
                self._repo.update_portfolio_trough(portfolio.id, pnl_pct)
            except Exception as e:
                logger.warning(f"[trough 갱신 오류] {e}")

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

        # ── 최종 손절: -1.5% 하드캡 → 잔여 전량 매도 ──
        elif pnl_pct <= _FINAL_SL_PCT:
            logger.warning(
                f"[최종 손절] '{portfolio.name}' {pnl_pct:+.2f}% <= {_FINAL_SL_PCT}%"
            )
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"최종 손절 ({pnl_pct:+.2f}% <= {_FINAL_SL_PCT}%)",
                target_prices=target_prices,
            )

        # ── Tier 1: -1.0% → AI 평가 기반 비율 매도 ──
        elif not tracker.tier1_sold and pnl_pct <= _TIER1_SL_PCT:
            sell_ratio = 0.5  # 기본값: 50%
            sell_reason = "기본"
            if self._agent:
                try:
                    ev = self._agent.evaluate_tier1_sell(
                        portfolio_name=portfolio.name,
                        pnl_pct=pnl_pct,
                        coin_details=coin_details,
                        holding_minutes=int(holding_minutes),
                    )
                    sell_ratio = ev.get("sell_ratio", 0.5)
                    sell_reason = ev.get("reason", "")
                except Exception as e:
                    logger.warning(f"[Tier1 AI 평가 실패] {e} → 기본 50% 적용")
            logger.warning(
                f"[1차 분할 매도] '{portfolio.name}' {pnl_pct:+.2f}% <= {_TIER1_SL_PCT}% "
                f"→ {sell_ratio:.0%} 매도 ({sell_reason})"
            )
            self._execute_portfolio_partial_sell(
                portfolio, positions, ratio=sell_ratio,
                reason=f"1차 분할 매도 {sell_ratio:.0%} ({pnl_pct:+.2f}%, {sell_reason})",
                target_prices=target_prices,
            )
            tracker.tier1_sold = True

        else:
            # 보유 중 — 주기적으로 전략 동적 조정
            self._maybe_adjust_strategy(
                portfolio, pnl_pct, coin_details, tracker, holding_minutes
            )
            # 플러스 국면 피라미딩 검토 (분할 매도 미발생, 아직 실행 전)
            if pnl_pct > 0 and not tracker.pyramid_done and not tracker.tier1_sold:
                self._maybe_pyramid(portfolio, positions, pnl_pct, holding_minutes, tracker)

    def _handle_trailing_tp(
        self, portfolio: Portfolio, positions: list[Position],
        pnl_pct: float, coin_details: list[dict],
        tracker: _PortfolioExitTracker,
        target_prices: dict[str, float] | None = None,
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
                target_prices=target_prices,
            )
        elif elapsed >= tracker.trailing_timeout:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"트레일링 타임아웃 ({elapsed:.0f}초, {pnl_pct:+.2f}%)",
                target_prices=target_prices,
            )
        elif pnl_pct < portfolio.take_profit_pct * 0.2:
            self._execute_portfolio_sell(
                portfolio, positions, pnl_pct, coin_details,
                f"트레일링 모멘텀 상실 ({pnl_pct:+.2f}% < TP의 20%)",
                target_prices=target_prices,
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
        target_prices: dict[str, float] | None = None,
    ) -> None:
        """8개 코인 각각 ratio만큼 분할 매도 (지정가 3회 → 시장가)"""
        logger.info(f"[분할 매도] '{portfolio.name}' {ratio*100:.0f}% | {reason}")

        for pos in positions:
            try:
                actual_units = self._client.get_coin_balance(pos.symbol)
                if actual_units <= 0:
                    continue

                sell_units = actual_units * ratio

                # 목표 매도가 결정
                tgt_price = (target_prices or {}).get(pos.symbol, 0)
                if tgt_price <= 0:
                    try:
                        tgt_price = self._client.get_current_price(pos.symbol)
                    except Exception:
                        tgt_price = pos.buy_price

                krw_est = sell_units * tgt_price
                if krw_est < settings.MIN_ORDER_KRW:
                    logger.debug(f"  {pos.symbol} 소액({krw_est:.0f}원) 스킵")
                    continue

                fill = self._limit_sell_with_retry(pos.symbol, tgt_price, sell_units)
                if fill["status"] == "0000":
                    self._repo.save_trade(
                        symbol=pos.symbol, side="sell",
                        price=fill["filled_price"],
                        units=fill["filled_units"],
                        krw_amount=fill["filled_krw"],
                        note=f"{reason} [{fill['method']}]",
                        order_id=fill["order_uuid"],
                        portfolio_id=portfolio.id,
                        target_price=tgt_price,
                    )
                    remaining_units = actual_units - fill["filled_units"]
                    remaining_ratio = remaining_units / actual_units if actual_units > 0 else 0.0
                    remaining_buy_krw = pos.buy_krw * remaining_ratio
                    self._repo.update_position_after_partial_sell(
                        pos.id, remaining_units, remaining_buy_krw,
                    )
                    logger.info(
                        f"  {pos.symbol} {fill['filled_units']:.6f}개 매도 "
                        f"({fill['filled_krw']:,.0f}원, 체결가={fill['filled_price']:,.0f}원, "
                        f"목표가={tgt_price:,.0f}원) [{fill['method']}]"
                    )
                else:
                    logger.warning(f"  {pos.symbol} 분할 매도 실패")
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
        target_prices: dict[str, float] | None = None,
    ) -> None:
        """8개 코인 전량 매도 → 포트폴리오 종료 (지정가 3회 → 시장가)"""
        logger.info(f"[포트폴리오 매도] '{portfolio.name}' | {reason}")

        # 분할 매도가 먼저 실행된 경우 이미 확보된 수익 포함
        prior_sell_krw = self._repo.get_portfolio_sell_total(portfolio.id)

        total_sell_krw = 0.0
        coin_results = []

        # 직전 감시 루프에서 계산된 가격 — 목표가 소스
        detail_price_map: dict[str, float] = {
            d["symbol"]: d["current_price"]
            for d in coin_details
            if d.get("current_price", 0) > 0
        }
        # 호출자가 전달한 target_prices 우선, 없으면 detail_price_map 사용
        tgt_map = target_prices or detail_price_map

        for pos in positions:
            try:
                actual_units = self._client.get_coin_balance(pos.symbol)
                if actual_units <= 0:
                    # 이미 분할 매도로 전부 팔림 — Trade 테이블에서 원매수금·실제 수익 조회
                    # pos.buy_krw는 분할 매도 후 잔여분 비례로 줄어든 값이므로 직접 사용 불가
                    coin_sell_krw = self._repo.get_coin_sell_total(portfolio.id, pos.symbol)
                    original_buy_krw = self._repo.get_coin_buy_total(portfolio.id, pos.symbol) or pos.buy_krw
                    coin_pnl = (
                        (coin_sell_krw - original_buy_krw) / original_buy_krw * 100
                        if original_buy_krw > 0 else 0.0
                    )
                    coin_results.append({
                        "symbol": pos.symbol,
                        "buy_price": pos.buy_price,
                        "buy_krw": round(original_buy_krw, 0),
                        "sell_price": 0,
                        "sell_krw": round(coin_sell_krw, 0),
                        "pnl_pct": round(coin_pnl, 2),
                        "pnl_krw": round(coin_sell_krw - original_buy_krw, 0),
                        "target_price": tgt_map.get(pos.symbol, 0),
                        "reason": pos.agent_reason or "",
                    })
                    self._repo.close_position(pos.id)
                    continue

                # 목표 매도가 결정
                tgt_price = tgt_map.get(pos.symbol, 0)
                if tgt_price <= 0:
                    try:
                        tgt_price = self._client.get_current_price(pos.symbol)
                    except Exception:
                        tgt_price = detail_price_map.get(pos.symbol, 0) or pos.buy_price

                fill = self._limit_sell_with_retry(pos.symbol, tgt_price, actual_units)
                if fill["status"] == "0000":
                    krw_value = fill["filled_krw"]
                    filled_price = fill["filled_price"]

                    self._repo.save_trade(
                        symbol=pos.symbol, side="sell",
                        price=filled_price,
                        units=fill["filled_units"],
                        krw_amount=krw_value,
                        note=f"{reason} [{fill['method']}]",
                        order_id=fill["order_uuid"],
                        portfolio_id=portfolio.id,
                        target_price=tgt_price,
                    )
                    total_sell_krw += krw_value
                    logger.info(
                        f"  {pos.symbol} 전량 매도 ({krw_value:,.0f}원, "
                        f"체결가={filled_price:,.0f}원, 목표가={tgt_price:,.0f}원) "
                        f"[{fill['method']}]"
                    )
                else:
                    # 매도 실패 — 실제로 받은 금액 없으므로 total_sell_krw에 가산하지 않음
                    # (get_portfolio_sell_total이 실제 Trade 합계만 집계하므로 일관성 유지)
                    logger.warning(f"  {pos.symbol} 매도 실패 — 손익 집계에서 제외")
                    filled_price = 0.0
                    total_sell_krw += 0.0

                # 분할 매도 포함 코인 전체 손익 — pos.buy_krw는 잔여분 기준이므로 원매수금 조회
                # ※ get_coin_sell_total은 방금 save_trade로 저장된 현재 매도 포함이므로
                #   krw_value를 별도로 더하지 않아야 이중 계산이 방지됨
                original_buy_krw = self._repo.get_coin_buy_total(portfolio.id, pos.symbol)
                total_coin_sell = self._repo.get_coin_sell_total(portfolio.id, pos.symbol)
                coin_pnl = (
                    (total_coin_sell - original_buy_krw) / original_buy_krw * 100
                    if original_buy_krw > 0 else 0.0
                )
                coin_results.append({
                    "symbol": pos.symbol,
                    "buy_price": pos.buy_price,
                    "buy_krw": round(original_buy_krw, 0),
                    "sell_price": filled_price,
                    "sell_krw": round(total_coin_sell, 0),
                    "pnl_pct": round(coin_pnl, 2),
                    "pnl_krw": round(total_coin_sell - original_buy_krw, 0),
                    "target_price": tgt_price,
                    "units": fill.get("filled_units", actual_units),
                    "reason": pos.agent_reason or "",
                })
                self._repo.close_position(pos.id)
                time.sleep(_BUY_INTERVAL_SEC)
            except Exception as e:
                logger.error(f"  {pos.symbol} 매도 오류: {e}")
                # 예외 발생 코인도 coins_summary에 기록 (누락 방지)
                try:
                    _orig_buy = self._repo.get_coin_buy_total(portfolio.id, pos.symbol) or pos.buy_krw
                    _coin_sell = self._repo.get_coin_sell_total(portfolio.id, pos.symbol)
                    _cpnl = (_coin_sell - _orig_buy) / _orig_buy * 100 if _orig_buy > 0 else 0.0
                    coin_results.append({
                        "symbol": pos.symbol,
                        "buy_price": pos.buy_price,
                        "buy_krw": round(_orig_buy, 0),
                        "sell_price": 0.0,
                        "sell_krw": round(_coin_sell, 0),
                        "pnl_pct": round(_cpnl, 2),
                        "pnl_krw": round(_coin_sell - _orig_buy, 0),
                        "target_price": 0.0,
                        "reason": pos.agent_reason or "",
                        "error": True,
                    })
                except Exception:
                    pass
                self._repo.close_position(pos.id)

        # 포트폴리오 종료
        self._repo.close_portfolio(portfolio.id)
        self._exit_tracker = None

        held_min = (datetime.utcnow() - portfolio.opened_at).total_seconds() / 60

        # ── 쿨다운 등록 (8개 코인 모두) ──
        exit_type_for_cd = "take_profit" if "익절" in reason else "stop_loss"
        for pos in positions:
            cooldown_registry.record_sell(pos.symbol, exit_type_for_cd)

        # 분할 매도 수익(prior_sell_krw)까지 포함한 실제 총 손익
        total_proceeds = total_sell_krw + prior_sell_krw
        pnl_krw = total_proceeds - portfolio.total_buy_krw
        pnl_pct_actual = (
            pnl_krw / portfolio.total_buy_krw * 100
            if portfolio.total_buy_krw > 0 else 0.0
        )
        logger.info(
            f"[포트폴리오 매도 완료] '{portfolio.name}' "
            f"수익={pnl_pct_actual:+.2f}% ({pnl_krw:+,.0f}원) | {reason}"
        )

        if self._notifier:
            try:
                coin_summary = "\n".join(
                    f"  {'✅' if cr['pnl_pct'] >= 0 else '❌'} {cr['symbol']} {cr['pnl_pct']:+.2f}%"
                    for cr in coin_results
                )
                # 트리거 사유와 실제 결과가 다를 수 있음을 명시
                triggered_by = "익절트리거" if "익절" in reason else "손절트리거"
                result_label = "실현이익" if pnl_pct_actual >= 0 else "실현손실"
                self._notifier.send(
                    f"{'💰' if pnl_pct_actual >= 0 else '📉'} <b>포트폴리오 매도</b> '{portfolio.name}'\n"
                    f"종합: {pnl_pct_actual:+.2f}% ({pnl_krw:+,.0f}원) [{result_label}]\n"
                    f"{coin_summary}\n"
                    f"트리거: {triggered_by} ({reason})"
                )
            except Exception:
                pass

        # ── 청산 시점 총 자산 조회 (KRW 잔고 + 매도 수익 기반 추정) ──
        closing_total_assets = None
        try:
            krw_bal = self._client.get_krw_balance()
            closing_total_assets = krw_bal  # 매도 직후 KRW 잔고 ≈ 총 자산
        except Exception:
            pass

        # ── 성과 평가 ──
        self._run_post_trade_evaluation(
            portfolio, total_proceeds, pnl_pct_actual, held_min, reason, coin_results,
            closing_total_assets_krw=closing_total_assets,
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
            return 0.3

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
    #  포지션 수량 reconciliation (거래소 실잔고 ↔ DB 보정)                    #
    # ------------------------------------------------------------------ #
    _RECONCILE_INTERVAL_SEC = 5 * 60        # 5분마다 실잔고 대조

    def _maybe_reconcile_positions(self, positions: list[Position]) -> None:
        """거래소 실잔고와 DB 포지션 수량을 주기적으로 대조하고 오차 보정.

        분할 매도 슬리피지·수수료 등으로 pos.units가 실제 잔고와 달라지면
        pnl_pct 계산이 틀어지므로 5분마다 보정합니다.
        """
        now = time.time()
        if now - self._last_reconcile_time < self._RECONCILE_INTERVAL_SEC:
            return
        self._last_reconcile_time = now

        try:
            balance_data = self._client.get_balance("ALL").get("data", {})
        except Exception as e:
            logger.debug(f"[수량 보정] 잔고 조회 실패: {e}")
            return

        for pos in positions:
            try:
                key = pos.symbol.lower()
                # total_ = available + locked (미체결 매도 주문 포함) → 실제 보유량과 일치
                # available_ 만 쓰면 미체결 매도 주문 중인 수량이 0으로 보여 P&L 오차 발생
                actual_units = float(balance_data.get(f"total_{key}", 0))
                avg_buy_price_str = balance_data.get(f"avg_buy_price_{key}")
                actual_buy_price = float(avg_buy_price_str) if avg_buy_price_str else 0.0

                if actual_units <= 0 or pos.units <= 0:
                    continue

                units_diff = abs(actual_units - pos.units) / pos.units

                # 수량 감소 방향만 보정 — buy_price 및 수량 증가는 건드리지 않음
                # - 감소: 부분 매도 후 잔여 수량 반영 → 보정 필요
                # - 증가: 계좌에 dust 코인(이전 포지션 잔여)이 섞인 것 → 보정 금지
                #   (증가 보정 시 (units*price - buy_krw)/buy_krw 계산이 왜곡됨)
                if actual_units < pos.units and units_diff > 0.005:
                    logger.warning(
                        f"[포지션 수량 보정] {pos.symbol}: "
                        f"{pos.units:.6f}→{actual_units:.6f} ({units_diff*100:.2f}%, 부분매도 반영)"
                    )
                    self._repo.update_position_units(pos.id, actual_units)
                    pos.units = actual_units
            except Exception as e:
                logger.debug(f"[포지션 보정 오류] {pos.symbol}: {e}")

    # ------------------------------------------------------------------ #
    #  피라미딩 추가 매수                                                     #
    # ------------------------------------------------------------------ #
    _PYRAMID_CHECK_INTERVAL_SEC = 15 * 60   # 15분마다 판단 재요청

    def _maybe_pyramid(
        self,
        portfolio: Portfolio,
        positions: list[Position],
        pnl_pct: float,
        holding_minutes: float,
        tracker: _PortfolioExitTracker,
    ) -> None:
        """플러스 국면 피라미딩 — 자산 운용가 판단 후 조건 충족 시 추가 매수"""
        if not self._agent:
            return

        now = time.time()
        # 첫 판단은 15분 보유 후, 이후 15분 주기
        if holding_minutes < 15:
            return
        if now - tracker.pyramid_checked_at < self._PYRAMID_CHECK_INTERVAL_SEC:
            return

        tracker.pyramid_checked_at = now

        try:
            krw = self._client.get_krw_balance()
        except Exception as e:
            logger.warning(f"[피라미딩] KRW 조회 실패: {e}")
            return

        decision = self._agent.decide_pyramid({
            "current_pnl_pct": pnl_pct,
            "peak_pnl_pct": tracker.peak_pnl_pct,
            "total_buy_krw": portfolio.total_buy_krw,
            "take_profit_pct": portfolio.take_profit_pct,
            "krw_available": krw,
            "coin_count": len(positions),
            "held_minutes": holding_minutes,
        })

        if not decision.should_pyramid:
            logger.info(f"[피라미딩 보류] '{portfolio.name}': {decision.reason}")
            tracker.pyramid_done = True
            return

        if pnl_pct < decision.threshold_pct:
            logger.info(
                f"[피라미딩 대기] '{portfolio.name}' 현재={pnl_pct:+.2f}% "
                f"< 최소={decision.threshold_pct:.1f}% — 다음 주기 재확인"
            )
            # threshold 미달 시 done 처리하지 않고 다음 주기에 재확인
            return

        # ── 추가 매수 실행 ──
        add_total_krw = portfolio.total_buy_krw * decision.add_ratio
        if add_total_krw > krw * 0.95:
            add_total_krw = krw * 0.95   # 가용 KRW 초과 방지
        if add_total_krw < 5_000:
            logger.warning(f"[피라미딩 스킵] 추가 투입 금액 부족 ({add_total_krw:,.0f}원)")
            tracker.pyramid_done = True
            return

        per_coin_add = add_total_krw / len(positions)
        bought = 0

        logger.info(
            f"[피라미딩 실행] '{portfolio.name}' {pnl_pct:+.2f}% "
            f"추가투입={add_total_krw:,.0f}원 ({decision.add_ratio:.0%}) / {decision.reason}"
        )

        for pos in positions:
            try:
                result = self._client.market_buy(pos.symbol, per_coin_add)
                if result.get("status") != "0000":
                    logger.warning(f"[피라미딩 매수 실패] {pos.symbol}: {result}")
                    continue

                pyr_order_data = result.get("data", {})
                pyr_order_uuid = pyr_order_data.get("uuid", "") if isinstance(pyr_order_data, dict) else ""

                time.sleep(_BUY_INTERVAL_SEC)
                add_units = self._client.get_coin_balance(pos.symbol) - pos.units
                if add_units <= 0:
                    for _ in range(3):
                        time.sleep(1.0)
                        cur_units = self._client.get_coin_balance(pos.symbol)
                        add_units = cur_units - pos.units
                        if add_units > 0:
                            break

                if add_units <= 0:
                    logger.warning(f"[피라미딩 체결 미확인] {pos.symbol} — 스킵")
                    continue

                # 거래소 체결가 조회 — 실체결가 우선, 없으면 계산값 fallback
                actual_add_price = per_coin_add / add_units
                actual_add_krw = per_coin_add
                if pyr_order_uuid:
                    try:
                        pyr_info = self._client.get_order_by_uuid(pyr_order_uuid)
                        if (pyr_info
                                and pyr_info.get("state") == "done"
                                and pyr_info.get("executed_volume", 0) > 0
                                and pyr_info.get("avg_price", 0) > 0):
                            actual_add_price = pyr_info["avg_price"]
                            if pyr_info.get("executed_funds", 0) > 0:
                                actual_add_krw = pyr_info["executed_funds"]
                    except Exception as _e:
                        logger.debug(f"  [피라미딩 체결가 조회 실패] {pos.symbol}: {_e}")

                self._repo.save_trade(
                    symbol=pos.symbol, side="buy",
                    price=actual_add_price, units=add_units,
                    krw_amount=actual_add_krw,
                    note=f"피라미딩 추가매수 ({pnl_pct:+.2f}%)",
                    portfolio_id=portfolio.id,
                )
                self._repo.pyramid_position(
                    portfolio_id=portfolio.id,
                    symbol=pos.symbol,
                    add_units=add_units,
                    add_krw=actual_add_krw,
                )
                bought += 1
                logger.info(
                    f"  [피라미딩] {pos.symbol} +{add_units:.6g}개 "
                    f"@ {actual_add_price:,.0f}원"
                )
            except Exception as e:
                logger.error(f"[피라미딩 매수 오류] {pos.symbol}: {e}")

        tracker.pyramid_done = True

        if bought > 0 and self._notifier:
            try:
                self._notifier.send(
                    f"📈 <b>피라미딩 추가 매수</b> '{portfolio.name}'\n"
                    f"현재 수익: {pnl_pct:+.2f}% | 추가 투입: {add_total_krw:,.0f}원\n"
                    f"코인 {bought}개 추가 매수 완료\n"
                    f"사유: {decision.reason}"
                )
            except Exception:
                pass

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
        closing_total_assets_krw: float | None = None,
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

            # 분할 매도(tier1/tier2)까지 포함한 전체 매도 금액 계산 (Trade 테이블 기준)
            total_sell_all = self._repo.get_portfolio_sell_total(portfolio.id)
            if total_sell_all < total_sell_krw:
                total_sell_all = total_sell_krw

            # 실제 매도 기준으로 pnl_pct 재계산 (시세 조회 오류 영향 제거)
            actual_pnl_pct = (
                (total_sell_all - portfolio.total_buy_krw) / portfolio.total_buy_krw * 100
                if portfolio.total_buy_krw > 0 else pnl_pct
            )

            adj = self._last_adjustment or {}
            self._repo.save_evaluation(
                portfolio_id=portfolio.id,
                portfolio_name=portfolio.name,
                total_buy_krw=portfolio.total_buy_krw,
                total_sell_krw=total_sell_all,
                pnl_pct=actual_pnl_pct,
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
                closing_total_assets_krw=closing_total_assets_krw,
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
