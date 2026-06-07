"""백테스트 지표 산출 (next_plan.md 1.A).

청산건수·승률·PF·평균이익/손실·손익비·기대값·MDD·청산사유분포.
검증 게이트용 시뮬↔운영 일치율도 제공.
"""
from dataclasses import dataclass, field


@dataclass
class KPI:
    n: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    rr_ratio: float = 0.0           # 손익비 = 평균이익/|평균손실|
    expectancy_pct: float = 0.0     # 기대값(%/건)
    sum_pnl_pct: float = 0.0
    mdd_pct: float = 0.0            # 누적(순차) 최대낙폭 — 참고용
    reason_dist: dict = field(default_factory=dict)

    def as_lines(self) -> list[str]:
        return [
            f"청산 {self.n}건 | 승률 {self.win_rate:.1f}% | PF {self.profit_factor:.2f}",
            f"평균익 {self.avg_win_pct:+.2f}% / 평균손 {self.avg_loss_pct:+.2f}% "
            f"(손익비 {self.rr_ratio:.2f})",
            f"기대값 {self.expectancy_pct:+.3f}%/건 | 누적 {self.sum_pnl_pct:+.1f}%p "
            f"| MDD {self.mdd_pct:.1f}%p",
        ]


def compute_kpi(pnls_pct: list[float], reasons: list[str] | None = None) -> KPI:
    """청산 손익률(%) 리스트로 KPI 산출."""
    k = KPI()
    pnls = [p for p in pnls_pct if p is not None]
    if not pnls:
        return k
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    k.n = len(pnls)
    k.win_rate = len(wins) / k.n * 100
    k.avg_win_pct = sum(wins) / len(wins) if wins else 0.0
    k.avg_loss_pct = sum(losses) / len(losses) if losses else 0.0
    gp, gl = sum(wins), -sum(losses)
    k.profit_factor = gp / gl if gl else (99.9 if gp else 0.0)
    k.rr_ratio = (k.avg_win_pct / -k.avg_loss_pct) if k.avg_loss_pct else 0.0
    k.expectancy_pct = sum(pnls) / k.n
    k.sum_pnl_pct = sum(pnls)
    # 누적(순차) MDD — %p 단위 자본곡선
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    k.mdd_pct = mdd
    if reasons:
        dist: dict = {}
        for r in reasons:
            dist[r] = dist.get(r, 0) + 1
        k.reason_dist = dict(sorted(dist.items(), key=lambda kv: -kv[1]))
    return k


@dataclass
class MatchStat:
    """검증 게이트 — 시뮬 vs 운영 일치율."""
    total: int = 0
    price_match: int = 0   # 청산가 ±0.3%
    time_match: int = 0    # 청산시각 ±10분
    pnl_match: int = 0     # 실현손익 ±5%

    def rate(self, hit: int) -> float:
        return hit / self.total * 100 if self.total else 0.0

    def as_lines(self) -> list[str]:
        return [
            f"검증 대상 {self.total}건 (합격선 90%↑)",
            f"  청산가 ±0.3% 일치: {self.rate(self.price_match):.1f}%",
            f"  청산시각 ±10분 일치: {self.rate(self.time_match):.1f}%",
            f"  실현손익 ±5% 일치: {self.rate(self.pnl_match):.1f}%",
        ]
