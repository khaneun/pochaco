"""Rich 터미널 대시보드

레이아웃:
  Header (현재 시각)
  ┌─────────────────┬─────────────────────────────────┐
  │  자산 평가       │  현재 포지션                      │
  └─────────────────┴─────────────────────────────────┘
  자산 변동 차트 (일별 총자산 바 차트)
  AI 일별 행동 보고서 (선정코인 / 익절·손절 / 수익률)
  전체 거래 내역 (최근 N건)
"""
import logging
import time
from datetime import datetime

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core import BithumbClient
from database import TradeRepository
from database.models import Position

logger = logging.getLogger(__name__)
console = Console()

# 브레인 블록 문자 (0 = 빈칸, 8 = 꽉참)
_BLOCKS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


# ------------------------------------------------------------------ #
#  유틸                                                                #
# ------------------------------------------------------------------ #
def _fmt_krw(v: float) -> str:
    """원화 금액 축약 표시"""
    av = abs(v)
    if av >= 1_0000_0000:
        return f"{v / 1_0000_0000:.2f}억"
    if av >= 10_000:
        return f"{v / 10_000:.1f}만"
    return f"{v:.0f}"


def _build_asset_chart(
    points: list[tuple[str, float]],
    chart_height: int = 6,
) -> Text:
    """
    ASCII 바 차트 생성.

    points: [(날짜 레이블(MM/DD), KRW 값), ...]
    색상: 전일 대비 상승=green, 하락=red
    """
    if not points:
        return Text("  차트 데이터 없음", style="dim")

    labels = [p[0] for p in points]
    values = [p[1] for p in points]
    min_v = min(values)
    max_v = max(values)
    v_range = max_v - min_v if max_v != min_v else 1.0

    def norm(v: float) -> int:
        return int((v - min_v) / v_range * chart_height * 8)

    normalized = [norm(v) for v in values]

    colors = [
        "green" if i == 0 or v >= values[i - 1] else "red"
        for i, v in enumerate(values)
    ]

    y_lbl_w = max(len(_fmt_krw(min_v)), len(_fmt_krw(max_v))) + 1

    text = Text()
    for row in range(chart_height, 0, -1):
        row_val = min_v + (row / chart_height) * v_range
        y_str = _fmt_krw(row_val).rjust(y_lbl_w)
        text.append(f" {y_str} ┤")

        for i, n in enumerate(normalized):
            lvl = n - (row - 1) * 8
            char = _BLOCKS[max(0, min(8, lvl))] if lvl > 0 else " "
            style = f"bold {colors[i]}" if char != " " else ""
            text.append(char * 2, style=style)

        text.append("\n")

    # X 축
    text.append(f" {' ' * y_lbl_w} └" + "──" * len(normalized) + "\n")
    text.append(f" {' ' * y_lbl_w}  ")
    for i, lbl in enumerate(labels):
        # 2칸 폭 바에 맞춰 짝수 인덱스만 레이블 표시
        if i % 2 == 0:
            text.append(f"{lbl[:5]:5s} ", style="dim")
        else:
            text.append("      ")

    return text


# ------------------------------------------------------------------ #
#  Dashboard                                                           #
# ------------------------------------------------------------------ #
class Dashboard:
    """Rich Live 기반 터미널 대시보드"""

    REFRESH_INTERVAL = 5  # 초

    def __init__(self, client: BithumbClient, repo: TradeRepository):
        self._client = client
        self._repo = repo
        self._running = False

    # ---------------------------------------------------------------- #
    #  패널: 헤더                                                        #
    # ---------------------------------------------------------------- #
    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return Panel(
            Text(f"pochaco  자동매매 대시보드   ·   {now}", justify="center", style="bold cyan"),
            box=box.DOUBLE,
        )

    # ---------------------------------------------------------------- #
    #  패널: 자산 평가                                                   #
    # ---------------------------------------------------------------- #
    def _build_portfolio_panel(self) -> Panel:
        try:
            krw = self._client.get_krw_balance()
            total_assets = krw
            pos_value = 0.0
            pos_symbol = ""

            pos: Position | None = self._repo.get_open_position()
            if pos:
                try:
                    cur_price = self._client.get_current_price(pos.symbol)
                    pos_value = pos.units * cur_price
                    total_assets = krw + pos_value
                    pos_symbol = pos.symbol
                except Exception:
                    pass

            stats = self._repo.get_total_stats()
            initial = stats["initial_krw"]
            total_pnl = total_assets - initial if initial > 0 else 0.0
            total_pnl_pct = (total_pnl / initial * 100) if initial > 0 else 0.0
            pnl_color = "green" if total_pnl >= 0 else "red"

            tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True)
            tbl.add_column("항목", style="dim", ratio=2)
            tbl.add_column("값", justify="right", ratio=3)

            tbl.add_row("총 자산", Text(f"{total_assets:>15,.0f} 원", style="bold white"))
            tbl.add_row("  ├ KRW 잔고", f"{krw:>15,.0f} 원")
            if pos_symbol:
                tbl.add_row(f"  └ {pos_symbol} 평가", f"{pos_value:>15,.0f} 원")

            pnl_text = Text()
            pnl_text.append(f"{total_pnl:>+15,.0f} 원", style=pnl_color)
            tbl.add_row("누적 손익", pnl_text)

            pct_text = Text()
            pct_text.append(f"{total_pnl_pct:>+14.2f}%", style=pnl_color)
            tbl.add_row("누적 수익률", pct_text)

            wr = stats["win_rate"]
            wr_text = Text()
            wr_text.append(f"{wr:>9.0%}", style="green" if wr >= 0.5 else "red")
            wr_text.append(f"  ({stats['win_count']}승 {stats['loss_count']}패)", style="dim")
            tbl.add_row("승률", wr_text)

            avg_h = stats["avg_hold_minutes"]
            hold_str = f"{avg_h / 60:.1f}시간" if avg_h >= 60 else f"{avg_h:.0f}분"
            tbl.add_row("평균 보유시간", f"{hold_str:>14s}")
            tbl.add_row("총 매매 횟수", f"{stats['total_cycles']:>13d} 회")

            return Panel(tbl, title="[bold cyan]자산 평가[/bold cyan]", box=box.ROUNDED)

        except Exception as e:
            logger.debug(f"portfolio panel error: {e}")
            return Panel(f"[red]{e}[/red]", title="자산 평가", box=box.ROUNDED)

    # ---------------------------------------------------------------- #
    #  패널: 현재 포지션                                                 #
    # ---------------------------------------------------------------- #
    def _build_position_panel(self) -> Panel:
        pos: Position | None = self._repo.get_open_position()
        if pos is None:
            return Panel(
                Text("\n\n  현재 보유 포지션 없음\n  AI 코인 선정 대기 중...", style="dim"),
                title="[bold]현재 포지션[/bold]",
                box=box.ROUNDED,
            )
        try:
            cur_price = self._client.get_current_price(pos.symbol)
            pnl_pct = (cur_price - pos.buy_price) / pos.buy_price * 100
            pnl_krw = (cur_price - pos.buy_price) * pos.units
            pnl_color = "green" if pnl_pct >= 0 else "red"

            held_min = (datetime.utcnow() - pos.opened_at).total_seconds() / 60
            held_str = f"{held_min / 60:.1f}시간" if held_min >= 60 else f"{held_min:.0f}분"

            # 익절 목표 대비 진행률 바
            progress = min(1.0, max(0.0, pnl_pct / pos.take_profit_pct)) if pos.take_profit_pct > 0 else 0.0
            bar_len = 24
            filled = int(progress * bar_len)
            bar_text = Text()
            bar_text.append("█" * filled, style=pnl_color)
            bar_text.append("░" * (bar_len - filled), style="dim")
            bar_text.append(f"  {progress:.0%}", style="dim")

            tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True)
            tbl.add_column("항목", style="dim", ratio=2)
            tbl.add_column("값", ratio=3)

            tbl.add_row("코인 / 수량", Text(f"{pos.symbol}  {pos.units:.6f}개", style="bold"))
            tbl.add_row(
                "매수가 → 현재가",
                f"{pos.buy_price:,.0f}  →  [bold]{cur_price:,.0f}[/bold] 원",
            )

            pnl_val = Text()
            pnl_val.append(f"{pnl_pct:+.2f}%", style=f"bold {pnl_color}")
            pnl_val.append(f"  ({pnl_krw:+,.0f} 원)", style=pnl_color)
            tbl.add_row("평가 손익", pnl_val)

            tbl.add_row("익절 / 손절 기준", f"[green]+{pos.take_profit_pct:.1f}%[/green]  /  [red]{pos.stop_loss_pct:.1f}%[/red]")
            tbl.add_row("보유 시간", held_str)
            tbl.add_row("익절 달성률", bar_text)
            tbl.add_row("AI 선정 이유", Text((pos.agent_reason or "")[:55], style="dim italic"))
            tbl.add_row("LLM", Text(pos.llm_provider or "-", style="dim"))

            return Panel(tbl, title="[bold]현재 포지션[/bold]", box=box.ROUNDED)
        except Exception as e:
            return Panel(f"[red]{e}[/red]", title="현재 포지션", box=box.ROUNDED)

    # ---------------------------------------------------------------- #
    #  패널: 자산 변동 차트                                               #
    # ---------------------------------------------------------------- #
    def _build_chart_panel(self) -> Panel:
        try:
            reports = self._repo.get_all_daily_reports()
            today_str = datetime.now().strftime("%Y-%m-%d")

            points: list[tuple[str, float]] = []
            for r in reports[-14:]:
                label = r.date[5:].replace("-", "/")
                points.append((label, r.ending_krw))

            # 오늘 실시간 총자산을 마지막 포인트로 추가/교체
            try:
                krw = self._client.get_krw_balance()
                total = krw
                pos = self._repo.get_open_position()
                if pos:
                    cur = self._client.get_current_price(pos.symbol)
                    total = krw + pos.units * cur

                today_label = today_str[5:].replace("-", "/") + "▶"
                today_prefix = today_str[5:].replace("-", "/")
                if points and points[-1][0].startswith(today_prefix):
                    points[-1] = (today_label, total)
                else:
                    points.append((today_label, total))
            except Exception:
                pass

            chart = _build_asset_chart(points, chart_height=6)
            return Panel(
                chart,
                title=(
                    "[bold]자산 변동[/bold]  "
                    "(일별 총자산 KRW · 최근 14일 · "
                    "[green]▲상승[/green] [red]▼하락[/red] · ▶오늘)"
                ),
                box=box.ROUNDED,
            )
        except Exception as e:
            return Panel(f"[red]{e}[/red]", title="자산 변동 차트", box=box.ROUNDED)

    # ---------------------------------------------------------------- #
    #  패널: AI 일별 행동 보고서                                          #
    # ---------------------------------------------------------------- #
    def _build_ai_report_panel(self) -> Panel:
        try:
            summaries = self._repo.get_daily_activity_summary(days=7)

            tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold blue", expand=True)
            tbl.add_column("날짜",     width=11)
            tbl.add_column("선정 코인",  width=22)
            tbl.add_column("매매수", justify="center", width=6)
            tbl.add_column("익절",   justify="center", width=5, style="green")
            tbl.add_column("손절",   justify="center", width=5, style="red")
            tbl.add_column("승률",   justify="right",  width=7)
            tbl.add_column("일 수익률", justify="right", width=10)
            tbl.add_column("LLM",    width=14, style="dim")

            for s in summaries:
                total_decided = s["wins"] + s["losses"]
                wr_text = Text()
                if total_decided > 0:
                    wr = s["wins"] / total_decided
                    wr_text.append(f"{wr:.0%}", style="green" if wr >= 0.5 else "red")
                else:
                    wr_text.append("-", style="dim")

                pnl = s["pnl_pct"]
                pnl_text = Text()
                if pnl > 0:
                    pnl_text.append(f"+{pnl:.2f}%", style="green")
                elif pnl < 0:
                    pnl_text.append(f"{pnl:.2f}%", style="red")
                else:
                    pnl_text.append(f"{pnl:.2f}%", style="dim")

                coins = ", ".join(s["symbols"][:4])
                if len(s["symbols"]) > 4:
                    coins += " …"
                llm_short = (s["llm"] or "-").split("/")[-1][:13]

                tbl.add_row(
                    s["date"],
                    coins,
                    str(s["total"]),
                    str(s["wins"]) if s["wins"] else "-",
                    str(s["losses"]) if s["losses"] else "-",
                    wr_text,
                    pnl_text,
                    llm_short,
                )

            if not summaries:
                tbl.add_row("—", "거래 데이터 없음", "", "", "", "", "", "")

            return Panel(tbl, title="[bold]AI 일별 행동 보고서[/bold]  (최근 7일)", box=box.ROUNDED)
        except Exception as e:
            return Panel(f"[red]{e}[/red]", title="AI 보고서", box=box.ROUNDED)

    # ---------------------------------------------------------------- #
    #  패널: 전체 거래 내역                                               #
    # ---------------------------------------------------------------- #
    def _build_trades_panel(self) -> Panel:
        trades = self._repo.get_all_trades(limit=30)

        tbl = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta", expand=True)
        tbl.add_column("시간",       style="dim", width=19)
        tbl.add_column("심볼",       width=8)
        tbl.add_column("구분",       width=5)
        tbl.add_column("가격(원)",   justify="right", width=13)
        tbl.add_column("수량",       justify="right", width=13)
        tbl.add_column("금액(KRW)", justify="right", width=15)
        tbl.add_column("비고",       width=24)

        for t in trades:
            side_style = "green" if t.side == "buy" else "red"
            side_text = Text(t.side.upper(), style=side_style)
            tbl.add_row(
                t.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                t.symbol,
                side_text,
                f"{t.price:>12,.0f}",
                f"{t.units:>12.6f}",
                f"{t.krw_amount:>14,.0f}",
                (t.note or "")[:24],
            )

        return Panel(
            tbl,
            title=f"[bold]전체 거래 내역[/bold]  (최근 {len(trades)}건)",
            box=box.ROUNDED,
        )

    # ---------------------------------------------------------------- #
    #  레이아웃 조립                                                     #
    # ---------------------------------------------------------------- #
    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header",    size=3),
            Layout(name="top",       size=14),
            Layout(name="chart",     size=12),
            Layout(name="ai_report", size=11),
            Layout(name="trades"),
        )
        layout["top"].split_row(
            Layout(name="portfolio", ratio=1),
            Layout(name="position",  ratio=2),
        )

        layout["header"].update(self._build_header())
        layout["portfolio"].update(self._build_portfolio_panel())
        layout["position"].update(self._build_position_panel())
        layout["chart"].update(self._build_chart_panel())
        layout["ai_report"].update(self._build_ai_report_panel())
        layout["trades"].update(self._build_trades_panel())

        return layout

    # ---------------------------------------------------------------- #
    #  실행                                                              #
    # ---------------------------------------------------------------- #
    def run(self) -> None:
        """Live 대시보드 실행 (블로킹). Ctrl+C로 종료"""
        self._running = True
        with Live(
            self._build_layout(),
            refresh_per_second=1,
            screen=True,
        ) as live:
            try:
                while self._running:
                    live.update(self._build_layout())
                    time.sleep(self.REFRESH_INTERVAL)
            except KeyboardInterrupt:
                pass
        self._running = False

    def stop(self) -> None:
        self._running = False
