"""쿨다운 레지스트리 — 매도 후 동일 종목 재매수 방지

TradingEngine(자동 익손절)과 WebDashboard(수동 청산) 모두 이 모듈을 통해
쿨다운을 기록·조회합니다. 두 컴포넌트가 같은 프로세스 내 다른 스레드로
동작하므로 모듈 싱글턴 + Lock으로 안전하게 공유됩니다.

쿨다운 시간:
  take_profit : 30분  — 익절 후 되돌림 구간 회피
  stop_loss   : 10분  — 직전 하락 패턴 재진입 방지
  manual      : 60분  — 사용자가 직접 개입했으므로 가장 보수적으로 적용
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_COOLDOWN_MINUTES: dict[str, float] = {
    "take_profit": 30.0,
    "stop_loss":   10.0,
    "manual":      60.0,
}

# symbol → 쿨다운 만료 시각 (epoch)
_cooldowns: dict[str, float] = {}
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
        # 이미 등록된 경우 더 긴 쪽 채택
        existing = _cooldowns.get(symbol, 0)
        _cooldowns[symbol] = max(existing, expiry)

    logger.info(
        f"[쿨다운 등록] {symbol} ({exit_type}) → {minutes:.0f}분 재매수 금지"
    )


def get_cooldown_symbols() -> set[str]:
    """현재 쿨다운 중인 심볼 집합 반환 (만료된 항목 자동 정리)"""
    now = time.time()
    expired = []
    result = set()

    with _lock:
        for symbol, expiry in _cooldowns.items():
            if now < expiry:
                remaining = (expiry - now) / 60
                result.add(symbol)
                logger.debug(f"[쿨다운 중] {symbol} 잔여 {remaining:.0f}분")
            else:
                expired.append(symbol)
        for symbol in expired:
            del _cooldowns[symbol]

    return result
