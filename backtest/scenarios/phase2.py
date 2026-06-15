"""phase2 시나리오 — 종목 유니버스 메이저 제한 (next_plan.md Phase 2).

rule_baseline 대비 3대 변경:
  1. 메이저 화이트리스트 — 시총·유동성 상위 ~25종만 진입 (잡알트 도배 차단)
  2. 변동성 보상 제거 — 과대 변동성 코인 감점 (펌핑 잡코인 우선 선정 방지)
  3. 손실코인 영구 블랙리스트 — 손실 N회 누적 시 영구 차단 (MERL 33회 패턴 차단)

다른 모든 파라미터(청산 로직·쿨다운·동시보유·거래대금 하한)는 baseline과 동일 →
세 변경의 순효과만 분리 측정.
"""
from backtest.rule_engine import RuleConfig, MAJOR_WHITELIST


def config(exchange: str) -> RuleConfig:
    min_vol = 30_000_000_000.0 if exchange == "bithumb" else 10_000_000_000.0
    return RuleConfig(
        name="phase2",
        whitelist=MAJOR_WHITELIST,         # 메이저 제한
        use_volatility_reward=False,       # 변동성 보상 제거 + 과대변동성 감점
        permanent_blacklist_losses=True,   # 손실 누적 코인 영구 차단
        blacklist_loss_threshold=3,
        min_volume_krw=min_vol,
    )
