"""간단한 HTTP 웹 대시보드 서버

GET /           — HTML 상태 페이지 (30초 자동 갱신)
GET /api/status — JSON 상태 데이터
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

_APP_DIR = Path(__file__).parent.parent

from config import settings
from database import TradeRepository
from database.models import Position

if TYPE_CHECKING:
    from core import BithumbClient

logger = logging.getLogger(__name__)


def _get_version() -> str:
    """release-note.md에서 최신 버전 추출"""
    try:
        for line in (_APP_DIR / "release-note.md").read_text("utf-8").splitlines():
            if line.startswith("## v"):
                return line.split()[1]
    except Exception:
        pass
    return ""


_VERSION = _get_version()


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
                    "units": round(pos.units, 2),
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
        recent_trades = repo.get_all_trades(limit=100)
        recent_reports = repo.get_recent_reports(7)

        trades_data = [
            {
                "time": t.created_at.strftime("%m-%d %H:%M:%S"),
                "symbol": t.symbol,
                "side": t.side,
                "price": t.price,
                "units": round(t.units, 2),
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

        # 성과 평가 데이터
        recent_evals = repo.get_recent_evaluations(limit=10)
        eval_stats = repo.get_evaluation_stats(last_n=10)
        evals_data = [
            {
                "time": ev.created_at.strftime("%m-%d %H:%M:%S"),
                "symbol": ev.symbol,
                "exit_type": ev.exit_type,
                "pnl_pct": round(ev.pnl_pct, 2),
                "held_minutes": round(ev.held_minutes, 1),
                "original_tp": ev.original_tp_pct,
                "original_sl": ev.original_sl_pct,
                "suggested_tp": ev.suggested_tp_pct,
                "suggested_sl": ev.suggested_sl_pct,
                "evaluation": ev.evaluation,
                "lesson": ev.lesson or "",
            }
            for ev in recent_evals
        ]

        # 거래소 실제 보유 코인 (DB position과 별도, 실제 잔고)
        holdings = []
        try:
            bal_data = client.get_balance("ALL")
            if bal_data.get("status") == "0000":
                for key, value in bal_data["data"].items():
                    if not key.startswith("available_"):
                        continue
                    sym = key.replace("available_", "").upper()
                    if sym == "KRW":
                        continue
                    amt = float(value)
                    if amt <= 0:
                        continue
                    try:
                        px = client.get_current_price(sym)
                        kv = amt * px
                        if kv >= 100:
                            holdings.append({
                                "symbol": sym,
                                "units": amt,
                                "price": px,
                                "krw_value": round(kv, 0),
                            })
                            total += kv  # 총 자산에 포함
                    except Exception:
                        holdings.append({"symbol": sym, "units": amt, "price": 0, "krw_value": 0})
        except Exception:
            pass

        return {
            "updated_at": datetime.now().strftime("%m-%d %H:%M:%S"),
            "version": _VERSION,
            "balance": {"krw": round(krw, 0), "total_assets": round(total, 0)},
            "holdings": holdings,
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
            "evaluations": evals_data,
            "eval_stats": eval_stats,
        }
    finally:
        repo.close()


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Pochaco Monitor</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  header {{ background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  header h1 {{ font-size: 1.4rem; color: #38bdf8; font-weight: 700; }}
  header span {{ font-size: 0.8rem; color: #64748b; }}
  .health-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                 background: #4ade80; box-shadow: 0 0 6px #4ade80; margin-right: 6px;
                 animation: pulse 2s infinite; vertical-align: middle; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
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
  .trade-row-main td {{ border-bottom: none; padding-bottom: 2px; }}
  .trade-row-sub td {{ padding-top: 2px; font-size: 0.78rem; color: #64748b; }}
  .trade-row-main {{ border-top: 1px solid #334155; }}
  .trade-row-main:hover td, .trade-row-sub:hover td {{ background: #0f172a; }}
  .eval-row-main td {{ border-bottom: none; padding-bottom: 2px; }}
  .eval-row-sub td {{ padding-top: 2px; font-size: 0.78rem; color: #64748b; }}
  .eval-row-main {{ border-top: 1px solid #334155; }}
  .eval-row-main:hover td, .eval-row-sub:hover td {{ background: #0f172a; }}
  .time-date {{ display: block; color: #64748b; font-size: 0.75rem; }}
  .time-hms  {{ display: block; font-size: 0.82rem; }}
  .profile-img {{ width: 2rem; height: 2rem; border-radius: 50%;
                  object-fit: cover; margin-right: 10px; vertical-align: middle;
                  border: 2px solid #334155; }}
  .expandable .full {{ display: none; }}
  .more-btn {{ background: none; border: none; color: #38bdf8; cursor: pointer;
               font-size: 0.75rem; padding: 0 2px; text-decoration: underline; }}
  .pager {{ display: flex; justify-content: center; gap: 6px; margin-top: 10px; }}
  .pager button {{ background: #334155; border: 1px solid #475569; color: #e2e8f0;
                   border-radius: 6px; padding: 4px 12px; cursor: pointer;
                   font-size: 0.78rem; }}
  .pager button:hover {{ background: #475569; }}
  .pager button:disabled {{ opacity: 0.35; cursor: default; }}
  .pager span {{ font-size: 0.78rem; color: #94a3b8; line-height: 28px; }}
  .stat-row {{ display: flex; justify-content: space-between;
               padding: 6px 0; border-bottom: 1px solid #334155; }}
  .stat-row:last-child {{ border-bottom: none; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; }}
  .stat-value {{ font-weight: 600; font-size: 0.85rem; }}
  footer {{ text-align: center; padding: 12px; color: #334155; font-size: 0.75rem; }}
  .no-data {{ color: #475569; font-style: italic; text-align: center;
              padding: 20px; }}
</style>
<script>
function liquidatePosition() {{
  if (!confirm('현재 포지션을 시장가로 청산합니다.\\n정말 실행하시겠습니까?')) return;
  var btn = document.getElementById('liq-btn');
  btn.disabled = true; btn.textContent = '청산 중...';
  fetch('/api/liquidate', {{ method: 'POST' }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.success) {{
        var msg = '포지션 청산 완료!\\n' + d.symbol + ' → ' + Number(d.krw_received).toLocaleString() + '원 회수';
        msg += '\\nKRW 잔고: ' + Number(d.krw_balance).toLocaleString() + '원';
        alert(msg);
        location.reload();
      }} else {{
        alert('청산 실패: ' + (d.error || '알 수 없는 오류'));
        btn.disabled = false; btn.textContent = '🔥 포지션 청산';
      }}
    }})
    .catch(function(e) {{
      alert('오류: ' + e);
      btn.disabled = false; btn.textContent = '🔥 포지션 청산';
    }});
}}

function toggleMore(btn) {{
  var wrap = btn.closest('.expandable');
  var short = wrap.querySelector('.short');
  var full  = wrap.querySelector('.full');
  if (full.style.display === 'none' || full.style.display === '') {{
    short.style.display = 'none';
    full.style.display  = 'inline';
  }} else {{
    full.style.display  = 'none';
    short.style.display = 'inline';
  }}
}}

/* 페이지네이션: 2줄씩 묶인 tr 그룹 단위로 페이징 */
function initPager(tableId, pagerId, rowsPerPage) {{
  var table = document.getElementById(tableId);
  if (!table) return;
  var tbody = table.querySelector('tbody') || table;
  var allRows = Array.from(tbody.querySelectorAll('tr:not(:first-child)'));
  /* 2줄(main+sub)이 한 그룹 */
  var groups = [];
  for (var i = 0; i < allRows.length; i += 2) {{
    groups.push([allRows[i], allRows[i+1]]);
  }}
  var totalPages = Math.ceil(groups.length / rowsPerPage) || 1;
  var page = 0;

  function render() {{
    groups.forEach(function(g) {{ g.forEach(function(r) {{ r.style.display = 'none'; }}); }});
    var start = page * rowsPerPage;
    var end = Math.min(start + rowsPerPage, groups.length);
    for (var j = start; j < end; j++) {{
      groups[j].forEach(function(r) {{ r.style.display = ''; }});
    }}
    var pager = document.getElementById(pagerId);
    if (pager) {{
      pager.querySelector('.pg-info').textContent = (page+1) + ' / ' + totalPages;
      pager.querySelector('.pg-prev').disabled = (page === 0);
      pager.querySelector('.pg-next').disabled = (page >= totalPages - 1);
    }}
  }}

  var pager = document.getElementById(pagerId);
  if (pager) {{
    pager.querySelector('.pg-prev').onclick = function() {{ if(page>0){{ page--; render(); }} }};
    pager.querySelector('.pg-next').onclick = function() {{ if(page<totalPages-1){{ page++; render(); }} }};
  }}
  render();
}}

document.addEventListener('DOMContentLoaded', function() {{
  initPager('eval-table', 'eval-pager', 3);
  initPager('trade-table', 'trade-pager', 10);
}});
</script>
</head>
<body>
<header>
  <h1><img src="/profile.png" class="profile-img" alt="">Pochaco Monitor
    <span style="font-size:0.55em; color:#475569; font-weight:400; margin-left:6px;">{version}</span></h1>
  <span><span class="health-dot"></span>갱신: {updated_at} &nbsp;|&nbsp; 30초마다 자동 새로고침</span>
</header>

<div class="grid">

  <!-- 자산 현황 -->
  <div class="card">
    <h2>💰 자산 현황</h2>
    <div class="big-num">{total_assets_fmt}</div>
    <div class="sub">KRW 잔고: {krw_fmt}</div>
    {pos_asset_line}
    {holdings_html}
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

<!-- AI 성과 평가 & 전략 조정 -->
<div style="padding: 0 20px 16px;">
  <div class="card">
    <h2>📊 AI 성과 평가 & 전략 조정</h2>
    {eval_summary_html}
    {evals_html}
    <div class="pager" id="eval-pager">
      <button class="pg-prev">&laquo; 이전</button>
      <span class="pg-info">1 / 1</span>
      <button class="pg-next">다음 &raquo;</button>
    </div>
  </div>
</div>

<!-- 거래 내역 -->
<div style="padding: 0 20px 20px;">
  <div class="card">
    <h2>🔄 최근 거래 내역</h2>
    {trades_html}
    <div class="pager" id="trade-pager">
      <button class="pg-prev">&laquo; 이전</button>
      <span class="pg-info">1 / 1</span>
      <button class="pg-next">다음 &raquo;</button>
    </div>
  </div>
</div>

<footer>pochaco — AI 자동매매 시스템 &nbsp;|&nbsp; 데이터는 30초마다 갱신됩니다</footer>
</body>
</html>"""


_expand_counter = 0


def _expandable(text: str, limit: int = 35) -> str:
    """limit자 초과 시 말줄임 + [more] 토글 버튼 HTML 반환"""
    global _expand_counter
    if not text or len(text) <= limit:
        return text
    _expand_counter += 1
    eid = f"exp{_expand_counter}"
    short = text[:limit].rstrip()
    return (
        f"<span class='expandable' id='{eid}'>"
        f"<span class='short'>{short}…"
        f"<button class='more-btn' onclick='toggleMore(this)'>more</button></span>"
        f"<span class='full'>{text}"
        f"<button class='more-btn' onclick='toggleMore(this)'>접기</button></span>"
        f"</span>"
    )


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

    # 포지션 평가액 줄 (코인명 + 평가금액 + 개수)
    pos_asset_line = ""
    pos_symbol = ""
    if pos:
        pos_symbol = pos["symbol"]
        pos_asset_line = (
            f'<div class="sub">'
            f'{pos["symbol"]} 평가: {fmt_krw(pos["units"] * pos["current_price"])}'
            f' ({pos["units"]:.6g}개)'
            f'</div>'
        )

    avg_h = perf["avg_hold_minutes"]
    avg_hold = f"{avg_h / 60:.1f}시간" if avg_h >= 60 else f"{avg_h:.0f}분"

    # 보유 코인 (1000원 이상, 포지션 코인 제외 — 중복 방지)
    holdings = [h for h in data.get("holdings", [])
                if h["krw_value"] >= 1000 and h["symbol"] != pos_symbol]
    if holdings:
        h_lines = []
        for h in holdings:
            h_lines.append(
                f'<div class="sub" style="margin-top:2px;">'
                f'{h["symbol"]} 평가: {h["krw_value"]:,.0f}원'
                f' ({h["units"]:.6g}개)'
                f'</div>'
            )
        holdings_html = "\n".join(h_lines)
    else:
        holdings_html = ""

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
        <div class="sub">{pos['symbol']} {pos['units']:.2f} 개</div>
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
        <div style="margin-top:12px;">
          <button id="liq-btn" onclick="liquidatePosition()"
            style="background:#dc2626; color:#fff; border:none; border-radius:8px;
            padding:8px 16px; font-size:0.85rem; font-weight:600; cursor:pointer;
            width:100%;">
            🔥 포지션 청산</button>
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
                f"<td>{r['date'][5:]}</td>"
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

    # 거래 내역 HTML — 2줄 레이아웃
    # 1행: 시간(2줄) / 심볼 / 가격 / 수량 / 금액
    # 2행: 구분 배지 / 비고 [more 확장]
    if data["recent_trades"]:
        rows = ""
        for t in data["recent_trades"]:
            side_class = "badge-green" if t["side"] == "buy" else "badge-red"
            side_label = "매수" if t["side"] == "buy" else "매도"
            note = t["note"] or ""
            # 시간 분리: "2026-04-03 14:16:54" → date / hms
            t_parts = t["time"].split(" ")
            t_date = t_parts[0] if len(t_parts) > 0 else t["time"]  # mm-dd
            t_hms  = t_parts[1] if len(t_parts) > 1 else ""
            note_html = _expandable(note, 35)
            rows += (
                f"<tr class='trade-row-main'>"
                f"<td><span class='time-date'>{t_date}</span>"
                f"<span class='time-hms'>{t_hms}</span></td>"
                f"<td><b>{t['symbol']}</b></td>"
                f"<td style='text-align:right'>{t['price']:,.0f}</td>"
                f"<td style='text-align:right'>{t['units']:.2f}</td>"
                f"<td style='text-align:right'>{t['krw_amount']:,.0f}</td>"
                f"</tr>"
                f"<tr class='trade-row-sub'>"
                f'<td><span class="badge {side_class}">{side_label}</span></td>'
                f"<td colspan='4'>{note_html}</td>"
                f"</tr>"
            )
        trades_html = (
            "<table id='trade-table'>"
            "<tr><th>시간</th><th>심볼</th>"
            "<th style='text-align:right'>가격(원)</th>"
            "<th style='text-align:right'>수량</th>"
            "<th style='text-align:right'>금액(원)</th></tr>"
            f"{rows}</table>"
        )
    else:
        trades_html = '<div class="no-data">거래 내역 없음</div>'

    # 성과 평가 HTML
    eval_stats = data.get("eval_stats", {})
    if eval_stats:
        eval_summary_html = (
            f'<div style="background:#0f172a; padding:12px 16px; border-radius:8px; '
            f'margin-bottom:12px; font-size:0.85rem;">'
            f'<span style="color:#facc15;">📈 최근 {eval_stats["count"]}건</span> &nbsp;|&nbsp; '
            f'승률 <b>{eval_stats["win_rate"]:.0%}</b> &nbsp;|&nbsp; '
            f'평균 수익 <span class="{"green" if eval_stats["avg_pnl_pct"] >= 0 else "red"}">'
            f'{eval_stats["avg_pnl_pct"]:+.2f}%</span> &nbsp;|&nbsp; '
            f'AI 제안 평균: 익절 <span class="green">+{eval_stats["avg_suggested_tp"]:.1f}%</span> '
            f'손절 <span class="red">{eval_stats["avg_suggested_sl"]:.1f}%</span>'
            f'</div>'
        )
    else:
        eval_summary_html = ""

    evals_list = data.get("evaluations", [])
    if evals_list:
        erows = ""
        for ev in evals_list:
            exit_cls = "green" if ev["exit_type"] == "take_profit" else "red"
            exit_label = "익절" if ev["exit_type"] == "take_profit" else "손절"
            held = ev["held_minutes"]
            held_str = f"{held/60:.1f}h" if held >= 60 else f"{held:.0f}m"
            t_parts = ev["time"].split(" ")
            t_date = t_parts[0] if len(t_parts) > 0 else ev["time"]  # mm-dd
            t_hms  = t_parts[1] if len(t_parts) > 1 else ""
            lesson_html = _expandable(ev.get("lesson", ""), 35)
            erows += (
                f"<tr class='eval-row-main'>"
                f"<td><span class='time-date'>{t_date}</span>"
                f"<span class='time-hms'>{t_hms}</span></td>"
                f"<td><b>{ev['symbol']}</b></td>"
                f'<td class="{exit_cls}">{ev["pnl_pct"]:+.2f}%</td>'
                f"<td>{held_str}</td>"
                f"<td>+{ev['original_tp']:.1f} / {ev['original_sl']:.1f}</td>"
                f"<td><b>+{ev['suggested_tp']:.1f} / {ev['suggested_sl']:.1f}</b></td>"
                f"</tr>"
                f"<tr class='eval-row-sub'>"
                f'<td><span class="badge {"badge-green" if ev["exit_type"] == "take_profit" else "badge-red"}">{exit_label}</span></td>'
                f"<td colspan='5'>{lesson_html}</td>"
                f"</tr>"
            )
        evals_html = (
            "<table id='eval-table'>"
            "<tr><th>시간</th><th>코인</th><th>수익률</th>"
            "<th>보유</th><th>설정 TP/SL</th><th>제안 TP/SL</th></tr>"
            f"{erows}</table>"
        )
    else:
        evals_html = '<div class="no-data">성과 평가 데이터 없음<br>(매매 완료 후 자동 기록)</div>'

    return _HTML_TEMPLATE.format(
        updated_at=data["updated_at"],
        version=data.get("version", ""),
        total_assets_fmt=fmt_krw(bal["total_assets"]),
        krw_fmt=fmt_krw(bal["krw"]),
        pos_asset_line=pos_asset_line,
        holdings_html=holdings_html,
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
        eval_summary_html=eval_summary_html,
        evals_html=evals_html,
    )


def _liquidate_position(client: "BithumbClient") -> dict:
    """현재 오픈 포지션을 시장가 매도 + DB 포지션 종료"""
    repo = TradeRepository()
    try:
        pos = repo.get_open_position()
        if not pos:
            return {"success": False, "error": "오픈 포지션 없음"}

        symbol = pos.symbol
        units = client.get_coin_balance(symbol)
        if units <= 0:
            repo.close_position(pos.id)
            return {"success": False, "error": f"{symbol} 실제 잔고 0 — 포지션만 종료"}

        current_price = client.get_current_price(symbol)
        krw_value = units * current_price

        sell_result = client.market_sell(symbol, units)
        if sell_result.get("status") != "0000":
            return {"success": False, "error": f"매도 실패: {sell_result}"}

        pnl_pct = (current_price - pos.buy_price) / pos.buy_price * 100
        repo.save_trade(
            symbol=symbol, side="sell",
            price=current_price, units=units,
            krw_amount=krw_value,
            note=f"대시보드 청산 ({pnl_pct:+.2f}%)",
        )
        repo.close_position(pos.id)

        krw = client.get_krw_balance()
        logger.info(
            f"[대시보드 청산] {symbol} {units}개 @ {current_price:,.0f}원 "
            f"→ {krw_value:,.0f}원 ({pnl_pct:+.2f}%)"
        )
        return {
            "success": True,
            "symbol": symbol,
            "units": round(units, 6),
            "price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "krw_received": round(krw_value, 0),
            "krw_balance": round(krw, 0),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        repo.close()


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

        elif self.path == "/profile.png":
            img_path = _APP_DIR / "profile.png"
            if img_path.exists():
                body = img_path.read_bytes()
                self._respond(200, "image/png", body)
            else:
                self._respond(404, "text/plain", b"Not Found")

        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        if self.path == "/api/liquidate":
            try:
                result = _liquidate_position(self.client)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                body = json.dumps({"success": False, "error": str(e)}).encode()
                self._respond(500, "application/json; charset=utf-8", body)
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
