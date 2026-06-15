"""rule_baseline 시나리오 — 룰 진입 엔진으로 현 v4.4 선정 근사.

풀 유니버스 + 변동성 보상 ON + 블랙리스트 OFF. phase2 비교의 기준선.
검증게이트(운영 trades 주입)용 baseline.py와 별개 — 이쪽은 진입까지 룰로 생성.
"""
from backtest.rule_engine import RuleConfig


def config(exchange: str) -> RuleConfig:
    min_vol = 30_000_000_000.0 if exchange == "bithumb" else 10_000_000_000.0
    return RuleConfig(
        name="rule_baseline",
        whitelist=None,                    # 풀 유니버스
        use_volatility_reward=True,        # 변동성 가점 ON
        permanent_blacklist_losses=False,  # 블랙리스트 OFF
        min_volume_krw=min_vol,
    )
