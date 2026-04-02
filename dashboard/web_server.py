"""간단한 HTTP 웹 대시보드 서버

GET /           — HTML 상태 페이지 (30초 자동 갱신)
GET /api/status — JSON 상태 데이터
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from database import TradeRepository
from database.models import Position

if TYPE_CHECKING:
    from core import BithumbClient

logger = logging.getLogger(__name__)


def _build_json_status(client: "BithumbClient") -> dict:
    """현재 상태를 JSON 직렬화 가능한 dict로 반환"""
    repo = TradeRepository()
    try:
        krw = client.get_krw_balance()
        total = krw
        position_data = None

        pos: Position | None = repo.get_open_position()
        if pos:
            try:
                cur = client.get_current_price(pos.symbol)
                pnl_pct = (cur - pos.buy_price) / pos.buy_price * 100
                pnl_krw = (cur - pos.buy_price) * pos.units
                pos_value = pos.units * cur
                total = krw + pos_value
                held_min = (datetime.utcnow() - pos.opened_at).total_seconds() / 60
                position_data = {
                    "symbol": pos.symbol,
                    "units": round(pos.units, 6),
                    "buy_price": pos.buy_price,
                    "current_price": cur,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_krw": round(pnl_krw, 0),
                    "take_profit_pct": pos.take_profit_pct,
                    "stop_loss_pct": pos.stop_loss_pct,
                    "held_minutes": round(held_min, 1),
                    "agent_reason": pos.agent_reason or "",
                    "llm_provider": pos.llm_provider or "",
                }
            except Exception:
                pass

        stats = repo.get_total_stats()
        recent_trades = repo.get_all_trades(limit=20)
        recent_reports = repo.get_recent_reports(7)

        trades_data = [
            {
                "time": t.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": t.symbol,
                "side": t.side,
                "price": t.price,
                "units": round(t.units, 6),
                "krw_amount": round(t.krw_amount, 0),
                "note": t.note or "",
            }
            for t in recent_trades
        ]
        reports_data = [
            {
                "date": r.date,
                "pnl_krw": round(r.pnl_krw, 0),
                "pnl_pct": round(r.pnl_pct, 2),
                "trade_count": r.trade_count,
                "win_count": r.win_count,
            }
            for r in reversed(recent_reports)
        ]

        initial = stats["initial_krw"]
        total_pnl_pct = (total - initial) / initial * 100 if initial > 0 else 0.0

        return {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "balance": {"krw": round(krw, 0), "total_assets": round(total, 0)},
            "performance": {
                "total_pnl_pct": round(total_pnl_pct, 2),
                "win_rate": round(stats["win_rate"] * 100, 1),
                "win_count": stats["win_count"],
                "loss_count": stats["loss_count"],
                "total_cycles": stats["total_cycles"],
                "avg_hold_minutes": round(stats["avg_hold_minutes"], 1),
            },
            "position": position_data,
            "recent_trades": trades_data,
            "daily_reports": reports_data,
        }
    finally:
        repo.close()


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>pochaco 대시보드</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  header {{ background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; justify-content: space-between; align-items: center; }}
  header h1 {{ font-size: 1.4rem; color: #38bdf8; font-weight: 700; }}
  header span {{ font-size: 0.8rem; color: #64748b; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
           gap: 16px; padding: 20px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 20px;
           border: 1px solid #334155; }}
  .card h2 {{ font-size: 0.85rem; color: #64748b; text-transform: uppercase;
              letter-spacing: 0.05em; margin-bottom: 12px; }}
  .big-num {{ font-size: 2rem; font-weight: 700; color: #f1f5f9; }}
  .sub {{ font-size: 0.85rem; color: #94a3b8; margin-top: 4px; }}
  .green {{ color: #4ade80; }}
  .red   {{ color: #f87171; }}
  .gray  {{ color: #64748b; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 9999px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-green {{ background: #14532d; color: #4ade80; }}
  .badge-red   {{ background: #450a0a; color: #f87171; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ color: #64748b; text-align: left; padding: 6px 8px;
        border-bottom: 1px solid #334155; font-weight: 500; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #1e293b; }}
  tr:hover td {{ background: #0f172a; }}
  .stat-row {{ display: flex; justify-content: space-between;
               padding: 6px 0; border-bottom: 1px solid #334155; }}
  .stat-row:last-child {{ border-bottom: none; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; }}
  .stat-value {{ font-weight: 600; font-size: 0.85rem; }}
  footer {{ text-align: center; padding: 12px; color: #334155; font-size: 0.75rem; }}
  .no-data {{ color: #475569; font-style: italic; text-align: center;
              padding: 20px; }}
</style>
</head>
<body>
<header>
  <h1>🤖 pochaco 자동매매 대시보드</h1>
  <span>갱신: {updated_at} &nbsp;|&nbsp; 30초마다 자동 새로고침</span>
</header>

<div class="grid">

  <!-- 자산 현황 -->
  <div class="card">
    <h2>💰 자산 현황</h2>
    <div class="big-num">{total_assets_fmt}</div>
    <div class="sub">KRW 잔고: {krw_fmt}</div>
    {pos_asset_line}
    <br>
    <div class="stat-row">
      <span class="stat-label">누적 수익률</span>
      <span class="stat-value {total_pnl_color}">{total_pnl_pct:+.2f}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">승률</span>
      <span class="stat-value">{win_rate:.1f}% ({win_count}승 {loss_count}패)</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">총 매매</span>
      <span class="stat-value">{total_cycles}회</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">평균 보유</span>
      <span class="stat-value">{avg_hold}</span>
    </div>
  </div>

  <!-- 현재 포지션 -->
  <div class="card">
    <h2>📦 현재 포지션</h2>
    {position_html}
  </div>

  <!-- 일별 성과 -->
  <div class="card">
    <h2>📅 일별 성과 (최근 7일)</h2>
    {reports_html}
  </div>

</div>

<!-- 거래 내역 -->
<div style="padding: 0 20px 20px;">
  <div class="card">
    <h2>🔄 최근 거래 내역</h2>
    {trades_html}
  </div>
</div>

<footer>pochaco — AI 자동매매 시스템 &nbsp;|&nbsp; 데이터는 30초마다 갱신됩니다</footer>
</body>
</html>"""


def _render_html(data: dict) -> str:
    bal = data["balance"]
    perf = data["performance"]
    pos = data["position"]

    def fmt_krw(v: float) -> str:
        av = abs(v)
        if av >= 1_0000_0000:
            return f"{v / 1_0000_0000:.2f}억 원"
        if av >= 10_000:
            return f"{v / 10_000:.1f}만 원"
        return f"{v:,.0f} 원"

    total_pnl_color = "green" if perf["total_pnl_pct"] >= 0 else "red"

    # 포지션 평가액 줄
    pos_asset_line = ""
    if pos:
        pos_asset_line = (
            f'<div class="sub">'
            f'{pos["symbol"]} 평가: {fmt_krw(pos["units"] * pos["current_price"])}'
            f'</div>'
        )

    avg_h = perf["avg_hold_minutes"]
    avg_hold = f"{avg_h / 60:.1f}시간" if avg_h >= 60 else f"{avg_h:.0f}분"

    # 포지션 HTML
    if pos:
        pnl_pct = pos["pnl_pct"]
        pnl_color = "green" if pnl_pct >= 0 else "red"
        held = pos["held_minutes"]
        held_str = f"{held / 60:.1f}시간" if held >= 60 else f"{held:.0f}분"
        progress = min(1.0, max(0.0, pnl_pct / pos["take_profit_pct"])) if pos["take_profit_pct"] > 0 else 0.0
        bar_color = "#4ade80" if pnl_pct >= 0 else "#f87171"
        position_html = f"""
        <div class="big-num {pnl_color}">{pnl_pct:+.2f}%</div>
        <div class="sub">{pos['symbol']} {pos['units']} 개</div>
        <br>
        <div class="stat-row">
          <span class="stat-label">매수가</span>
          <span class="stat-value">{pos['buy_price']:,.0f} 원</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">현재가</span>
          <span class="stat-value">{pos['current_price']:,.0f} 원</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">평가 손익</span>
          <span class="stat-value {pnl_color}">{pos['pnl_krw']:+,.0f} 원</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">익절 / 손절</span>
          <span class="stat-value">
            <span class="green">+{pos['take_profit_pct']}%</span> /
            <span class="red">{pos['stop_loss_pct']}%</span>
          </span>
        </div>
        <div class="stat-row">
          <span class="stat-label">보유 시간</span>
          <span class="stat-value">{held_str}</span>
        </div>
        <div style="margin-top:10px; font-size:0.8rem; color:#94a3b8;">익절 달성률</div>
        <div style="background:#0f172a; border-radius:4px; height:8px; margin-top:4px;">
          <div style="background:{bar_color}; width:{progress*100:.0f}%; height:100%; border-radius:4px;"></div>
        </div>
        <div style="font-size:0.75rem; color:#64748b; margin-top:4px;">{progress:.0%} / 100%</div>
        <div style="margin-top:10px; font-size:0.78rem; color:#64748b; font-style:italic;">
          {pos['agent_reason'][:80]}
        </div>
        """
    else:
        position_html = '<div class="no-data">현재 보유 포지션 없음<br>AI 코인 선정 대기 중...</div>'

    # 일별 성과 HTML
    if data["daily_reports"]:
        rows = ""
        for r in reversed(data["daily_reports"]):
            sign = "+" if r["pnl_pct"] >= 0 else ""
            color = "green" if r["pnl_pct"] >= 0 else "red"
            badge_cls = "badge-green" if r["pnl_pct"] >= 0 else "badge-red"
            total_d = r["trade_count"] // 2 if r["trade_count"] > 0 else 0
            rows += (
                f"<tr>"
                f"<td>{r['date']}</td>"
                f'<td class="{color}">{sign}{r["pnl_pct"]:.2f}%</td>'
                f'<td class="{color}">{sign}{r["pnl_krw"]:,.0f}</td>'
                f'<td><span class="badge {badge_cls}">'
                f'{r["win_count"]}/{total_d}승</span></td>'
                f"</tr>"
            )
        reports_html = (
            "<table><tr>"
            "<th>날짜</th><th>수익률</th><th>손익(원)</th><th>승</th>"
            f"</tr>{rows}</table>"
        )
    else:
        reports_html = '<div class="no-data">성과 데이터 없음<br>(매일 23:55 기록)</div>'

    # 거래 내역 HTML
    if data["recent_trades"]:
        rows = ""
        for t in data["recent_trades"]:
            side_class = "badge-green" if t["side"] == "buy" else "badge-red"
            side_label = "BUY" if t["side"] == "buy" else "SELL"
            rows += (
                f"<tr>"
                f"<td class='gray'>{t['time']}</td>"
                f"<td><b>{t['symbol']}</b></td>"
                f'<td><span class="badge {side_class}">{side_label}</span></td>'
                f"<td>{t['price']:,.0f}</td>"
                f"<td>{t['units']:.4f}</td>"
                f"<td>{t['krw_amount']:,.0f}</td>"
                f"<td class='gray'>{t['note'][:28]}</td>"
                f"</tr>"
            )
        trades_html = (
            "<table><tr>"
            "<th>시간</th><th>심볼</th><th>구분</th>"
            "<th>가격(원)</th><th>수량</th><th>금액(원)</th><th>비고</th>"
            f"</tr>{rows}</table>"
        )
    else:
        trades_html = '<div class="no-data">거래 내역 없음</div>'

    return _HTML_TEMPLATE.format(
        updated_at=data["updated_at"],
        total_assets_fmt=fmt_krw(bal["total_assets"]),
        krw_fmt=fmt_krw(bal["krw"]),
        pos_asset_line=pos_asset_line,
        total_pnl_color=total_pnl_color,
        total_pnl_pct=perf["total_pnl_pct"],
        win_rate=perf["win_rate"],
        win_count=perf["win_count"],
        loss_count=perf["loss_count"],
        total_cycles=perf["total_cycles"],
        avg_hold=avg_hold,
        position_html=position_html,
        reports_html=reports_html,
        trades_html=trades_html,
    )


class _Handler(BaseHTTPRequestHandler):
    client: "BithumbClient"

    def log_message(self, fmt, *args):
        pass  # 액세스 로그 억제

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                data = _build_json_status(self.client)
                body = _render_html(data).encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "text/plain", f"Error: {e}".encode())

        elif self.path == "/api/status":
            try:
                data = _build_json_status(self.client)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"error": str(e)}).encode())

        else:
            self._respond(404, "text/plain", b"Not Found")

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebDashboard:
    """백그라운드 HTTP 대시보드 서버"""

    def __init__(self, client: "BithumbClient", host: str, port: int):
        self._client = client
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {"client": self._client})
        self._server = ThreadingHTTPServer((self._host, self._port), handler)

        thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="web-dashboard",
        )
        thread.start()
        logger.info(f"웹 대시보드 시작: http://{self._host}:{self._port}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
