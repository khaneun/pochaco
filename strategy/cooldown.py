"""쿨다운 레지스트리 — 매도 후 동일 종목 재매수 방지 (v4.4 strategy_evaluations 기반)

TradingEngine(자동 익손절)과 WebDashboard(수동 청산) 모두 이 모듈을 통해
쿨다운을 기록·조회합니다. 두 컴포넌트가 같은 프로세스 내 다른 스레드로
동작하므로 모듈 싱글턴 + Lock으로 안전하게 공유됩니다.

쿨다운 시간 (v4.4):
  take_profit : 6시간   — 익절 후 동일 코인 재진입 차단 (30분 → 6h, 패턴 반복 방지)
  stop_loss   : 12시간  — 손절 직후 재매수가 손실 누적의 주원인
  manual      : 60분    — 사용자가 직접 개입했으므로 가장 보수적으로 적용

DB 기반 손실 차단 (rebuild_loss_blacklist, v4.4 재설계):
  - strategy_evaluations.coins_summary JSON을 파싱하여 코인별 손익 집계
  - 7일 내 손실(pnl_pct<0) 포트폴리오에 N회 이상 포함된 코인 → 차단
  - 누적 손실액이 임계 이하인 코인 → 장기 차단
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_COOLDOWN_MINUTES: dict[str, float] = {
    "take_profit": 6 * 60.0,    # 6시간 (v4.4: 30분 → 6h, 익절 후 동일 코인 단기 재진입 차단)
    "stop_loss":   12 * 60.0,   # 12시간 (직전 손절 패턴 재진입 강력 차단)
    "manual":      60.0,
}

# symbol → 쿨다운 만료 시각 (epoch)
_cooldowns: dict[str, float] = {}
# DB 기반 블랙리스트 (별도 관리)
_blacklist: dict[str, tuple[float, str]] = {}   # symbol → (만료 epoch, 사유)
_lock = threading.Lock()


def record_sell(symbol: str, exit_type: str) -> None:
    """매도 후 쿨다운 등록

    Args:
        symbol: 코인 심볼 (예: 'BTC')
        exit_type: 'take_profit' | 'stop_loss' | 'manual'
    """
    minutes = _COOLDOWN_MINUTES.get(exit_type, 30.0)
    expiry = time.time() + minutes * 60

    with _lock:
        existing = _cooldowns.get(symbol, 0)
        _cooldowns[symbol] = max(existing, expiry)

    logger.info(
        f"[쿨다운 등록] {symbol} ({exit_type}) → {minutes:.0f}분 재매수 금지"
    )


def add_to_blacklist(symbol: str, days: float, reason: str) -> None:
    """DB 분석 결과 기반 장기 블랙리스트 등록 (반복 손실 코인 차단)

    Args:
        symbol: 코인 심볼
        days: 차단 일수
        reason: 차단 사유 (로깅용)
    """
    expiry = time.time() + days * 86400
    with _lock:
        existing_expiry, _ = _blacklist.get(symbol, (0, ""))
        if expiry > existing_expiry:
            _blacklist[symbol] = (expiry, reason)
    logger.info(f"[블랙리스트 등록] {symbol}: {days:.1f}일 차단 — {reason}")


def get_cooldown_symbols() -> set[str]:
    """현재 쿨다운 중인 심볼 집합 반환 (만료된 항목 자동 정리)

    쿨다운 + 블랙리스트 모두 포함하여 반환.
    """
    now = time.time()
    expired = []
    expired_bl = []
    result = set()

    with _lock:
        for symbol, expiry in _cooldowns.items():
            if now < expiry:
                result.add(symbol)
                logger.debug(f"[쿨다운 중] {symbol} 잔여 {(expiry-now)/60:.0f}분")
            else:
                expired.append(symbol)
        for symbol in expired:
            del _cooldowns[symbol]

        for symbol, (expiry, reason) in _blacklist.items():
            if now < expiry:
                result.add(symbol)
                logger.debug(
                    f"[블랙리스트] {symbol} 잔여 {(expiry-now)/86400:.1f}일 ({reason})"
                )
            else:
                expired_bl.append(symbol)
        for symbol in expired_bl:
            del _blacklist[symbol]

    return result


def get_blacklist_snapshot() -> dict[str, tuple[float, str]]:
    """현재 블랙리스트 사본 반환 (대시보드/디버그용)"""
    with _lock:
        return dict(_blacklist)


def rebuild_loss_blacklist(repo, days: int = 7) -> int:
    """strategy_evaluations 기반 코인별 손실 집계 → 블랙리스트 갱신 (v4.4 재설계)

    매 사이클 호출되어도 안전(idempotent). 기존 블랙리스트와 만료시점을 비교하여
    더 긴 차단만 적용한다.

    임계값:
      - 손실(pnl_pct<0) 포트폴리오에 3회 이상 포함된 코인 → 5일 차단
      - 손실 포트폴리오 2회 + 1번이라도 -2% 이상 폭락 → 3일 차단
      - 익절 포트폴리오에 한 번이라도 포함된 코인은 손실 카운트 -1 가감 (회복 가능 신호)

    Args:
        repo: TradeRepository 인스턴스
        days: 분석 기간 (기본 7일)

    Returns:
        새로 차단되거나 갱신된 코인 수
    """
    try:
        import json as _json
        from database.models import StrategyEvaluation

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # 코인별 손익 집계
        coin_stats: dict[str, dict] = {}

        with repo._session() as db:
            evals = (
                db.query(StrategyEvaluation)
                .filter(StrategyEvaluation.created_at >= cutoff)
                .all()
            )
            for ev in evals:
                summary = ev.coins_summary or ""
                try:
                    coins = _json.loads(summary) if summary else []
                except Exception:
                    coins = []
                if not isinstance(coins, list):
                    continue
                portfolio_pnl = ev.pnl_pct or 0.0
                for coin in coins:
                    if not isinstance(coin, dict):
                        continue
                    sym = (coin.get("symbol") or "").upper()
                    if not sym:
                        continue
                    s = coin_stats.setdefault(
                        sym, {"loss_count": 0, "win_count": 0,
                              "severe_count": 0, "min_pnl": 0.0}
                    )
                    coin_pnl = coin.get("pnl_pct")
                    pnl = coin_pnl if isinstance(coin_pnl, (int, float)) else portfolio_pnl
                    if pnl < 0:
                        s["loss_count"] += 1
                        s["min_pnl"] = min(s["min_pnl"], pnl)
                        if pnl <= -2.0:
                            s["severe_count"] += 1
                    elif pnl > 0:
                        s["win_count"] += 1

        added = 0
        for symbol, s in coin_stats.items():
            net_loss = s["loss_count"] - s["win_count"]  # 익절 발생 시 감점
            min_pnl = s["min_pnl"]
            severe = s["severe_count"]

            ban_days = 0
            reason = ""
            # 3회 이상 손실 우세 → 5일 차단
            if net_loss >= 3:
                ban_days = 5
                reason = (
                    f"{days}일 내 손실 {s['loss_count']}회/익절 {s['win_count']}회 "
                    f"(최저 {min_pnl:.1f}%)"
                )
            # 2회 손실 + 한 번이라도 -2% 이상 폭락 → 3일 차단
            elif s["loss_count"] >= 2 and severe >= 1:
                ban_days = 3
                reason = (
                    f"{days}일 내 손실 {s['loss_count']}회 중 폭락 {severe}회 "
                    f"(최저 {min_pnl:.1f}%)"
                )

            if ban_days > 0:
                add_to_blacklist(symbol, days=ban_days, reason=reason)
                added += 1

        if added:
            logger.info(f"[블랙리스트 갱신] {added}개 코인 차단/연장 (분석 기간 {days}일)")
        return added
    except Exception as e:
        logger.warning(f"[블랙리스트 갱신 실패] {e}")
        return 0
