"""5분봉에서 진입 지표 재계산 (next_plan.md Phase 2 — 룰 진입 엔진).

candles.db의 5분봉 OHLCV만으로 coin_selector가 쓰는 지표를 근사 재현한다.
운영 selector는 라이브 스냅샷(RSI/MACD/OBV/BB·펀딩비 등)을 쓰지만, 여기서는
시나리오 간 **상대 비교**가 목적이므로 핵심 지표(거래대금·변동폭·모멘텀·RSI·
하락추세)만 동일 기준으로 산출한다. 절대값이 운영과 달라도 baseline_rule과
phase2가 같은 지표를 공유하므로 재계산 오차는 상쇄된다(계획서 원칙).

운영 환경에 numpy가 없어 순수 파이썬으로 구현(누적합·단조 deque로 선형 처리).
모든 지표는 사전 계산해 walk-forward 시 O(1) 조회한다.
"""
from __future__ import annotations

from collections import deque

# 5분봉 기준 윈도우 상수
_BARS_1H = 12       # 1시간 = 5분봉 12개
_BARS_24H = 288     # 24시간 = 5분봉 288개
_BARS_3H = 36       # 3시간

NAN = float("nan")


def rsi_wilder(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI. closes와 같은 길이, 초기 구간은 NaN."""
    n = len(closes)
    out = [NAN] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains[i] = d if d > 0 else 0.0
        losses[i] = -d if d < 0 else 0.0
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    for i in range(period, n):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l > 0:
            rs = avg_g / avg_l
            out[i] = 100.0 - 100.0 / (1.0 + rs)
        else:
            out[i] = 100.0 if avg_g > 0 else 50.0
    return out


def _rolling_sum(a: list[float], w: int) -> list[float]:
    """길이 보존 rolling sum (앞 구간은 가용분만 합산) — 누적합 O(n)."""
    csum = [0.0]
    for x in a:
        csum.append(csum[-1] + x)
    out = [0.0] * len(a)
    for i in range(len(a)):
        lo = max(0, i - w + 1)
        out[i] = csum[i + 1] - csum[lo]
    return out


def _rolling_extreme(a: list[float], w: int, want_max: bool) -> list[float]:
    """단조 deque로 rolling max/min — O(n)."""
    out = [0.0] * len(a)
    dq: deque[int] = deque()  # 인덱스, 값이 단조
    for i, x in enumerate(a):
        while dq and dq[0] <= i - w:
            dq.popleft()
        if want_max:
            while dq and a[dq[-1]] <= x:
                dq.pop()
        else:
            while dq and a[dq[-1]] >= x:
                dq.pop()
        dq.append(i)
        out[i] = a[dq[0]]
    return out


def _pct_change(c: list[float], w: int) -> list[float]:
    out = [NAN] * len(c)
    for i in range(w, len(c)):
        base = c[i - w]
        if base > 0:
            out[i] = (c[i] - base) / base * 100.0
    return out


class Indicators:
    """단일 종목 5분봉 시계열의 사전 계산된 지표 묶음.

    각 리스트는 candles와 동일 인덱스. 인덱스 i = "i번째 봉 종료 시점"에
    관측 가능한 값(미래 참조 없음).
    """

    def __init__(self, ts: list[int], o: list[float], h: list[float],
                 l: list[float], c: list[float], v: list[float]):
        self.ts = ts
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v
        n = len(c)

        # 거래대금(24h) = Σ(volume × close)  — 원화 환산
        vc = [v[i] * c[i] for i in range(n)]
        self.vol_krw_24h = _rolling_sum(vc, _BARS_24H)
        # 변동폭(24h 고저폭 %) = (max high − min low) / min low × 100
        hi = _rolling_extreme(h, _BARS_24H, want_max=True)
        lo = _rolling_extreme(l, _BARS_24H, want_max=False)
        self.volatility_pct = [
            (hi[i] - lo[i]) / lo[i] * 100.0 if lo[i] > 0 else 0.0
            for i in range(n)
        ]

        # 변화율
        self.change_24h = _pct_change(c, _BARS_24H)
        self.change_3h = _pct_change(c, _BARS_3H)
        self.change_1h = _pct_change(c, _BARS_1H)

        # 모멘텀: 1h 변화율 × 3 + 최근 3봉 연속성, [-10, 10] 클램프
        mom = [0.0] * n
        for i in range(n):
            s = 0
            for k in range(1, 4):
                if i - k - 1 < 0:
                    break
                if c[i - k] > c[i - k - 1]:
                    s += 1
                elif c[i - k] < c[i - k - 1]:
                    s -= 1
            ch1h = self.change_1h[i]
            base = (ch1h * 3.0) if ch1h == ch1h else 0.0  # NaN 체크
            mom[i] = max(-10.0, min(10.0, base + s * 1.5))
        self.momentum = mom

        # RSI(14) — 1시간 리샘플 종가 기준(노이즈 완화), 5분 인덱스로 매핑
        self.rsi = self._hourly_rsi(ts, c)

    @staticmethod
    def _hourly_rsi(ts: list[int], c: list[float]) -> list[float]:
        """1시간 리샘플 종가로 RSI14 계산 후 5분 인덱스에 전방 채움."""
        n = len(c)
        out = [50.0] * n
        if n < _BARS_1H * 16:
            return out
        # 1시간 버킷의 마지막 봉 인덱스
        hour_idx: list[int] = []
        for i in range(1, n):
            if ts[i] // 3600 != ts[i - 1] // 3600:
                hour_idx.append(i - 1)
        hour_idx.append(n - 1)
        hour_closes = [c[k] for k in hour_idx]
        hr = rsi_wilder(hour_closes, 14)
        # 각 5분 인덱스 i에 "직전 완료된 시간봉"의 RSI 매핑
        j = 0
        cur = 50.0
        for i in range(n):
            while j < len(hour_idx) and hour_idx[j] <= i:
                if hr[j] == hr[j]:  # not NaN
                    cur = hr[j]
                j += 1
            out[i] = cur
        return out
