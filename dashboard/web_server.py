"""간단한 HTTP 웹 대시보드 서버

GET /           — HTML 종합 대시보드 (30초 자동 갱신)
GET /experts    — HTML 전문가 실적표
GET /api/status — JSON 상태 데이터
GET /api/experts — JSON 전문가 데이터
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, parse_qs

_APP_DIR = Path(__file__).parent.parent

from config import settings
from database import TradeRepository
from database.models import Position
from strategy import cooldown as cooldown_registry
from core.llm_provider import usage_tracker

if TYPE_CHECKING:
    from core import BithumbClient
    from strategy.agent_coordinator import AgentCoordinator

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


def _to_kst(dt: datetime) -> datetime:
    """UTC naive datetime → KST datetime"""
    return dt.replace(tzinfo=timezone.utc).astimezone(_KST)


def _kst_now() -> datetime:
    return datetime.now(tz=_KST)


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


def _parse_manual_note(note: str) -> tuple[float | None, float | None, float | None]:
    """대시보드 청산 노트에서 (pnl_pct, pnl_krw, held_min) 파싱"""
    pnl_pct = pnl_krw = held_min = None
    m = re.search(r'([+-]?\d+\.?\d*)%', note)
    if m:
        pnl_pct = float(m.group(1))
    m = re.search(r'([+-]?[\d,]+)원', note)
    if m:
        pnl_krw = float(m.group(1).replace(',', ''))
    m = re.search(r'(\d+\.?\d*)분', note)
    if m:
        held_min = float(m.group(1))
    return pnl_pct, pnl_krw, held_min


def _build_json_status(client: "BithumbClient", coordinator: "AgentCoordinator | None" = None) -> dict:
    """현재 상태를 JSON 직렬화 가능한 dict로 반환 (포트폴리오 기반)"""
    from database.models import Portfolio
    repo = TradeRepository()
    try:
        krw = client.get_krw_balance()
        total = krw
        portfolio_data = None

        pf: Portfolio | None = repo.get_open_portfolio()

        # ── 거래소 실제 보유 코인 잔고 ──
        pf_symbols: set[str] = set()
        holdings = []
        actual_coin_units: dict[str, float] = {}
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
                    actual_coin_units[sym] = amt
        except Exception:
            pass

        if pf:
            positions = repo.get_portfolio_positions(pf.id)
            pf_symbols = {p.symbol for p in positions}
            total_buy = 0.0
            total_current = 0.0
            coins_data = []

            for pos in positions:
                try:
                    cur = client.get_current_price(pos.symbol)
                    actual_units = actual_coin_units.get(pos.symbol, pos.units)
                    coin_value = actual_units * cur
                    coin_pnl_pct = (cur - pos.buy_price) / pos.buy_price * 100 if pos.buy_price > 0 else 0
                    coin_pnl_krw = (cur - pos.buy_price) * actual_units
                    # 분할 매도 후 actual_units < pos.units 인 경우 cost basis 비례 조정
                    if pos.units > 0 and actual_units < pos.units * 0.99:
                        effective_buy_krw = pos.buy_krw * (actual_units / pos.units)
                    else:
                        effective_buy_krw = pos.buy_krw
                    total_buy += effective_buy_krw
                    total_current += coin_value
                    coins_data.append({
                        "symbol": pos.symbol,
                        "units": round(actual_units, 6),
                        "buy_price": pos.buy_price,
                        "buy_krw": round(pos.buy_krw, 0),
                        "current_price": cur,
                        "current_value": round(coin_value, 0),
                        "pnl_pct": round(coin_pnl_pct, 2),
                        "pnl_krw": round(coin_pnl_krw, 0),
                        "reason": pos.agent_reason or "",
                    })
                except Exception:
                    coins_data.append({
                        "symbol": pos.symbol,
                        "units": round(pos.units, 6),
                        "buy_price": pos.buy_price,
                        "buy_krw": round(pos.buy_krw, 0),
                        "current_price": pos.buy_price,
                        "current_value": round(pos.buy_krw, 0),
                        "pnl_pct": 0.0,
                        "pnl_krw": 0.0,
                        "reason": pos.agent_reason or "",
                    })
                    total_buy += pos.buy_krw
                    total_current += pos.buy_krw

            pf_pnl_pct = (total_current - total_buy) / total_buy * 100 if total_buy > 0 else 0
            pf_pnl_krw = total_current - total_buy
            total = krw + total_current
            held_min = (datetime.now(tz=timezone.utc) - pf.opened_at.replace(tzinfo=timezone.utc)).total_seconds() / 60

            portfolio_data = {
                "id": pf.id,
                "name": pf.name,
                "total_buy_krw": round(total_buy, 0),
                "total_current_value": round(total_current, 0),
                "pnl_pct": round(pf_pnl_pct, 2),
                "pnl_krw": round(pf_pnl_krw, 0),
                "take_profit_pct": pf.take_profit_pct,
                "stop_loss_pct": pf.stop_loss_pct,
                "held_minutes": round(held_min, 1),
                "agent_reason": pf.agent_reason or "",
                "llm_provider": pf.llm_provider or "",
                "coin_count": len(coins_data),
                "coins": coins_data,
                "opened_at": _to_kst(pf.opened_at).strftime("%m-%d %H:%M"),
            }

        # holdings — 포트폴리오 외 보유 코인
        for sym, amt in actual_coin_units.items():
            if sym in pf_symbols:
                continue
            try:
                px = client.get_current_price(sym)
                kv = amt * px
                if kv >= 100:
                    holdings.append({
                        "symbol": sym, "units": amt,
                        "price": px, "krw_value": round(kv, 0),
                    })
                    total += kv
            except Exception:
                holdings.append({"symbol": sym, "units": amt, "price": 0, "krw_value": 0})

        stats = repo.get_total_stats()
        recent_trades = repo.get_all_trades(limit=100)

        trades_data = [
            {
                "time": _to_kst(t.created_at).strftime("%m-%d %H:%M:%S"),
                "symbol": t.symbol,
                "side": t.side,
                "price": t.price,
                "units": round(t.units, 2),
                "krw_amount": round(t.krw_amount, 0),
                "note": t.note or "",
                "portfolio_id": t.portfolio_id,
            }
            for t in recent_trades
        ]

        initial = stats["initial_krw"]
        total_pnl_pct = (total - initial) / initial * 100 if initial > 0 else 0.0

        # 성과 평가 데이터 (포트폴리오 단위)
        recent_evals = repo.get_recent_evaluations(limit=10)
        eval_stats = repo.get_evaluation_stats(last_n=10)
        evals_data = []
        for ev in recent_evals:
            pnl_krw_est = round(ev.total_sell_krw - ev.total_buy_krw, 0) if ev.total_buy_krw else None
            try:
                ev_coins = json.loads(ev.coins_summary) if ev.coins_summary else []
            except Exception:
                ev_coins = []
            evals_data.append({
                "time": _to_kst(ev.created_at).strftime("%m-%d %H:%M:%S"),
                "portfolio_name": ev.portfolio_name,
                "exit_type": ev.exit_type,
                "pnl_pct": round(ev.pnl_pct, 2),
                "pnl_krw": pnl_krw_est,
                "total_buy_krw": round(ev.total_buy_krw, 0),
                "total_sell_krw": round(ev.total_sell_krw, 0),
                "coin_count": len(ev_coins),
                "held_minutes": round(ev.held_minutes, 1),
                "original_tp": ev.original_tp_pct,
                "original_sl": ev.original_sl_pct,
                "suggested_tp": ev.suggested_tp_pct,
                "suggested_sl": ev.suggested_sl_pct,
                "evaluation": ev.evaluation,
                "lesson": ev.lesson or "",
                "coins": ev_coins,
            })

        # 포트폴리오 히스토리
        pf_history = repo.get_portfolio_history(limit=20)
        portfolio_history = []
        for ph in pf_history:
            if ph.is_open:
                continue
            ph_held = (ph.closed_at - ph.opened_at).total_seconds() / 60 if ph.closed_at and ph.opened_at else 0
            portfolio_history.append({
                "name": ph.name,
                "total_buy_krw": round(ph.total_buy_krw, 0),
                "opened_at": _to_kst(ph.opened_at).strftime("%m-%d %H:%M"),
                "closed_at": _to_kst(ph.closed_at).strftime("%m-%d %H:%M") if ph.closed_at else "",
                "held_minutes": round(ph_held, 1),
                "tp_pct": ph.take_profit_pct,
                "sl_pct": ph.stop_loss_pct,
            })

        # 전문가 점수 추세용 이력 (최근 5건)
        agent_score_history: dict[str, list[dict]] = {}
        try:
            all_hist = repo.get_all_agent_score_history(limit=5)
            for role, rows in all_hist.items():
                agent_score_history[role] = [
                    {"score": r.score} for r in rows
                ]
        except Exception:
            pass

        # 수동 청산 이력
        manual_trades_data = []
        for t in recent_trades:
            note = t.note or ""
            if "대시보드 청산" in note:
                pnl_pct_m, pnl_krw_m, held_m = _parse_manual_note(note)
                manual_trades_data.append({
                    "time": _to_kst(t.created_at).strftime("%m-%d %H:%M:%S"),
                    "symbol": t.symbol,
                    "sell_krw": round(t.krw_amount, 0),
                    "pnl_pct": pnl_pct_m,
                    "pnl_krw": pnl_krw_m,
                    "held_min": held_m,
                })

        return {
            "updated_at": _kst_now().strftime("%m-%d %H:%M:%S"),
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
            "portfolio": portfolio_data,
            "portfolio_history": portfolio_history,
            "recent_trades": trades_data,
            "evaluations": evals_data,
            "manual_trades": manual_trades_data,
            "eval_stats": eval_stats,
            "agent_scores": coordinator.get_agent_scores() if coordinator else {},
            "agent_score_history": agent_score_history,
        }
    finally:
        repo.close()


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
  .green {{ color: #f87171; }}
  .red   {{ color: #60a5fa; }}
  .gray  {{ color: #64748b; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 9999px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-green {{ background: #450a0a; color: #f87171; }}
  .badge-red   {{ background: #1e3a5f; color: #60a5fa; }}
  .badge-open   {{ background: #14532d; color: #86efac; }}
  .badge-manual {{ background: #422006; color: #fb923c; }}
  /* 포트폴리오 거래 1줄 행 */
  .pf-tx-list {{ display: flex; flex-direction: column; }}
  .pf-tx-row  {{ display: flex; align-items: center; padding: 7px 12px;
                 border-bottom: 1px solid #1e293b; cursor: pointer;
                 transition: background 0.1s; gap: 10px; }}
  .pf-tx-row:hover {{ background: rgba(51,65,85,0.4); }}
  .pf-tx-row.pf-tx-open {{ background: rgba(34,197,94,0.06);
                           border-left: 3px solid #22c55e; padding-left: 9px; }}
  .pf-tx-dt   {{ min-width: 44px; text-align: center; line-height: 1.35; flex-shrink: 0; }}
  .pf-tx-dt b {{ display: block; color: #fff; font-size: 0.78rem; font-weight: 600; }}
  .pf-tx-dt small {{ display: block; color: #64748b; font-size: 0.7rem; }}
  .pf-tx-nm   {{ flex: 1; font-size: 0.88rem; min-width: 0; white-space: nowrap;
                 overflow: hidden; text-overflow: ellipsis; }}
  .pf-tx-held {{ color: #94a3b8; font-size: 0.75rem; min-width: 44px;
                 text-align: right; white-space: nowrap; flex-shrink: 0; }}
  .pf-tx-pnl  {{ min-width: 74px; text-align: right; line-height: 1.35; flex-shrink: 0; }}
  .pf-tx-pnl-r {{ font-size: 0.88rem; font-weight: 700; }}
  .pf-tx-pnl-k {{ font-size: 0.7rem; }}
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
  .pnl-link {{ cursor: pointer; text-decoration: underline dotted; }}
  .pnl-link:hover {{ opacity: 0.8; }}
{extra_css}
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

/* 페이지네이션 — rowStep: 1=단일행, 2=2행묶음(기본) */
function initPager(tableId, pagerId, rowsPerPage, rowStep) {{
  var step = rowStep || 2;
  var table = document.getElementById(tableId);
  if (!table) return;
  var tbody = table.querySelector('tbody') || table;
  var allRows = Array.from(tbody.querySelectorAll('tr:not(:first-child)'));
  var groups = [];
  for (var i = 0; i < allRows.length; i += step) {{
    var grp = [];
    for (var k = 0; k < step; k++) {{ if (allRows[i+k]) grp.push(allRows[i+k]); }}
    groups.push(grp);
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

/* 카드 리스트 페이지네이션 */
function initCardPager(listId, pagerId, cardsPerPage) {{
  var list = document.getElementById(listId);
  if (!list) return;
  var cards = Array.from(list.children);
  var totalPages = Math.ceil(cards.length / cardsPerPage) || 1;
  var page = 0;
  function render() {{
    cards.forEach(function(c) {{ c.style.display = 'none'; }});
    var start = page * cardsPerPage;
    var end = Math.min(start + cardsPerPage, cards.length);
    for (var j = start; j < end; j++) {{ cards[j].style.display = ''; }}
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

/* 모달이 열려있을 때는 자동 새로고침 일시 정지 */
function _isAnyModalOpen() {{
  var ids = ['pf-tx-modal', 'eval-detail-modal', 'prompt-modal', 'chat-modal', 'coin-profile-modal'];
  for (var i=0; i<ids.length; i++) {{
    var m = document.getElementById(ids[i]);
    if (!m) continue;
    var disp = m.style.display;
    if (disp === 'flex' || disp === 'block') return true;
  }}
  return false;
}}
function _scheduleAutoRefresh(intervalMs) {{
  setInterval(function() {{
    if (_isAnyModalOpen()) {{
      var hint = document.getElementById('refresh-hint');
      if (hint) hint.textContent = '⏸ 팝업 닫으면 새로고침';
      return;
    }}
    location.reload();
  }}, intervalMs || 30000);
}}

document.addEventListener('DOMContentLoaded', function() {{
  initPager('eval-table', 'eval-pager', 5, 1);
  initCardPager('trade-list', 'trade-pager', 10);
  _scheduleAutoRefresh(30000);
}});
{extra_js}
</script>
</head>
<body>
<header>
  <div>
    <h1><img src="/profile.png" class="profile-img" alt="">Pochaco Monitor
      <span style="font-size:0.55em; color:#475569; font-weight:400; margin-left:6px;">{version}</span></h1>
    <div style="margin-top:6px; font-size:0.82rem; color:#64748b;">
      <span class="health-dot"></span>갱신: {updated_at} &nbsp;|&nbsp;
      <span id="refresh-hint">30초마다 자동 새로고침</span>
    </div>
  </div>
  <nav style="display:flex; gap:8px; align-items:center;">
    <a href="/" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:{nav_dashboard_bg};">종합 대시보드</a>
    <a href="/experts" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:{nav_experts_bg};">전문가 실적표</a>
    <a href="/system" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:{nav_system_bg};">시스템</a>
  </nav>
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

{manual_trades_section}

<!-- 포트폴리오 거래 내역 -->
<div style="padding: 0 20px 20px;">
  <div class="card">
    <h2>📋 포트폴리오 거래 내역</h2>
    {trades_html}
    <div class="pager" id="trade-pager">
      <button class="pg-prev">&laquo; 이전</button>
      <span class="pg-info">1 / 1</span>
      <button class="pg-next">다음 &raquo;</button>
    </div>
  </div>
</div>

<!-- 전문가 점수 요약 -->
<div style="padding: 0 20px 16px;">
  <div class="card">
    <h2 style="display:flex;align-items:center;justify-content:space-between;">
      <span>🤖 전문가 Agent 점수</span>
      <a href="/experts" title="전문가 실적표" style="color:#38bdf8;font-size:1rem;text-decoration:none;opacity:0.7;" onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=0.7">&#x1F517;</a>
    </h2>
    {agent_scores_html}
  </div>
</div>

<script>var _evalPopup = {eval_js_data}; var _pfTxPopup = {portfolio_tx_js_data};</script>
{portfolio_tx_modal}
{eval_detail_modal}
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
    pf = data.get("portfolio")

    def fmt_krw(v: float) -> str:
        av = abs(v)
        if av >= 1_0000_0000:
            return f"{v / 1_0000_0000:.2f}억 원"
        if av >= 10_000:
            return f"{v / 10_000:.1f}만 원"
        return f"{v:,.0f} 원"

    total_pnl_color = "green" if perf["total_pnl_pct"] >= 0 else "red"

    # 포트폴리오 평가액 줄
    pos_asset_line = ""
    pos_symbol = ""  # 호환용 (빈 문자열)
    if pf:
        pos_asset_line = (
            f'<div class="sub">'
            f'포트폴리오 평가: {fmt_krw(pf["total_current_value"])} '
            f'({pf["coin_count"]}개 코인)'
            f'</div>'
        )

    avg_h = perf["avg_hold_minutes"]
    avg_hold = f"{avg_h / 60:.1f}시간" if avg_h >= 60 else f"{avg_h:.0f}분"

    # 보유 코인 (포트폴리오 외)
    holdings = [h for h in data.get("holdings", []) if h["krw_value"] >= 1000]
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

    # 포트폴리오 HTML
    if pf:
        pnl_pct = pf["pnl_pct"]
        pnl_color = "green" if pnl_pct >= 0 else "red"
        held = pf["held_minutes"]
        held_str = f"{held / 60:.1f}시간" if held >= 60 else f"{held:.0f}분"
        progress = min(1.0, max(0.0, pnl_pct / pf["take_profit_pct"])) if pf["take_profit_pct"] > 0 else 0.0
        bar_color = "#f87171" if pnl_pct >= 0 else "#60a5fa"

        # 개별 코인 테이블
        coin_rows = ""
        for c in pf.get("coins", []):
            c_color = "green" if c["pnl_pct"] >= 0 else "red"
            coin_rows += (
                f'<tr>'
                f'<td><b>{c["symbol"]}</b></td>'
                f'<td style="text-align:right">{c["buy_price"]:,.0f}</td>'
                f'<td style="text-align:right">{c["current_price"]:,.0f}</td>'
                f'<td style="text-align:right" class="{c_color}">{c["pnl_pct"]:+.2f}%</td>'
                f'<td style="text-align:right" class="{c_color}">{c["pnl_krw"]:+,.0f}</td>'
                f'</tr>'
            )

        coins_table = (
            '<table style="margin-top:10px;">'
            '<tr><th>코인</th><th style="text-align:right">매수가</th>'
            '<th style="text-align:right">현재가</th>'
            '<th style="text-align:right">수익률</th>'
            '<th style="text-align:right">손익(원)</th></tr>'
            f'{coin_rows}</table>'
        )

        position_html = f"""
        <div class="big-num {pnl_color}">{pnl_pct:+.2f}%</div>
        <div class="sub" style="font-size:1.1em; font-weight:600;">{pf['name']}</div>
        <div class="sub">{pf['coin_count']}개 코인 | 투입 {pf['total_buy_krw']:,.0f}원</div>
        <br>
        <div class="stat-row">
          <span class="stat-label">평가 손익</span>
          <span class="stat-value {pnl_color}">{pf['pnl_krw']:+,.0f} 원</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">익절 / 손절</span>
          <span class="stat-value">
            <span class="green">+{pf['take_profit_pct']}%</span> /
            <span class="red">{pf['stop_loss_pct']}%</span>
            <span style="color:#64748b; font-size:0.8em;"> (분할: -1%/-1.5%/-2%)</span>
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
        {coins_table}
        <div style="margin-top:10px; font-size:0.78rem; color:#64748b; font-style:italic;">
          {(pf.get('agent_reason') or '')[:120]}
        </div>
        <div style="margin-top:12px;">
          <button id="liq-btn" onclick="liquidatePosition()"
            style="background:#dc2626; color:#fff; border:none; border-radius:8px;
            padding:8px 16px; font-size:0.85rem; font-weight:600; cursor:pointer;
            width:100%;">
            🔥 포트폴리오 청산</button>
        </div>
        """
    else:
        position_html = '<div class="no-data">현재 활성 포트폴리오 없음<br>AI 포트폴리오 구성 대기 중...</div>'

    # 포트폴리오 거래 내역 — 카드 2줄 형태 (클릭 → 팝업)
    pf_tx_popup_list: list[dict] = []
    pf_tx_cards = ""

    def _fmt_held(minutes: float) -> str:
        if minutes >= 60:
            return f"{minutes / 60:.1f}H"
        return f"{minutes:.0f}M"

    # 현재 보유 중인 포트폴리오 (최상단)
    if pf:
        idx = len(pf_tx_popup_list)
        open_pnl_krw = pf["pnl_krw"]
        open_pnl_color = "green" if open_pnl_krw > 0 else ("red" if open_pnl_krw < 0 else "gray")
        held_str_open = _fmt_held(pf["held_minutes"])
        _dt_parts = pf["opened_at"].split(" ")
        _dt_date = _dt_parts[0] if _dt_parts else ""
        _dt_time = _dt_parts[1] if len(_dt_parts) > 1 else ""
        pf_tx_cards += (
            f"<div class='pf-tx-row pf-tx-open' onclick='showPfTx({idx})'>"
            f"<div class='pf-tx-dt'><b>{_dt_date}</b><small>{_dt_time}</small></div>"
            f"<div class='pf-tx-nm'><b>[{pf['coin_count']}] {pf['name']}</b></div>"
            f"<span class='pf-tx-held'>⏱ {held_str_open}</span>"
            f"<div class='pf-tx-pnl'>"
            f"<div class='pf-tx-pnl-r {open_pnl_color}'>{pf['pnl_pct']:+.2f}%</div>"
            f"<div class='pf-tx-pnl-k {open_pnl_color}'>{open_pnl_krw:+,.0f}원</div>"
            f"</div>"
            f"</div>"
        )
        pf_tx_popup_list.append({
            "name": pf["name"],
            "is_open": True,
            "opened_at": pf["opened_at"],
            "closed_at": None,
            "coin_count": pf["coin_count"],
            "total_buy_krw": pf["total_buy_krw"],
            "total_sell_krw": None,
            "pnl_pct": pf["pnl_pct"],
            "pnl_krw": pf["pnl_krw"],
            "held_minutes": pf["held_minutes"],
            "exit_type": "open",
            "take_profit_pct": pf["take_profit_pct"],
            "stop_loss_pct": pf["stop_loss_pct"],
            "coins": pf.get("coins", []),
            "evaluation": "",
            "lesson": "",
        })

    # 종료된 포트폴리오 (평가 기록 기반)
    for ev in data.get("evaluations", []):
        idx = len(pf_tx_popup_list)
        if ev["exit_type"] == "take_profit":
            exit_kr, exit_class = "익절", "badge-green"
        elif ev["exit_type"] == "manual":
            exit_kr, exit_class = "수동", "badge-manual"
        else:
            exit_kr, exit_class = "손절", "badge-red"
        coin_count = ev.get("coin_count", "?")
        pnl_krw = ev.get("pnl_krw") or 0
        pnl_color = "green" if pnl_krw > 0 else ("red" if pnl_krw < 0 else "gray")
        held_str_closed = _fmt_held(ev["held_minutes"])
        _dt_parts = ev["time"].split(" ")
        _dt_date = _dt_parts[0] if _dt_parts else ""
        _dt_time_raw = _dt_parts[1] if len(_dt_parts) > 1 else ""
        _dt_time = ":".join(_dt_time_raw.split(":")[:2])  # 초 제거
        pf_tx_cards += (
            f"<div class='pf-tx-row' onclick='showPfTx({idx})'>"
            f"<div class='pf-tx-dt'><b>{_dt_date}</b><small>{_dt_time}</small></div>"
            f"<div class='pf-tx-nm'><b>[{coin_count}] {ev['portfolio_name']}</b></div>"
            f"<span class='pf-tx-held'>⏱ {held_str_closed}</span>"
            f"<div class='pf-tx-pnl'>"
            f"<div class='pf-tx-pnl-r {pnl_color}'>{ev['pnl_pct']:+.2f}%</div>"
            f"<div class='pf-tx-pnl-k {pnl_color}'>{pnl_krw:+,.0f}원</div>"
            f"</div>"
            f"</div>"
        )
        pf_tx_popup_list.append({
            "name": ev["portfolio_name"],
            "is_open": False,
            "opened_at": "",
            "closed_at": ev["time"],
            "coin_count": coin_count,
            "total_buy_krw": ev.get("total_buy_krw", 0),
            "total_sell_krw": ev.get("total_sell_krw", 0),
            "pnl_pct": ev["pnl_pct"],
            "pnl_krw": ev.get("pnl_krw") or 0,
            "held_str": held_str_closed,
            "exit_type": ev["exit_type"],
            "take_profit_pct": ev.get("original_tp", ""),
            "stop_loss_pct": ev.get("original_sl", ""),
            "coins": ev.get("coins", []),
            "evaluation": ev.get("evaluation", ""),
            "lesson": ev.get("lesson", ""),
        })

    if pf_tx_cards:
        trades_html = f"<div id='trade-list' class='pf-tx-list'>{pf_tx_cards}</div>"
    else:
        trades_html = '<div class="no-data">포트폴리오 거래 내역 없음</div>'

    portfolio_tx_js_data = json.dumps(pf_tx_popup_list, ensure_ascii=False)

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
    eval_popup_list: list[dict] = []
    if evals_list:
        erows = ""
        for idx, ev in enumerate(evals_list):
            pnl_color = "green" if ev["pnl_pct"] >= 0 else "red"
            t_parts = ev["time"].split(" ")
            t_date = t_parts[0] if len(t_parts) > 0 else ev["time"]
            t_hms  = t_parts[1] if len(t_parts) > 1 else ""
            pnl_krw_str = f'{ev["pnl_krw"]:+,.0f}원' if ev.get("pnl_krw") is not None else "—"
            ev_label = ev.get("portfolio_name") or ev.get("symbol", "")
            erows += (
                f"<tr>"
                f"<td><span class='time-date'>{t_date}</span>"
                f"<span class='time-hms'>{t_hms}</span></td>"
                f"<td><b>{ev_label}</b></td>"
                f'<td class="{pnl_color} pnl-link" onclick="showEvalDetail({idx})">'
                f'{ev["pnl_pct"]:+.2f}%</td>'
                f'<td class="{pnl_color}">{pnl_krw_str}</td>'
                f"</tr>"
            )
            held = ev["held_minutes"]
            held_str_popup = f"{held/60:.1f}시간" if held >= 60 else f"{held:.0f}분"
            if ev["exit_type"] == "take_profit":
                exit_label = "익절"
            elif ev["exit_type"] == "manual":
                exit_label = "수동청산"
            else:
                exit_label = "손절"
            eval_popup_list.append({
                "symbol": ev_label,
                "time": ev["time"],
                "exit": exit_label,
                "pnl_pct": ev["pnl_pct"],
                "held": held_str_popup,
                "orig_tp": ev.get("original_tp") or "",
                "orig_sl": ev.get("original_sl") or "",
                "sug_tp": ev.get("suggested_tp") or "",
                "sug_sl": ev.get("suggested_sl") or "",
                "evaluation": ev.get("evaluation") or "",
                "lesson": ev.get("lesson") or "",
            })
        evals_html = (
            "<table id='eval-table'>"
            "<tr><th>시간</th><th>포트폴리오</th><th>수익률</th><th>수익금액</th></tr>"
            f"{erows}</table>"
        )
    else:
        evals_html = '<div class="no-data">성과 평가 데이터 없음<br>(매매 완료 후 자동 기록)</div>'

    eval_js_data = json.dumps(eval_popup_list, ensure_ascii=False)

    # 전문가 점수 요약 HTML
    agent_scores = data.get("agent_scores", {})
    _role_names = {
        "market_analyst": "시장 분석가",
        "asset_manager": "자산 운용가",
        "investment_strategist": "투자 전문가",
        "buy_strategist": "매수 전문가",
        "sell_strategist": "매도 전문가",
        "portfolio_evaluator": "포트폴리오 평가가",
        "coin_profile_analyst": "특성 분석가",
    }
    if agent_scores:
        # 최근 5건 평가 이력에서 역할별 점수 추세 계산
        experts_data_cache = data.get("agent_score_history", {})
        score_cards = '<div style="display:flex; gap:10px; flex-wrap:wrap;">'
        for role, name in _role_names.items():
            sc = agent_scores.get(role, 50.0)
            if sc >= 70:
                sc_color = "#4ade80"
            elif sc >= 40:
                sc_color = "#facc15"
            else:
                sc_color = "#f87171"
            # 추세 표기: 이전 점수 대비
            history = experts_data_cache.get(role, [])
            trend_html = ""
            if len(history) >= 2:
                recent5 = history[:5]
                avg5 = sum(h["score"] for h in recent5) / len(recent5)
                oldest = recent5[-1]["score"]
                newest = recent5[0]["score"]
                if newest > oldest + 1:
                    trend_html = '<span style="color:#f87171;font-size:0.75rem;">▲</span>'
                elif newest < oldest - 1:
                    trend_html = '<span style="color:#60a5fa;font-size:0.75rem;">▼</span>'
                else:
                    trend_html = '<span style="color:#64748b;font-size:0.75rem;">—</span>'
                avg_html = f'<div style="font-size:0.7rem;color:#64748b;margin-top:2px;">최근{len(recent5)}건 평균 {avg5:.0f}</div>'
            else:
                avg_html = ""
            score_cards += (
                f'<div style="flex:1; min-width:120px; background:#0f172a; border-radius:8px; '
                f'padding:12px; text-align:center; border:1px solid #334155;">'
                f'<div style="font-size:0.75rem; color:#94a3b8;">{name}</div>'
                f'<div style="font-size:1.6rem; font-weight:700; color:{sc_color};">{sc:.0f}'
                f'<span style="font-size:0.9rem;">{trend_html}</span></div>'
                f'{avg_html}'
                f'</div>'
            )
        score_cards += '</div>'
        agent_scores_html = score_cards
    else:
        agent_scores_html = '<div class="no-data">전문가 평가 데이터 없음<br>(6시간 주기 평가 후 표시)</div>'

    # 수동 청산 이력 HTML
    manual_list = data.get("manual_trades", [])
    if manual_list:
        mrows = ""
        for m in manual_list:
            pnl_c = "green" if (m.get("pnl_pct") or 0) >= 0 else "red"
            t_parts = m["time"].split(" ")
            t_date = t_parts[0] if len(t_parts) > 0 else m["time"]
            t_hms  = t_parts[1] if len(t_parts) > 1 else ""
            pnl_str = f'{m["pnl_pct"]:+.2f}%' if m.get("pnl_pct") is not None else "—"
            pnl_krw_str = f'{m["pnl_krw"]:+,.0f}원' if m.get("pnl_krw") is not None else "—"
            held_str = f'{m["held_min"]:.0f}분' if m.get("held_min") is not None else "—"
            mrows += (
                f"<tr>"
                f"<td><span class='time-date'>{t_date}</span>"
                f"<span class='time-hms'>{t_hms}</span></td>"
                f"<td><b>{m['symbol']}</b></td>"
                f'<td class="{pnl_c}">{pnl_str}</td>'
                f'<td class="{pnl_c}">{pnl_krw_str}</td>'
                f"<td>{held_str}</td>"
                f"</tr>"
            )
        manual_trades_section = (
            '<div style="padding: 0 20px 16px;">'
            '<div class="card">'
            '<h2>🖐 수동 청산 이력</h2>'
            "<table><tr><th>시간</th><th>코인</th><th>수익률</th>"
            "<th>수익금액</th><th>보유</th></tr>"
            f"{mrows}</table></div></div>"
        )
    else:
        manual_trades_section = ""

    # 포트폴리오 거래 팝업 모달 HTML
    portfolio_tx_modal = (
        '<div id="pf-tx-modal" style="display:none;position:fixed;inset:0;'
        'background:rgba(0,0,0,0.75);z-index:1000;align-items:center;justify-content:center;">'
        '<div style="background:#1e293b;border-radius:12px;border:1px solid #334155;'
        'width:92%;max-width:640px;max-height:85vh;overflow:hidden;display:flex;flex-direction:column;">'
        '<div style="padding:14px 20px;border-bottom:1px solid #334155;'
        'display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">'
        '<span style="font-size:1rem;font-weight:600;color:#e2e8f0;">📋 포트폴리오 상세</span>'
        '<button onclick="closePfTx()" style="background:none;border:none;'
        'color:#64748b;font-size:1.5rem;cursor:pointer;line-height:1;padding:2px 6px;">&#215;</button>'
        '</div>'
        '<div id="pf-tx-content" style="padding:16px 20px;font-size:0.85rem;overflow-y:auto;"></div>'
        '<div style="padding:12px 20px;border-top:1px solid #334155;'
        'display:flex;justify-content:flex-end;flex-shrink:0;">'
        '<button onclick="closePfTx()" style="background:#334155;color:#e2e8f0;'
        'border:none;border-radius:6px;padding:8px 18px;font-size:0.85rem;'
        'font-weight:600;cursor:pointer;">닫기</button>'
        '</div></div></div>'
    )

    # eval 상세 팝업 JS (extra_js에 삽입 — 일반 문자열, 중괄호 이스케이프 불필요)
    extra_css = ""
    extra_js = """
function showPfTx(idx) {
  var d = _pfTxPopup[idx];
  if (!d) return;
  var pnlColor = d.pnl_pct >= 0 ? '#f87171' : '#60a5fa';
  var sign = d.pnl_pct >= 0 ? '+' : '';
  var statusBadge = d.is_open
    ? '<span style="background:#1d4ed8;color:#bfdbfe;padding:2px 8px;border-radius:4px;font-size:0.78rem;">보유 중</span>'
    : (d.exit_type === 'take_profit'
      ? '<span style="background:#166534;color:#bbf7d0;padding:2px 8px;border-radius:4px;font-size:0.78rem;">익절</span>'
      : d.exit_type === 'manual'
      ? '<span style="background:#7c2d12;color:#fdba74;padding:2px 8px;border-radius:4px;font-size:0.78rem;">수동청산</span>'
      : '<span style="background:#991b1b;color:#fecaca;padding:2px 8px;border-radius:4px;font-size:0.78rem;">손절</span>');
  var timeStr = d.is_open ? ('매수: ' + d.opened_at) : ('종료: ' + d.closed_at);
  var heldStr = d.is_open
    ? (d.held_minutes >= 60 ? (d.held_minutes / 60).toFixed(1) + '시간' : Math.round(d.held_minutes) + '분')
    : (d.held_str || '—');
  var buyFmt = d.total_buy_krw ? d.total_buy_krw.toLocaleString('ko-KR') + '원' : '—';
  var sellFmt = d.total_sell_krw ? d.total_sell_krw.toLocaleString('ko-KR') + '원' : '—';
  var pnlKrwFmt = d.pnl_krw ? (d.pnl_krw >= 0 ? '+' : '') + d.pnl_krw.toLocaleString('ko-KR') + '원' : '—';
  var tpSlStr = (d.take_profit_pct ? '<span style="color:#f87171">+' + d.take_profit_pct + '%</span>' : '—')
    + ' / ' + (d.stop_loss_pct ? '<span style="color:#60a5fa">' + d.stop_loss_pct + '%</span>' : '—');

  var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">'
    + '<span style="font-size:1rem;font-weight:700;">' + d.name + '</span>' + statusBadge + '</div>'
    + '<div class="stat-row"><span class="stat-label">시간</span><span class="stat-value">' + timeStr + '</span></div>'
    + '<div class="stat-row"><span class="stat-label">보유 시간</span><span class="stat-value">' + heldStr + '</span></div>'
    + '<div class="stat-row"><span class="stat-label">종목 수</span><span class="stat-value">' + d.coin_count + '개</span></div>'
    + '<div class="stat-row"><span class="stat-label">매수 금액</span><span class="stat-value">' + buyFmt + '</span></div>'
    + '<div class="stat-row"><span class="stat-label">매도 금액</span><span class="stat-value">' + sellFmt + '</span></div>'
    + '<div class="stat-row"><span class="stat-label">수익률</span><span class="stat-value" style="color:' + pnlColor + '">' + sign + d.pnl_pct.toFixed(2) + '%</span></div>'
    + '<div class="stat-row"><span class="stat-label">손익(원)</span><span class="stat-value" style="color:' + pnlColor + '">' + pnlKrwFmt + '</span></div>'
    + '<div class="stat-row"><span class="stat-label">TP / SL 설정</span><span class="stat-value">' + tpSlStr + '</span></div>';

  // 코인 상세 테이블
  if (d.coins && d.coins.length > 0) {
    html += '<div style="margin-top:12px;font-size:0.8rem;color:#94a3b8;font-weight:600;">코인별 상세</div>';
    if (d.is_open) {
      html += '<table style="margin-top:6px;width:100%;font-size:0.8rem;">'
        + '<tr style="color:#64748b;"><th style="text-align:left;">코인</th>'
        + '<th style="text-align:right;">매수가</th><th style="text-align:right;">현재가</th>'
        + '<th style="text-align:right;">수익률</th><th style="text-align:right;">손익(원)</th></tr>';
      for (var i = 0; i < d.coins.length; i++) {
        var c = d.coins[i];
        var cc = c.pnl_pct >= 0 ? '#f87171' : '#60a5fa';
        html += '<tr><td><b>' + c.symbol + '</b></td>'
          + '<td style="text-align:right">' + (c.buy_price || 0).toLocaleString('ko-KR') + '</td>'
          + '<td style="text-align:right">' + (c.current_price || 0).toLocaleString('ko-KR') + '</td>'
          + '<td style="text-align:right;color:' + cc + '">' + (c.pnl_pct >= 0 ? '+' : '') + (c.pnl_pct || 0).toFixed(2) + '%</td>'
          + '<td style="text-align:right;color:' + cc + '">' + (c.pnl_krw >= 0 ? '+' : '') + (c.pnl_krw || 0).toLocaleString('ko-KR') + '</td>'
          + '</tr>';
      }
      html += '</table>';
    } else {
      html += '<table style="margin-top:6px;width:100%;font-size:0.8rem;">'
        + '<tr style="color:#64748b;"><th style="text-align:left;">코인</th>'
        + '<th style="text-align:right;">매수(원)</th><th style="text-align:right;">매도(원)</th>'
        + '<th style="text-align:right;">수익률</th></tr>';
      for (var j = 0; j < d.coins.length; j++) {
        var cr = d.coins[j];
        var crc = cr.pnl_pct >= 0 ? '#f87171' : '#60a5fa';
        html += '<tr><td><b>' + cr.symbol + '</b></td>'
          + '<td style="text-align:right">' + (cr.buy_krw || 0).toLocaleString('ko-KR') + '</td>'
          + '<td style="text-align:right">' + (cr.sell_krw || 0).toLocaleString('ko-KR') + '</td>'
          + '<td style="text-align:right;color:' + crc + '">' + (cr.pnl_pct >= 0 ? '+' : '') + (cr.pnl_pct || 0).toFixed(2) + '%</td>'
          + '</tr>';
      }
      html += '</table>';
    }
  }

  if (d.evaluation) {
    html += '<div style="margin-top:10px;padding:10px;background:#0f172a;border-radius:6px;'
      + 'font-size:0.82rem;color:#94a3b8;white-space:pre-wrap;">' + d.evaluation + '</div>';
  }
  if (d.lesson) {
    html += '<div style="margin-top:8px;font-size:0.78rem;color:#64748b;font-style:italic;">' + d.lesson + '</div>';
  }

  document.getElementById('pf-tx-content').innerHTML = html;
  document.getElementById('pf-tx-modal').style.display = 'flex';
}
function closePfTx() {
  document.getElementById('pf-tx-modal').style.display = 'none';
}
document.addEventListener('DOMContentLoaded', function() {
  var ptm = document.getElementById('pf-tx-modal');
  if (ptm) ptm.addEventListener('click', function(e) { if (e.target === this) closePfTx(); });
});
function showEvalDetail(idx) {
  var d = _evalPopup[idx];
  if (!d) return;
  var pnlColor = d.pnl_pct >= 0 ? '#f87171' : '#60a5fa';
  var sign = d.pnl_pct >= 0 ? '+' : '';
  document.getElementById('edm-content').innerHTML =
    '<div class="stat-row"><span class="stat-label">코인 / 결과</span>' +
    '<span class="stat-value">' + d.symbol + ' \u2014 ' + d.exit + '</span></div>' +
    '<div class="stat-row"><span class="stat-label">수익률</span>' +
    '<span class="stat-value" style="color:' + pnlColor + '">' + sign + d.pnl_pct.toFixed(2) + '%</span></div>' +
    '<div class="stat-row"><span class="stat-label">보유 시간</span>' +
    '<span class="stat-value">' + d.held + '</span></div>' +
    '<div class="stat-row"><span class="stat-label">설정 TP / SL</span>' +
    '<span class="stat-value"><span style="color:#f87171">+' + d.orig_tp + '%</span>' +
    ' / <span style="color:#60a5fa">' + d.orig_sl + '%</span></span></div>' +
    '<div class="stat-row"><span class="stat-label">제안 TP / SL</span>' +
    '<span class="stat-value"><b><span style="color:#f87171">+' + d.sug_tp + '%</span>' +
    ' / <span style="color:#60a5fa">' + d.sug_sl + '%</span></b></span></div>' +
    (d.evaluation ? '<div style="margin-top:10px;padding:10px;background:#0f172a;' +
    'border-radius:6px;font-size:0.82rem;color:#94a3b8;white-space:pre-wrap;">' +
    d.evaluation + '</div>' : '') +
    (d.lesson ? '<div style="margin-top:8px;font-size:0.78rem;color:#64748b;' +
    'font-style:italic;">' + d.lesson + '</div>' : '');
  document.getElementById('eval-detail-modal').style.display = 'flex';
}
function closeEvalDetail() {
  document.getElementById('eval-detail-modal').style.display = 'none';
}
document.addEventListener('DOMContentLoaded', function() {
  var edm = document.getElementById('eval-detail-modal');
  if (edm) edm.addEventListener('click', function(e) { if (e.target === this) closeEvalDetail(); });
});
"""
    eval_detail_modal = (
        '<div id="eval-detail-modal" style="display:none;position:fixed;inset:0;'
        'background:rgba(0,0,0,0.75);z-index:1000;align-items:center;justify-content:center;">'
        '<div style="background:#1e293b;border-radius:12px;border:1px solid #334155;'
        'width:90%;max-width:500px;overflow:hidden;">'
        '<div style="padding:16px 20px;border-bottom:1px solid #334155;'
        'display:flex;justify-content:space-between;align-items:center;">'
        '<span style="font-size:1rem;font-weight:600;color:#e2e8f0;">📊 상세 평가</span>'
        '<button onclick="closeEvalDetail()" style="background:none;border:none;'
        'color:#64748b;font-size:1.5rem;cursor:pointer;line-height:1;padding:2px 6px;">&#215;</button>'
        '</div>'
        '<div id="edm-content" style="padding:16px 20px;font-size:0.85rem;"></div>'
        '<div style="padding:12px 20px;border-top:1px solid #334155;'
        'display:flex;justify-content:flex-end;">'
        '<button onclick="closeEvalDetail()" style="background:#334155;color:#e2e8f0;'
        'border:none;border-radius:6px;padding:8px 18px;font-size:0.85rem;'
        'font-weight:600;cursor:pointer;">닫기</button>'
        '</div></div></div>'
    )

    return _HTML_TEMPLATE.format(
        updated_at=data["updated_at"],
        version=data.get("version", ""),
        nav_dashboard_bg="#334155",
        nav_experts_bg="transparent",
        nav_system_bg="transparent",
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
        trades_html=trades_html,
        eval_summary_html=eval_summary_html,
        evals_html=evals_html,
        agent_scores_html=agent_scores_html,
        eval_js_data=eval_js_data,
        eval_detail_modal=eval_detail_modal,
        portfolio_tx_js_data=portfolio_tx_js_data,
        portfolio_tx_modal=portfolio_tx_modal,
        manual_trades_section=manual_trades_section,
        extra_css=extra_css,
        extra_js=extra_js,
    )


def _render_system_page() -> str:
    """시스템 페이지 — LLM 토큰 사용량 & 비용 대시보드"""
    from core.llm_provider import usage_tracker
    stats = usage_tracker.get_stats()

    version = _VERSION
    now_str = _kst_now().strftime("%m-%d %H:%M:%S")

    # ── 요약 카드 ──
    total_calls    = stats["total_calls"]
    total_in       = stats["total_input_tokens"]
    total_out      = stats["total_output_tokens"]
    total_tok      = stats["total_tokens"]
    cost_usd       = stats["total_cost_usd"]
    cost_krw       = int(stats["total_cost_krw"])
    session_start  = stats["session_start"]

    summary_cards = f"""
    <div style="display:flex; flex-wrap:wrap; gap:14px; margin-bottom:24px;">
      <div class="sys-card">
        <div class="sys-card-label">세션 시작</div>
        <div class="sys-card-val" style="font-size:1.1rem;">{session_start}</div>
      </div>
      <div class="sys-card">
        <div class="sys-card-label">총 LLM 호출</div>
        <div class="sys-card-val">{total_calls:,}</div>
      </div>
      <div class="sys-card">
        <div class="sys-card-label">Input 토큰</div>
        <div class="sys-card-val">{total_in:,}</div>
      </div>
      <div class="sys-card">
        <div class="sys-card-label">Output 토큰</div>
        <div class="sys-card-val">{total_out:,}</div>
      </div>
      <div class="sys-card">
        <div class="sys-card-label">총 토큰</div>
        <div class="sys-card-val">{total_tok:,}</div>
      </div>
      <div class="sys-card" style="border-color:#38bdf8;">
        <div class="sys-card-label">예상 비용 (USD)</div>
        <div class="sys-card-val" style="color:#38bdf8;">${cost_usd:.4f}</div>
      </div>
      <div class="sys-card" style="border-color:#4ade80;">
        <div class="sys-card-label">예상 비용 (KRW)</div>
        <div class="sys-card-val" style="color:#4ade80;">₩{cost_krw:,}</div>
      </div>
    </div>
    """

    # ── Agent별 집계 테이블 ──
    by_agent = stats.get("by_agent", {})
    agent_rows = ""
    for agent_name, ag in by_agent.items():
        ag_cost_krw = int(ag["cost_usd"] * 1380)
        agent_rows += (
            f'<tr>'
            f'<td>{agent_name}</td>'
            f'<td style="text-align:right;">{ag["calls"]:,}</td>'
            f'<td style="text-align:right;">{ag["input"]:,}</td>'
            f'<td style="text-align:right;">{ag["output"]:,}</td>'
            f'<td style="text-align:right; color:#38bdf8;">${ag["cost_usd"]:.4f}</td>'
            f'<td style="text-align:right; color:#4ade80;">₩{ag_cost_krw:,}</td>'
            f'</tr>'
        )
    if not agent_rows:
        agent_rows = '<tr><td colspan="6" style="text-align:center; color:#475569;">데이터 없음</td></tr>'

    agent_table = f"""
    <div style="margin-bottom:28px;">
      <h3 style="color:#94a3b8; font-size:0.9rem; font-weight:600; margin-bottom:12px; text-transform:uppercase; letter-spacing:0.05em;">
        Agent별 비용
      </h3>
      <div style="overflow-x:auto;">
        <table class="sys-table">
          <thead>
            <tr>
              <th>Agent</th><th>호출수</th><th>Input</th><th>Output</th>
              <th>비용 USD</th><th>비용 KRW</th>
            </tr>
          </thead>
          <tbody>{agent_rows}</tbody>
        </table>
      </div>
    </div>
    """

    # ── 최근 호출 로그 테이블 ──
    recent = stats.get("recent", [])
    recent_rows = ""
    for r in recent:
        recent_rows += (
            f'<tr>'
            f'<td style="color:#94a3b8;">{r["ts"]}</td>'
            f'<td>{r["agent"]}</td>'
            f'<td style="color:#64748b; font-size:0.75rem;">{r["model"]}</td>'
            f'<td style="text-align:right;">{r["input_tokens"]:,}</td>'
            f'<td style="text-align:right;">{r["output_tokens"]:,}</td>'
            f'<td style="text-align:right; color:#38bdf8;">${r["cost_usd"]:.5f}</td>'
            f'<td style="text-align:right; color:#4ade80;">₩{r["cost_krw"]:,.1f}</td>'
            f'</tr>'
        )
    if not recent_rows:
        recent_rows = '<tr><td colspan="7" style="text-align:center; color:#475569;">아직 기록 없음</td></tr>'

    recent_table = f"""
    <div>
      <h3 style="color:#94a3b8; font-size:0.9rem; font-weight:600; margin-bottom:12px; text-transform:uppercase; letter-spacing:0.05em;">
        최근 LLM 호출 로그 (최대 100건)
      </h3>
      <div style="overflow-x:auto; max-height:520px; overflow-y:auto;">
        <table class="sys-table">
          <thead style="position:sticky; top:0; background:#0f172a; z-index:1;">
            <tr>
              <th>시각</th><th>Agent</th><th>모델</th>
              <th>Input</th><th>Output</th><th>비용 USD</th><th>비용 KRW</th>
            </tr>
          </thead>
          <tbody>{recent_rows}</tbody>
        </table>
      </div>
    </div>
    """

    sys_css = """
    .sys-card {
        background:#1e293b; border:1px solid #334155; border-radius:10px;
        padding:14px 20px; min-width:130px; flex:1;
    }
    .sys-card-label { font-size:0.75rem; color:#64748b; margin-bottom:6px; }
    .sys-card-val { font-size:1.6rem; font-weight:700; color:#e2e8f0; }
    .sys-table {
        width:100%; border-collapse:collapse; font-size:0.82rem; color:#e2e8f0;
    }
    .sys-table th {
        background:#0f172a; color:#64748b; font-weight:600; padding:8px 12px;
        border-bottom:1px solid #334155; text-align:left; white-space:nowrap;
    }
    .sys-table td {
        padding:7px 12px; border-bottom:1px solid #1e293b; white-space:nowrap;
    }
    .sys-table tbody tr:hover { background:#1e293b; }
    """

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pochaco - 시스템</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  header {{ background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  header h1 {{ font-size: 1.4rem; color: #38bdf8; font-weight: 700; }}
  .profile-img {{ width: 2rem; height: 2rem; border-radius: 50%;
                  object-fit: cover; margin-right: 10px; vertical-align: middle;
                  border: 2px solid #334155; }}
  .health-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                 background: #4ade80; box-shadow: 0 0 6px #4ade80; margin-right: 6px;
                 animation: pulse 2s infinite; vertical-align: middle; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
  footer {{ text-align: center; padding: 12px; color: #334155; font-size: 0.75rem; }}
  .sys-card {{
      background:#1e293b; border:1px solid #334155; border-radius:10px;
      padding:14px 20px; min-width:130px; flex:1;
  }}
  .sys-card-label {{ font-size:0.75rem; color:#64748b; margin-bottom:6px; }}
  .sys-card-val {{ font-size:1.6rem; font-weight:700; color:#e2e8f0; }}
  .sys-table {{
      width:100%; border-collapse:collapse; font-size:0.82rem; color:#e2e8f0;
  }}
  .sys-table th {{
      background:#0f172a; color:#64748b; font-weight:600; padding:8px 12px;
      border-bottom:1px solid #334155; text-align:left; white-space:nowrap;
  }}
  .sys-table td {{
      padding:7px 12px; border-bottom:1px solid #1e293b; white-space:nowrap;
  }}
  .sys-table tbody tr:hover {{ background:#1e293b; }}
</style>
<script>
  setInterval(function() {{ location.reload(); }}, 30000);
</script>
</head>
<body>
<header>
  <div>
    <h1><img src="/profile.png" class="profile-img" alt="">Pochaco Monitor
      <span style="font-size:0.55em; color:#475569; font-weight:400; margin-left:6px;">{version}</span></h1>
    <div style="margin-top:6px; font-size:0.82rem; color:#64748b;">
      <span class="health-dot"></span>갱신: {now_str} &nbsp;|&nbsp;
      <span>30초마다 자동 새로고침</span>
    </div>
  </div>
  <nav style="display:flex; gap:8px; align-items:center;">
    <a href="/" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:transparent;">종합 대시보드</a>
    <a href="/experts" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:transparent;">전문가 실적표</a>
    <a href="/system" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:#334155;">시스템</a>
  </nav>
</header>

<div style="max-width:1100px; margin:0 auto; padding:24px 16px;">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
    <h2 style="font-size:1.1rem; color:#e2e8f0; margin:0;">⚙️ 시스템 — LLM 사용량</h2>
    <span style="font-size:0.75rem; color:#475569;">{now_str} 기준</span>
  </div>
  {summary_cards}
  {agent_table}
  {recent_table}
</div>

<footer>pochaco — AI 자동매매 시스템 &nbsp;|&nbsp; 데이터는 30초마다 갱신됩니다</footer>
</body>
</html>"""


_ROLE_DISPLAY = {
    "market_analyst": ("시장 분석가", "📊"),
    "asset_manager": ("자산 운용가", "💰"),
    "investment_strategist": ("투자 전문가", "🚀"),
    "buy_strategist": ("매수 전문가", "🎯"),
    "sell_strategist": ("매도 전문가", "📉"),
    "portfolio_evaluator": ("포트폴리오 평가가", "📋"),
    "coin_profile_analyst": ("특성 분석가", "🔍"),
}


def _build_experts_data(coordinator: "AgentCoordinator | None") -> dict:
    """전문가 실적표 JSON 데이터"""
    repo = TradeRepository()
    try:
        agents_data = []
        all_history = repo.get_all_agent_score_history(limit=28)
        latest_scores = repo.get_latest_agent_scores()
        latest_map = {s.agent_role: s for s in latest_scores}

        for role, (name, emoji) in _ROLE_DISPLAY.items():
            latest = latest_map.get(role)
            history = all_history.get(role, [])
            score_trend = [round(h.score, 1) for h in reversed(history[:16])]

            agent_info = {
                "role": role,
                "display_name": name,
                "emoji": emoji,
                "current_score": round(latest.score, 1) if latest else 50.0,
                "previous_score": round(latest.previous_score, 1) if latest and latest.previous_score else None,
                "score_trend": score_trend,
                "last_feedback": None,
            }
            if latest:
                agent_info["last_feedback"] = {
                    "strengths": latest.strengths or "",
                    "weaknesses": latest.weaknesses or "",
                    "directive": latest.directive or "",
                    "priority": latest.priority or "",
                    "evaluated_at": _to_kst(latest.created_at).strftime("%m-%d %H:%M"),
                }
            # 라이브 점수 (coordinator에서 메모리 기반)
            if coordinator:
                live = coordinator.get_agent_scores()
                agent_info["current_score"] = round(live.get(role, agent_info["current_score"]), 1)

            # 특성 분석가 전용: 관리 중인 코인 목록 추가
            if role == "coin_profile_analyst" and coordinator:
                agent_info["profiled_coins"] = coordinator.list_coin_profiles()

            agents_data.append(agent_info)

        # ── 총괄 평가가 보고서: 가장 최근 평가 일시 + 전 Agent 지시 요약 ──
        # latest_scores 의 created_at 중 가장 최신이 마지막 메타평가 시각
        meta_report: dict | None = None
        scored_entries = [s for s in latest_scores if s.directive]
        if scored_entries:
            latest_eval_time = max(s.created_at for s in scored_entries)
            _name_map = {r: n for r, (n, _) in _ROLE_DISPLAY.items()}
            rows = []
            for s in sorted(scored_entries, key=lambda x: x.created_at, reverse=False):
                rows.append({
                    "role": s.agent_role,
                    "display_name": _name_map.get(s.agent_role, s.agent_role),
                    "score": round(s.score, 1),
                    "priority": s.priority or "improve",
                    "directive": s.directive or "",
                    "strengths": s.strengths or "",
                    "weaknesses": s.weaknesses or "",
                })
            meta_report = {
                "evaluated_at": _to_kst(latest_eval_time).strftime("%Y-%m-%d %H:%M"),
                "rows": rows,
            }

        return {
            "updated_at": _kst_now().strftime("%m-%d %H:%M:%S"),
            "agents": agents_data,
            "meta_report": meta_report,
        }
    finally:
        repo.close()


def _render_experts_page(coordinator: "AgentCoordinator | None") -> str:
    """전문가 실적표 HTML 페이지 렌더링"""
    data = _build_experts_data(coordinator)
    version = _VERSION

    # 카드 HTML 빌드
    cards = ""
    for a in data["agents"]:
        role = a["role"]
        name = a["display_name"]
        sc = a["current_score"]
        if sc >= 70:
            sc_color = "#4ade80"
        elif sc >= 40:
            sc_color = "#facc15"
        else:
            sc_color = "#f87171"

        prev = a.get("previous_score")
        delta_str = ""
        if prev is not None:
            delta = sc - prev
            if delta > 0:
                delta_str = f'<span style="color:#4ade80; font-size:0.8rem;">+{delta:.1f}</span>'
            elif delta < 0:
                delta_str = f'<span style="color:#f87171; font-size:0.8rem;">{delta:.1f}</span>'

        # 트렌드 바 차트
        trend = a.get("score_trend", [])
        trend_html = ""
        if trend:
            max_s = max(trend) if trend else 100
            bars = ""
            for val in trend:
                h = max(2, int(val / max_s * 40))
                c = "#4ade80" if val >= 70 else "#facc15" if val >= 40 else "#f87171"
                bars += f'<div style="width:6px; height:{h}px; background:{c}; border-radius:2px;"></div>'
            trend_html = (
                f'<div style="display:flex; gap:2px; align-items:flex-end; '
                f'justify-content:center; height:45px; margin-top:8px;">{bars}</div>'
            )

        # 특성 분석가 전용: 관리 중인 코인 목록 (클릭 → 프로파일 팝업)
        profiled_html = ""
        if role == "coin_profile_analyst":
            coins = a.get("profiled_coins", [])
            if coins:
                tags = "".join(
                    f'<span onclick="showCoinProfile(\'{c}\')" '
                    f'style="background:#0f172a; border:1px solid #334155; '
                    f'border-radius:4px; padding:2px 7px; font-size:0.73rem; '
                    f'color:#38bdf8; margin:2px 2px 0 0; display:inline-block; '
                    f'cursor:pointer;" '
                    f'onmouseover="this.style.borderColor=\'#38bdf8\'" '
                    f'onmouseout="this.style.borderColor=\'#334155\'">'
                    f'{c}</span>'
                    for c in coins
                )
                profiled_html = (
                    f'<div style="margin-top:10px; padding-top:10px; border-top:1px solid #334155;">'
                    f'<div style="font-size:0.75rem; color:#64748b; margin-bottom:6px;">'
                    f'프로파일 관리 중 ({len(coins)}개) — 클릭하면 상세 내용 확인</div>'
                    f'{tags}</div>'
                )
            else:
                profiled_html = (
                    '<div style="margin-top:10px; padding-top:10px; border-top:1px solid #334155;'
                    ' font-size:0.75rem; color:#475569; font-style:italic;">'
                    '아직 관리 중인 코인 없음<br>(매매 완료 후 자동 기록)</div>'
                )

        # 피드백
        fb = a.get("last_feedback")
        fb_html = ""
        if fb and (fb["strengths"] or fb["weaknesses"]):
            priority_badge = ""
            p = fb.get("priority", "")
            if p == "critical":
                priority_badge = '<span style="background:#450a0a; color:#f87171; padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:600;">개선 시급</span>'
            elif p == "improve":
                priority_badge = '<span style="background:#422006; color:#facc15; padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:600;">개선 필요</span>'
            elif p == "reinforce":
                priority_badge = '<span style="background:#14532d; color:#4ade80; padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:600;">강화 유지</span>'
            fb_html = (
                f'<div style="margin-top:10px; padding-top:10px; border-top:1px solid #334155; font-size:0.78rem;">'
                f'{priority_badge} <span style="color:#475569; font-size:0.7rem;">{fb.get("evaluated_at", "")}</span>'
            )
            if fb["strengths"]:
                fb_html += f'<div style="margin-top:6px; color:#4ade80;">✓ {fb["strengths"]}</div>'
            if fb["weaknesses"]:
                fb_html += f'<div style="margin-top:4px; color:#f87171;">✗ {fb["weaknesses"]}</div>'
            if fb.get("directive"):
                fb_html += f'<div style="margin-top:4px; color:#94a3b8; font-style:italic;">→ {fb["directive"]}</div>'
            fb_html += "</div>"

        # 액션 버튼
        action_btns = (
            f'<div style="display:flex; gap:6px; margin-top:14px; padding-top:10px; border-top:1px solid #334155;">'
            f'<button onclick="showPromptModal(\'{role}\')" class="btn-action">📝 프롬프트</button>'
            f'<button onclick="showChatModal(\'{role}\', \'{name}\')" class="btn-action">💬 대화</button>'
            f'</div>'
        )

        cards += (
            f'<div style="background:#1e293b; border-radius:12px; padding:20px; '
            f'border:1px solid #334155; flex:1; min-width:280px;">'
            f'<div style="display:flex; justify-content:space-between; align-items:center;">'
            f'<span style="font-size:1rem;">{a["emoji"]} {name}</span>'
            f'{delta_str}'
            f'</div>'
            f'<div style="text-align:center; margin:12px 0;">'
            f'<div style="font-size:2.5rem; font-weight:700; color:{sc_color};">{sc:.0f}</div>'
            f'<div style="font-size:0.75rem; color:#475569;">/ 100</div>'
            f'</div>'
            f'{trend_html}'
            f'{profiled_html}'
            f'{fb_html}'
            f'{action_btns}'
            f'</div>'
        )

    updated_at = data["updated_at"]

    # CSS — 일반 문자열로 정의 (f-string 이중 중괄호 불필요)
    _extra_css = """
    .btn-action {
        background: #162032; color: #38bdf8; border: 1px solid #1d4ed8;
        padding: 5px 12px; border-radius: 6px; font-size: 0.78rem; cursor: pointer;
        transition: background 0.15s; font-weight: 500;
    }
    .btn-action:hover { background: #1d3a6a; }
    .modal-overlay {
        display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75);
        z-index: 1000; align-items: center; justify-content: center;
    }
    .modal-box {
        background: #1e293b; border-radius: 12px; border: 1px solid #334155;
        width: 90%; max-width: 760px; max-height: 90vh;
        display: flex; flex-direction: column; overflow: hidden;
    }
    .modal-header {
        padding: 16px 20px; border-bottom: 1px solid #334155;
        display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;
    }
    .modal-title { font-size: 1rem; font-weight: 600; color: #e2e8f0; }
    .modal-close {
        background: none; border: none; color: #64748b;
        font-size: 1.5rem; cursor: pointer; line-height: 1; padding: 2px 6px;
    }
    .modal-close:hover { color: #e2e8f0; }
    .modal-body { padding: 16px 20px; overflow-y: auto; flex: 1; min-height: 0; }
    .modal-footer {
        padding: 12px 20px; border-top: 1px solid #334155;
        display: flex; justify-content: flex-end; gap: 8px; flex-shrink: 0;
    }
    .mbtn { padding: 8px 18px; border-radius: 6px; border: none; font-size: 0.85rem; font-weight: 600; cursor: pointer; }
    .mbtn-primary { background: #38bdf8; color: #0f172a; }
    .mbtn-primary:hover { background: #7dd3fc; }
    .mbtn-secondary { background: #334155; color: #e2e8f0; }
    .mbtn-secondary:hover { background: #475569; }
    .prompt-textarea {
        width: 100%; min-height: 280px; background: #0f172a; color: #e2e8f0;
        border: 1px solid #334155; border-radius: 8px; padding: 12px;
        font-family: 'Courier New', monospace; font-size: 0.82rem; line-height: 1.5; resize: vertical;
    }
    .prompt-textarea:focus { outline: none; border-color: #38bdf8; }
    .sect-label { font-size: 0.8rem; color: #94a3b8; margin-bottom: 6px; margin-top: 14px; display: block; }
    .sect-label:first-child { margin-top: 0; }
    .feedback-readonly {
        background: #0f172a; border: 1px solid #1e293b; border-radius: 8px;
        padding: 12px; font-size: 0.82rem; color: #64748b; white-space: pre-wrap;
        max-height: 150px; overflow-y: auto; font-family: monospace;
    }
    .chat-messages {
        height: 380px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 4px 0;
    }
    .chat-msg { padding: 10px 14px; border-radius: 8px; font-size: 0.85rem; max-width: 88%; word-break: break-word; }
    .chat-msg.user { background: #1d4ed8; color: #fff; align-self: flex-end; border-radius: 8px 8px 2px 8px; }
    .chat-msg.assistant {
        background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
        align-self: flex-start; border-radius: 8px 8px 8px 2px; white-space: pre-wrap;
    }
    .chat-msg.system-notice { color: #64748b; font-size: 0.78rem; text-align: center; align-self: center; font-style: italic; }
    .chat-input-row { display: flex; gap: 8px; margin-top: 10px; align-items: flex-end; }
    .chat-textarea {
        flex: 1; background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
        border-radius: 8px; padding: 8px 12px; font-size: 0.85rem; resize: none;
        height: 42px; font-family: 'Segoe UI', sans-serif;
    }
    .chat-textarea:focus { outline: none; border-color: #38bdf8; }
    .btn-send {
        background: #38bdf8; color: #0f172a; border: none; border-radius: 8px;
        padding: 0 18px; font-weight: 700; cursor: pointer; height: 42px; font-size: 0.9rem;
    }
    .btn-send:hover { background: #7dd3fc; }
    .btn-send:disabled { opacity: 0.5; cursor: default; }
    """

    # JavaScript — 일반 문자열로 정의 (f-string 중괄호 충돌 없음)
    _js = """
    var _chatHist = [];
    var _chatRole = '';

    function showPromptModal(role) {
        var modal = document.getElementById('prompt-modal');
        modal.style.display = 'flex';
        document.getElementById('pm-title').textContent = '프롬프트 로딩 중...';
        document.getElementById('pm-base').value = '';
        document.getElementById('pm-feedback').textContent = '';
        document.getElementById('pm-role').value = role;
        fetch('/api/agent/prompt?role=' + encodeURIComponent(role))
            .then(function(r) { return r.json(); })
            .then(function(d) {
                document.getElementById('pm-title').textContent = d.role + ' — 프롬프트 설정';
                document.getElementById('pm-base').value = d.base_prompt || '';
                document.getElementById('pm-feedback').textContent = d.feedback_prompt || '(MetaEvaluator 피드백 없음)';
            })
            .catch(function(e) {
                document.getElementById('pm-title').textContent = '오류';
                document.getElementById('pm-base').value = '프롬프트 로드 실패: ' + e;
            });
    }

    function closePromptModal() {
        document.getElementById('prompt-modal').style.display = 'none';
    }

    function savePrompt() {
        var role = document.getElementById('pm-role').value;
        var newPrompt = document.getElementById('pm-base').value.trim();
        if (!newPrompt) { alert('프롬프트를 입력해주세요.'); return; }
        var btn = document.getElementById('pm-save-btn');
        btn.disabled = true; btn.textContent = '저장 중...';
        fetch('/api/agent/update_prompt', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({role: role, new_prompt: newPrompt})
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            btn.disabled = false; btn.textContent = '저장';
            if (d.success) {
                alert('프롬프트가 업데이트되었습니다.');
                closePromptModal();
            } else {
                alert('업데이트 실패: ' + (d.error || '알 수 없는 오류'));
            }
        })
        .catch(function(e) {
            btn.disabled = false; btn.textContent = '저장';
            alert('오류: ' + e);
        });
    }

    function showChatModal(role, displayName) {
        _chatRole = role;
        _chatHist = [];
        document.getElementById('cm-title').textContent = displayName + '와 대화';
        var msgs = document.getElementById('cm-messages');
        msgs.innerHTML = '<div class="chat-msg system-notice">안녕하세요! ' + displayName + '입니다. 무엇이든 질문해 주세요.</div>';
        document.getElementById('cm-input').value = '';
        document.getElementById('chat-modal').style.display = 'flex';
        setTimeout(function() { document.getElementById('cm-input').focus(); }, 100);
    }

    function closeChatModal() {
        document.getElementById('chat-modal').style.display = 'none';
    }

    function sendMessage() {
        var input = document.getElementById('cm-input');
        var msg = input.value.trim();
        if (!msg) return;
        input.value = '';
        var msgs = document.getElementById('cm-messages');
        var userDiv = document.createElement('div');
        userDiv.className = 'chat-msg user';
        userDiv.textContent = msg;
        msgs.appendChild(userDiv);
        var lid = 'ld' + Date.now();
        var waitDiv = document.createElement('div');
        waitDiv.className = 'chat-msg assistant';
        waitDiv.id = lid;
        waitDiv.textContent = '...';
        msgs.appendChild(waitDiv);
        msgs.scrollTop = msgs.scrollHeight;
        var sendBtn = document.getElementById('cm-send');
        sendBtn.disabled = true;
        fetch('/api/agent/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({role: _chatRole, message: msg, history: _chatHist})
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            sendBtn.disabled = false;
            var resp = d.response || '응답 없음';
            _chatHist.push({role: 'user', content: msg});
            _chatHist.push({role: 'assistant', content: resp});
            var el = document.getElementById(lid);
            if (el) el.textContent = resp;
            msgs.scrollTop = msgs.scrollHeight;
        })
        .catch(function(e) {
            sendBtn.disabled = false;
            var el = document.getElementById(lid);
            if (el) el.textContent = '오류: ' + e;
        });
    }

    document.addEventListener('DOMContentLoaded', function() {
        var cinput = document.getElementById('cm-input');
        if (cinput) {
            cinput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                }
            });
        }
        var pm = document.getElementById('prompt-modal');
        if (pm) pm.addEventListener('click', function(e) { if (e.target === this) closePromptModal(); });
        var cm = document.getElementById('chat-modal');
        if (cm) cm.addEventListener('click', function(e) { if (e.target === this) closeChatModal(); });

        /* 30초 자동 새로고침 — 모달 열려있을 때는 스킵 */
        setInterval(function() {
            var ids = ['prompt-modal', 'chat-modal', 'coin-profile-modal'];
            for (var i=0; i<ids.length; i++) {
                var m = document.getElementById(ids[i]);
                if (m && (m.style.display === 'flex' || m.style.display === 'block')) {
                    return;
                }
            }
            location.reload();
        }, 30000);
    });
    """

    # 모달 HTML — 일반 문자열
    _modals = """
<div id="prompt-modal" class="modal-overlay">
  <div class="modal-box">
    <div class="modal-header">
      <span id="pm-title" class="modal-title">프롬프트 설정</span>
      <button class="modal-close" onclick="closePromptModal()">&#215;</button>
    </div>
    <div class="modal-body">
      <input type="hidden" id="pm-role">
      <span class="sect-label">기본 역할 프롬프트 (수정 가능)</span>
      <textarea id="pm-base" class="prompt-textarea" placeholder="로딩 중..."></textarea>
      <span class="sect-label">MetaEvaluator 피드백 (읽기 전용)</span>
      <div id="pm-feedback" class="feedback-readonly"></div>
    </div>
    <div class="modal-footer">
      <button class="mbtn mbtn-secondary" onclick="closePromptModal()">닫기</button>
      <button id="pm-save-btn" class="mbtn mbtn-primary" onclick="savePrompt()">저장</button>
    </div>
  </div>
</div>

<div id="chat-modal" class="modal-overlay">
  <div class="modal-box">
    <div class="modal-header">
      <span id="cm-title" class="modal-title">대화</span>
      <button class="modal-close" onclick="closeChatModal()">&#215;</button>
    </div>
    <div class="modal-body">
      <div id="cm-messages" class="chat-messages"></div>
      <div class="chat-input-row">
        <textarea id="cm-input" class="chat-textarea"
          placeholder="메시지 입력... (Enter: 전송, Shift+Enter: 줄바꿈)" rows="1"></textarea>
        <button id="cm-send" class="btn-send" onclick="sendMessage()">전송</button>
      </div>
    </div>
  </div>
</div>

<div id="coin-profile-modal" class="modal-overlay" onclick="if(event.target===this)closeCoinProfile()">
  <div class="modal-box" style="max-width:640px;">
    <div class="modal-header">
      <span id="cpm-title" class="modal-title">코인 프로파일</span>
      <button class="modal-close" onclick="closeCoinProfile()">&#215;</button>
    </div>
    <div id="cpm-body" class="modal-body"
         style="font-family:'Courier New',monospace; font-size:0.82rem;
                line-height:1.6; white-space:pre-wrap; word-break:break-word;">
      로딩 중...
    </div>
  </div>
</div>

<script>
function showCoinProfile(symbol) {
  document.getElementById('cpm-title').textContent = symbol + ' 특성 프로파일';
  document.getElementById('cpm-body').textContent = '로딩 중...';
  document.getElementById('coin-profile-modal').style.display = 'flex';
  fetch('/api/coin_profile?symbol=' + encodeURIComponent(symbol))
    .then(r => r.json())
    .then(d => {
      if (d.content) {
        document.getElementById('cpm-body').textContent = d.content;
      } else {
        document.getElementById('cpm-body').textContent = '프로파일 없음: ' + (d.error || '');
      }
    })
    .catch(e => { document.getElementById('cpm-body').textContent = '조회 실패: ' + e; });
}
function closeCoinProfile() {
  document.getElementById('coin-profile-modal').style.display = 'none';
}
</script>
"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pochaco - 전문가 실적표</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; }}
  header {{ background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
            display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  header h1 {{ font-size: 1.4rem; color: #38bdf8; font-weight: 700; }}
  .profile-img {{ width: 2rem; height: 2rem; border-radius: 50%;
                  object-fit: cover; margin-right: 10px; vertical-align: middle;
                  border: 2px solid #334155; }}
  .health-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                 background: #4ade80; box-shadow: 0 0 6px #4ade80; margin-right: 6px;
                 animation: pulse 2s infinite; vertical-align: middle; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
  footer {{ text-align: center; padding: 12px; color: #334155; font-size: 0.75rem; }}
{_extra_css}
</style>
<script>
{_js}
</script>
</head>
<body>
<header>
  <div>
    <h1><img src="/profile.png" class="profile-img" alt="">Pochaco Monitor
      <span style="font-size:0.55em; color:#475569; font-weight:400; margin-left:6px;">{version}</span></h1>
    <div style="margin-top:6px; font-size:0.82rem; color:#64748b;">
      <span class="health-dot"></span>갱신: {updated_at} &nbsp;|&nbsp;
      <span id="refresh-hint">30초마다 자동 새로고침</span>
    </div>
  </div>
  <nav style="display:flex; gap:8px; align-items:center;">
    <a href="/" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:transparent;">종합 대시보드</a>
    <a href="/experts" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:#334155;">전문가 실적표</a>
    <a href="/system" style="color:#38bdf8; text-decoration:none; font-size:0.82rem; padding:4px 12px;
       border-radius:6px; background:transparent;">시스템</a>
  </nav>
</header>

<div style="padding:20px;">
  <h2 style="color:#e2e8f0; margin-bottom:16px; font-size:1.1rem;">전문가 Agent 실적표</h2>
  <p style="color:#64748b; font-size:0.82rem; margin-bottom:20px;">
    6시간 주기(0·6·12·18시) 총괄 평가가가 각 전문가를 평가합니다.
    잘하는 부분은 강화, 못하는 부분은 강한 피드백을 부여합니다.
  </p>
  <div style="display:flex; gap:16px; flex-wrap:wrap;">
    {cards}
  </div>
</div>

{_modals}

<footer>pochaco — AI 자동매매 시스템 &nbsp;|&nbsp; 데이터는 30초마다 갱신됩니다</footer>
</body>
</html>"""


def _liquidate_position(client: "BithumbClient") -> dict:
    """현재 활성 포트폴리오를 일괄 청산 (8개 코인 전량 매도)"""
    from database.models import Portfolio
    repo = TradeRepository()
    try:
        pf = repo.get_open_portfolio()
        if not pf:
            return {"success": False, "error": "활성 포트폴리오 없음"}

        positions = repo.get_portfolio_positions(pf.id)
        if not positions:
            repo.close_portfolio(pf.id)
            return {"success": False, "error": "포트폴리오 내 포지션 없음 — 종료 처리"}

        total_krw_received = 0.0
        total_buy_krw = 0.0
        sold_coins = []

        for pos in positions:
            try:
                units = client.get_coin_balance(pos.symbol)
                if units <= 0:
                    repo.close_position(pos.id)
                    continue

                current_price = client.get_current_price(pos.symbol)
                krw_value = units * current_price

                sell_result = client.market_sell(pos.symbol, units)
                if sell_result.get("status") != "0000":
                    logger.warning(f"[포트폴리오 청산] {pos.symbol} 매도 실패: {sell_result}")
                    repo.close_position(pos.id)
                    continue

                coin_pnl_pct = (current_price - pos.buy_price) / pos.buy_price * 100 if pos.buy_price > 0 else 0
                repo.save_trade(
                    symbol=pos.symbol, side="sell",
                    price=current_price, units=units,
                    krw_amount=krw_value,
                    note=f"대시보드 포트폴리오 청산 ({coin_pnl_pct:+.2f}%)",
                    portfolio_id=pf.id,
                )
                repo.close_position(pos.id)
                cooldown_registry.record_sell(pos.symbol, "manual")

                total_krw_received += krw_value
                total_buy_krw += pos.buy_krw
                sold_coins.append({
                    "symbol": pos.symbol,
                    "buy_krw": round(pos.buy_krw, 0),
                    "sell_krw": round(krw_value, 0),
                    "pnl_pct": round(coin_pnl_pct, 2),
                })
            except Exception as e:
                logger.error(f"[포트폴리오 청산] {pos.symbol} 오류: {e}")
                repo.close_position(pos.id)

        repo.close_portfolio(pf.id)

        pnl_pct = (total_krw_received - total_buy_krw) / total_buy_krw * 100 if total_buy_krw > 0 else 0
        held_min = (
            (datetime.utcnow() - pf.opened_at.replace(tzinfo=None)).total_seconds() / 60
            if pf.opened_at else 0
        )

        # 수동 청산 평가 기록 — 포트폴리오 거래 내역에 표시되기 위해 저장
        try:
            repo.save_evaluation(
                portfolio_id=pf.id,
                portfolio_name=pf.name,
                total_buy_krw=total_buy_krw,
                total_sell_krw=total_krw_received,
                pnl_pct=round(pnl_pct, 2),
                held_minutes=round(held_min, 1),
                exit_type="manual",
                original_tp_pct=pf.take_profit_pct or 0.0,
                original_sl_pct=pf.stop_loss_pct or 0.0,
                evaluation="대시보드 수동 청산",
                suggested_tp_pct=pf.take_profit_pct or 0.0,
                suggested_sl_pct=pf.stop_loss_pct or 0.0,
                coins_summary=json.dumps(sold_coins, ensure_ascii=False),
                lesson="",
            )
        except Exception as e:
            logger.warning(f"[포트폴리오 청산] 평가 기록 저장 실패 (무시): {e}")

        krw = client.get_krw_balance()
        logger.info(
            f"[대시보드 포트폴리오 청산] '{pf.name}' "
            f"{len(sold_coins)}개 코인 → {total_krw_received:,.0f}원 ({pnl_pct:+.2f}%)"
        )
        return {
            "success": True,
            "portfolio_name": pf.name,
            "coins_sold": len(sold_coins),
            "coin_details": sold_coins,
            "pnl_pct": round(pnl_pct, 2),
            "krw_received": round(total_krw_received, 0),
            "krw_balance": round(krw, 0),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        repo.close()


class _Handler(BaseHTTPRequestHandler):
    client: "BithumbClient"
    coordinator: "AgentCoordinator | None" = None

    def log_message(self, fmt, *args):
        pass  # 액세스 로그 억제

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                data = _build_json_status(self.client, self.coordinator)
                body = _render_html(data).encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "text/plain", f"Error: {e}".encode())

        elif self.path == "/system":
            try:
                body = _render_system_page().encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "text/plain", f"Error: {e}".encode())

        elif self.path == "/api/system":
            try:
                from core.llm_provider import usage_tracker
                data = usage_tracker.get_stats()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"error": str(e)}).encode())

        elif self.path == "/experts":
            try:
                body = _render_experts_page(self.coordinator).encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "text/plain", f"Error: {e}".encode())

        elif self.path == "/api/status":
            try:
                data = _build_json_status(self.client, self.coordinator)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"error": str(e)}).encode())

        elif self.path == "/api/experts":
            try:
                data = _build_experts_data(self.coordinator)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"error": str(e)}).encode())

        elif self.path.startswith("/api/agent/prompt"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            role = params.get("role", [None])[0]
            if not role or not self.coordinator:
                body = json.dumps({"error": "role 파라미터 또는 coordinator 없음"}).encode()
                self._respond(400, "application/json; charset=utf-8", body)
            else:
                data = self.coordinator.get_agent_prompt(role)
                if data is None:
                    body = json.dumps({"error": f"에이전트 '{role}' 없음"}).encode()
                    self._respond(404, "application/json; charset=utf-8", body)
                else:
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    self._respond(200, "application/json; charset=utf-8", body)

        elif self.path.startswith("/api/coin_profile"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [None])[0]
            if not symbol:
                body = json.dumps({"error": "symbol 파라미터 없음"}).encode()
                self._respond(400, "application/json; charset=utf-8", body)
            else:
                profile_text = None
                if self.coordinator and hasattr(self.coordinator, "_coin_analyst") \
                        and self.coordinator._coin_analyst:
                    profile_text = self.coordinator._coin_analyst.get_profile(symbol.upper())
                if profile_text:
                    body = json.dumps(
                        {"symbol": symbol.upper(), "content": profile_text},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self._respond(200, "application/json; charset=utf-8", body)
                else:
                    body = json.dumps({"error": f"{symbol} 프로파일 없음"}).encode()
                    self._respond(404, "application/json; charset=utf-8", body)

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

        elif self.path == "/api/agent/chat":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length)) if length > 0 else {}
                role = req.get("role", "")
                message = req.get("message", "")
                history = req.get("history", [])
                if not self.coordinator or not role or not message:
                    body = json.dumps({"error": "coordinator/role/message 필수"}).encode()
                    self._respond(400, "application/json; charset=utf-8", body)
                else:
                    response = self.coordinator.chat_with_agent(role, message, history)
                    body = json.dumps({"response": response}, ensure_ascii=False).encode("utf-8")
                    self._respond(200, "application/json; charset=utf-8", body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self._respond(500, "application/json; charset=utf-8", body)

        elif self.path == "/api/agent/update_prompt":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length)) if length > 0 else {}
                role = req.get("role", "")
                new_prompt = req.get("new_prompt", "")
                if not self.coordinator or not role or not new_prompt:
                    body = json.dumps({"error": "coordinator/role/new_prompt 필수"}).encode()
                    self._respond(400, "application/json; charset=utf-8", body)
                else:
                    success = self.coordinator.update_agent_prompt(role, new_prompt)
                    body = json.dumps({"success": success}, ensure_ascii=False).encode("utf-8")
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

    def __init__(
        self,
        client: "BithumbClient",
        host: str,
        port: int,
        coordinator: "AgentCoordinator | None" = None,
    ):
        self._client = client
        self._host = host
        self._port = port
        self._coordinator = coordinator
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {
            "client": self._client,
            "coordinator": self._coordinator,
        })
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
