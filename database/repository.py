"""DB CRUD 레이어 (thread-safe) — v4.0 포트폴리오 기반"""
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, Generator

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (
    SessionLocal, Portfolio, Trade, Position, DailyReport,
    StrategyEvaluation, AgentScore, AgentDecisionLog,
)


class TradeRepository:
    """거래 데이터 저장소 — 요청별 세션으로 멀티스레드 안전"""

    @contextmanager
    def _session(self) -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self):
        pass  # 하위호환 유지 (세션은 요청별 자동 관리)

    # ------------------------------------------------------------------ #
    #  Portfolio                                                           #
    # ------------------------------------------------------------------ #
    def open_portfolio(
        self,
        name: str,
        total_buy_krw: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        agent_reason: str = "",
        llm_provider: str = "",
    ) -> Portfolio:
        """새 포트폴리오 생성 (기존 활성 포트폴리오가 있으면 강제 종료)"""
        with self._session() as db:
            db.query(Portfolio).filter(Portfolio.is_open == True).update(
                {"is_open": False, "closed_at": datetime.utcnow()}
            )
            pf = Portfolio(
                name=name, total_buy_krw=total_buy_krw,
                take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                agent_reason=agent_reason, llm_provider=llm_provider,
            )
            db.add(pf)
            db.flush()
            db.refresh(pf)
            db.expunge(pf)
            return pf

    def get_open_portfolio(self) -> Optional[Portfolio]:
        with self._session() as db:
            pf = db.query(Portfolio).filter(Portfolio.is_open == True).first()
            if pf:
                db.expunge(pf)
            return pf

    def close_portfolio(self, portfolio_id: int) -> None:
        with self._session() as db:
            pf = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
            if pf:
                pf.is_open = False
                pf.closed_at = datetime.utcnow()
            # 하위 포지션도 모두 종료
            db.query(Position).filter(
                Position.portfolio_id == portfolio_id,
                Position.is_open == True,
            ).update({"is_open": False, "closed_at": datetime.utcnow()})

    def get_portfolio_history(self, limit: int = 20) -> list[Portfolio]:
        with self._session() as db:
            rows = (
                db.query(Portfolio)
                .order_by(Portfolio.opened_at.desc())
                .limit(limit).all()
            )
            db.expunge_all()
            return rows

    def get_closed_portfolios(self, limit: int = 100) -> list[Portfolio]:
        with self._session() as db:
            rows = (
                db.query(Portfolio)
                .filter(Portfolio.is_open == False, Portfolio.closed_at.isnot(None))
                .order_by(Portfolio.opened_at.desc())
                .limit(limit).all()
            )
            db.expunge_all()
            return rows

    def update_portfolio_targets(
        self, portfolio_id: int, new_tp: float, new_sl: float,
    ) -> None:
        """포트폴리오 TP/SL 동적 조정"""
        with self._session() as db:
            pf = db.query(Portfolio).filter(Portfolio.id == portfolio_id).first()
            if pf:
                pf.take_profit_pct = new_tp
                pf.stop_loss_pct = new_sl

    # ------------------------------------------------------------------ #
    #  Position (포트폴리오 하위)                                            #
    # ------------------------------------------------------------------ #
    def open_position(
        self,
        portfolio_id: int,
        symbol: str,
        units: float,
        buy_price: float,
        buy_krw: float,
        agent_reason: str = "",
    ) -> Position:
        with self._session() as db:
            pos = Position(
                portfolio_id=portfolio_id,
                symbol=symbol, units=units,
                buy_price=buy_price, buy_krw=buy_krw,
                agent_reason=agent_reason,
            )
            db.add(pos)
            db.flush()
            db.refresh(pos)
            db.expunge(pos)
            return pos

    def get_portfolio_positions(self, portfolio_id: int) -> list[Position]:
        """포트폴리오 내 활성 포지션 목록"""
        with self._session() as db:
            rows = (
                db.query(Position)
                .filter(
                    Position.portfolio_id == portfolio_id,
                    Position.is_open == True,
                )
                .all()
            )
            db.expunge_all()
            return rows

    def get_all_portfolio_positions(self, portfolio_id: int) -> list[Position]:
        """포트폴리오 내 전체 포지션 (종료 포함)"""
        with self._session() as db:
            rows = (
                db.query(Position)
                .filter(Position.portfolio_id == portfolio_id)
                .all()
            )
            db.expunge_all()
            return rows

    def update_position_after_partial_sell(
        self, position_id: int, remaining_units: float, remaining_buy_krw: float,
    ) -> None:
        """분할 매도 후 잔여 수량·투입금액 업데이트 (P&L 기준 보정)"""
        with self._session() as db:
            pos = db.query(Position).filter(Position.id == position_id).first()
            if pos:
                pos.units = remaining_units
                pos.buy_krw = remaining_buy_krw

    def close_position(self, position_id: int) -> None:
        with self._session() as db:
            pos = db.query(Position).filter(Position.id == position_id).first()
            if pos:
                pos.is_open = False
                pos.closed_at = datetime.utcnow()

    def close_all_positions(self, portfolio_id: int | None = None) -> None:
        """포트폴리오 ID 기준 또는 전체 포지션 종료"""
        with self._session() as db:
            q = db.query(Position).filter(Position.is_open == True)
            if portfolio_id is not None:
                q = q.filter(Position.portfolio_id == portfolio_id)
            q.update({"is_open": False, "closed_at": datetime.utcnow()})

    # ------------------------------------------------------------------ #
    #  Trade                                                               #
    # ------------------------------------------------------------------ #
    def save_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        units: float,
        krw_amount: float,
        fee: float = 0.0,
        order_id: str = "",
        note: str = "",
        portfolio_id: int | None = None,
    ) -> Trade:
        with self._session() as db:
            trade = Trade(
                portfolio_id=portfolio_id,
                symbol=symbol, side=side, price=price, units=units,
                krw_amount=krw_amount, fee=fee, order_id=order_id, note=note,
            )
            db.add(trade)
            db.flush()
            db.refresh(trade)
            db.expunge(trade)
            return trade

    def get_recent_trades(self, limit: int = 20) -> list[Trade]:
        with self._session() as db:
            rows = db.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
            db.expunge_all()
            return rows

    def get_all_trades(self, limit: int = 200) -> list[Trade]:
        with self._session() as db:
            rows = db.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
            db.expunge_all()
            return rows

    def get_coin_sell_total(self, portfolio_id: int, symbol: str) -> float:
        """특정 코인의 포트폴리오 내 전체 매도 금액 합산.

        분할 매도가 완료된 코인의 실제 수익을 계산할 때 사용.

        Args:
            portfolio_id: 포트폴리오 ID
            symbol: 코인 심볼

        Returns:
            해당 코인의 sell side 거래 krw_amount 합계
        """
        with self._session() as db:
            trades = (
                db.query(Trade)
                .filter(
                    Trade.portfolio_id == portfolio_id,
                    Trade.symbol == symbol,
                    Trade.side == "sell",
                )
                .all()
            )
            return sum(t.krw_amount for t in trades)

    def get_portfolio_sell_total(self, portfolio_id: int) -> float:
        """포트폴리오의 전체 매도 금액 합산 (분할 매도 포함).

        Args:
            portfolio_id: 포트폴리오 ID

        Returns:
            해당 포트폴리오의 sell side 거래 krw_amount 합계
        """
        with self._session() as db:
            trades = (
                db.query(Trade)
                .filter(Trade.portfolio_id == portfolio_id, Trade.side == "sell")
                .all()
            )
            return sum(t.krw_amount for t in trades)

    # ------------------------------------------------------------------ #
    #  DailyReport                                                         #
    # ------------------------------------------------------------------ #
    def upsert_daily_report(
        self,
        date_str: str,
        starting_krw: float,
        ending_krw: float,
        trade_count: int,
        win_count: int,
        total_fee: float = 0.0,
    ) -> DailyReport:
        with self._session() as db:
            report = db.query(DailyReport).filter(DailyReport.date == date_str).first()
            pnl_krw = ending_krw - starting_krw
            pnl_pct = (pnl_krw / starting_krw * 100) if starting_krw > 0 else 0.0

            if report:
                report.ending_krw = ending_krw
                report.pnl_krw = pnl_krw
                report.pnl_pct = pnl_pct
                report.trade_count = trade_count
                report.win_count = win_count
                report.total_fee = total_fee
                if report.starting_krw == 0.0 and starting_krw > 0:
                    report.starting_krw = starting_krw
            else:
                report = DailyReport(
                    date=date_str, starting_krw=starting_krw,
                    ending_krw=ending_krw, pnl_krw=pnl_krw, pnl_pct=pnl_pct,
                    total_fee=total_fee,
                    trade_count=trade_count, win_count=win_count,
                )
                db.add(report)
            db.flush()
            db.refresh(report)
            db.expunge(report)
            return report

    def get_recent_reports(self, limit: int = 7) -> list[DailyReport]:
        with self._session() as db:
            rows = db.query(DailyReport).order_by(DailyReport.date.desc()).limit(limit).all()
            db.expunge_all()
            return rows

    def get_all_daily_reports(self) -> list[DailyReport]:
        with self._session() as db:
            rows = db.query(DailyReport).order_by(DailyReport.date.asc()).all()
            db.expunge_all()
            return rows

    def get_daily_activity_summary(self, days: int = 7) -> list[dict]:
        """일별 AI 행동 요약 (포트폴리오 기반)"""
        with self._session() as db:
            since = datetime.utcnow() - timedelta(days=days)

            portfolios = (
                db.query(Portfolio).filter(Portfolio.opened_at >= since)
                .order_by(Portfolio.opened_at.asc()).all()
            )
            sell_trades = (
                db.query(Trade)
                .filter(Trade.side == "sell", Trade.created_at >= since).all()
            )
            reports = {r.date: r for r in db.query(DailyReport).all()}

            by_date: dict[str, dict] = {}
            for pf in portfolios:
                d_key = pf.opened_at.strftime("%Y-%m-%d")
                if d_key not in by_date:
                    by_date[d_key] = {
                        "date": d_key, "portfolio_names": [], "total": 0,
                        "wins": 0, "losses": 0, "llm": "",
                        "pnl_pct": 0.0, "pnl_krw": 0.0,
                        "starting_krw": 0.0, "total_fee": 0.0,
                    }
                entry = by_date[d_key]
                entry["total"] += 1
                entry["portfolio_names"].append(pf.name)
                if pf.llm_provider:
                    entry["llm"] = pf.llm_provider

            for t in sell_trades:
                d_key = t.created_at.strftime("%Y-%m-%d")
                if d_key in by_date:
                    if "익절" in (t.note or ""):
                        by_date[d_key]["wins"] += 1
                    elif "손절" in (t.note or ""):
                        by_date[d_key]["losses"] += 1

            for d_key, entry in by_date.items():
                r = reports.get(d_key)
                if r:
                    entry["pnl_pct"] = r.pnl_pct
                    entry["pnl_krw"] = r.pnl_krw
                    entry["starting_krw"] = r.starting_krw
                    entry["total_fee"] = getattr(r, "total_fee", 0.0) or 0.0

            return sorted(by_date.values(), key=lambda x: x["date"], reverse=True)

    def get_total_stats(self) -> dict:
        """전체 누적 통계 (포트폴리오 기반)"""
        with self._session() as db:
            all_trades = db.query(Trade).all()
            sell_trades = [t for t in all_trades if t.side == "sell"]

            win_count = sum(1 for t in sell_trades if "익절" in (t.note or ""))
            loss_count = sum(1 for t in sell_trades if "손절" in (t.note or ""))

            # 포트폴리오 사이클 수 기준
            closed_pf = (
                db.query(Portfolio)
                .filter(Portfolio.is_open == False, Portfolio.closed_at.isnot(None))
                .all()
            )
            total_cycles = len(closed_pf)

            hold_minutes = [
                (pf.closed_at - pf.opened_at).total_seconds() / 60
                for pf in closed_pf if pf.opened_at and pf.closed_at
            ]
            avg_hold = sum(hold_minutes) / len(hold_minutes) if hold_minutes else 0.0

            reports = db.query(DailyReport).order_by(DailyReport.date.asc()).all()
            total_pnl = sum(r.pnl_krw for r in reports)
            initial_krw = reports[0].starting_krw if reports else 0.0

            return {
                "total_trades": len(all_trades),
                "total_cycles": total_cycles,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": win_count / total_cycles if total_cycles > 0 else 0.0,
                "avg_hold_minutes": avg_hold,
                "total_pnl_krw": total_pnl,
                "initial_krw": initial_krw,
            }

    # ------------------------------------------------------------------ #
    #  StrategyEvaluation                                                  #
    # ------------------------------------------------------------------ #
    def save_evaluation(
        self,
        portfolio_id: int,
        portfolio_name: str,
        total_buy_krw: float,
        total_sell_krw: float,
        pnl_pct: float,
        held_minutes: float,
        exit_type: str,
        original_tp_pct: float,
        original_sl_pct: float,
        evaluation: str,
        suggested_tp_pct: float,
        suggested_sl_pct: float,
        coins_summary: str = "",
        lesson: str = "",
        adjusted_tp_pct: float | None = None,
        adjusted_sl_pct: float | None = None,
        adjustment_reason: str = "",
    ) -> StrategyEvaluation:
        with self._session() as db:
            ev = StrategyEvaluation(
                portfolio_id=portfolio_id,
                portfolio_name=portfolio_name,
                total_buy_krw=total_buy_krw,
                total_sell_krw=total_sell_krw,
                pnl_pct=pnl_pct, held_minutes=held_minutes,
                exit_type=exit_type,
                original_tp_pct=original_tp_pct,
                original_sl_pct=original_sl_pct,
                evaluation=evaluation,
                suggested_tp_pct=suggested_tp_pct,
                suggested_sl_pct=suggested_sl_pct,
                coins_summary=coins_summary,
                lesson=lesson,
                adjusted_tp_pct=adjusted_tp_pct,
                adjusted_sl_pct=adjusted_sl_pct,
                adjustment_reason=adjustment_reason,
            )
            db.add(ev)
            db.flush()
            db.refresh(ev)
            db.expunge(ev)
            return ev

    def get_recent_evaluations(self, limit: int = 10) -> list[StrategyEvaluation]:
        with self._session() as db:
            rows = (
                db.query(StrategyEvaluation)
                .order_by(StrategyEvaluation.created_at.desc())
                .limit(limit).all()
            )
            db.expunge_all()
            return rows

    def get_evaluation_stats(self, last_n: int = 10) -> dict:
        """최근 N건 평가 기반 전략 통계 — Agent 프롬프트에 주입용"""
        with self._session() as db:
            evals = (
                db.query(StrategyEvaluation)
                .order_by(StrategyEvaluation.created_at.desc())
                .limit(last_n).all()
            )
            if not evals:
                return {}

            wins = [e for e in evals if e.exit_type == "take_profit"]
            losses = [e for e in evals if e.exit_type == "stop_loss"]
            avg_pnl = sum(e.pnl_pct for e in evals) / len(evals)
            avg_hold = sum(e.held_minutes for e in evals) / len(evals)
            avg_tp_set = sum(e.original_tp_pct for e in evals) / len(evals)
            avg_sl_set = sum(e.original_sl_pct for e in evals) / len(evals)

            avg_suggested_tp = sum(e.suggested_tp_pct for e in evals) / len(evals)
            avg_suggested_sl = sum(e.suggested_sl_pct for e in evals) / len(evals)

            # ── 추세 방향 ──
            recent_5 = list(reversed(evals[:5]))
            tp_trend = [e.suggested_tp_pct for e in recent_5]

            tp_direction = ""
            if len(tp_trend) >= 4:
                first_half = sum(tp_trend[:len(tp_trend)//2]) / (len(tp_trend)//2)
                second_half = sum(tp_trend[len(tp_trend)//2:]) / (len(tp_trend) - len(tp_trend)//2)
                diff = second_half - first_half
                if diff < -0.3:
                    tp_direction = "하향"
                elif diff > 0.3:
                    tp_direction = "상향"
                else:
                    tp_direction = "유지"

            # ── 최근 포트폴리오 결과 요약 ──
            recent_trades_summary = [
                {
                    "portfolio_name": e.portfolio_name,
                    "pnl_pct": e.pnl_pct,
                    "exit_type": e.exit_type,
                    "held_minutes": e.held_minutes,
                }
                for e in evals[:5]
            ]

            # ── suggested 기반 적응형 clamp 범위 ──
            if len(evals) >= 3:
                weights = [2.0 if i < 3 else 1.0 for i in range(len(evals))]
                w_sum = sum(weights)
                w_tp = sum(e.suggested_tp_pct * w for e, w in zip(evals, weights)) / w_sum

                tp_clamp_min = max(4.0, round(w_tp - 1.0, 1))
                tp_clamp_max = min(10.0, round(w_tp + 1.5, 1))
                sl_clamp_min = -2.0
                sl_clamp_max = -1.0
            else:
                tp_clamp_min, tp_clamp_max = 4.0, 8.0
                sl_clamp_min, sl_clamp_max = -2.0, -1.0

            return {
                "count": len(evals),
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": len(wins) / len(evals) if evals else 0,
                "avg_pnl_pct": round(avg_pnl, 2),
                "avg_hold_minutes": round(avg_hold, 1),
                "avg_tp_set": round(avg_tp_set, 2),
                "avg_sl_set": round(avg_sl_set, 2),
                "avg_suggested_tp": round(avg_suggested_tp, 2),
                "avg_suggested_sl": round(avg_suggested_sl, 2),
                "tp_trend": tp_trend,
                "tp_direction": tp_direction,
                "recent_lessons": [e.lesson for e in evals[:3] if e.lesson],
                "recent_trades": recent_trades_summary,
                "tp_clamp_min": tp_clamp_min,
                "tp_clamp_max": tp_clamp_max,
                "sl_clamp_min": sl_clamp_min,
                "sl_clamp_max": sl_clamp_max,
            }

    # ------------------------------------------------------------------ #
    #  AgentScore                                                          #
    # ------------------------------------------------------------------ #
    def save_agent_scores(self, scores: list[dict]) -> None:
        """총괄 평가 결과 일괄 저장"""
        with self._session() as db:
            for s in scores:
                record = AgentScore(
                    agent_role=s["agent_role"],
                    score=s["score"],
                    previous_score=s.get("previous_score"),
                    strengths=s.get("strengths", ""),
                    weaknesses=s.get("weaknesses", ""),
                    directive=s.get("directive", ""),
                    priority=s.get("priority", ""),
                    eval_period=s["eval_period"],
                )
                db.add(record)

    def get_latest_agent_scores(self) -> list[AgentScore]:
        """각 agent_role별 최신 점수 1건씩 반환"""
        with self._session() as db:
            subq = (
                db.query(
                    AgentScore.agent_role,
                    func.max(AgentScore.created_at).label("max_created"),
                )
                .group_by(AgentScore.agent_role)
                .subquery()
            )
            rows = (
                db.query(AgentScore)
                .join(
                    subq,
                    (AgentScore.agent_role == subq.c.agent_role)
                    & (AgentScore.created_at == subq.c.max_created),
                )
                .all()
            )
            db.expunge_all()
            return rows

    def get_agent_score_history(self, agent_role: str, limit: int = 28) -> list[AgentScore]:
        with self._session() as db:
            rows = (
                db.query(AgentScore)
                .filter(AgentScore.agent_role == agent_role)
                .order_by(AgentScore.created_at.desc())
                .limit(limit)
                .all()
            )
            db.expunge_all()
            return rows

    def get_all_agent_score_history(self, limit: int = 28) -> dict[str, list[AgentScore]]:
        with self._session() as db:
            roles = [r[0] for r in db.query(AgentScore.agent_role).distinct().all()]
            result: dict[str, list[AgentScore]] = {}
            for role in roles:
                rows = (
                    db.query(AgentScore)
                    .filter(AgentScore.agent_role == role)
                    .order_by(AgentScore.created_at.desc())
                    .limit(limit)
                    .all()
                )
                result[role] = rows
            db.expunge_all()
            return result

    # ------------------------------------------------------------------ #
    #  AgentDecisionLog                                                    #
    # ------------------------------------------------------------------ #
    def save_decision_log(
        self,
        agent_role: str,
        decision_type: str,
        input_summary: str,
        output_summary: str,
        portfolio_id: int | None = None,
    ) -> None:
        with self._session() as db:
            log = AgentDecisionLog(
                agent_role=agent_role,
                decision_type=decision_type,
                input_summary=input_summary,
                output_summary=output_summary,
                portfolio_id=portfolio_id,
            )
            db.add(log)

    def get_recent_decision_logs(self, hours: int = 6) -> list[AgentDecisionLog]:
        with self._session() as db:
            since = datetime.utcnow() - timedelta(hours=hours)
            rows = (
                db.query(AgentDecisionLog)
                .filter(AgentDecisionLog.created_at >= since)
                .order_by(AgentDecisionLog.created_at.desc())
                .all()
            )
            db.expunge_all()
            return rows
