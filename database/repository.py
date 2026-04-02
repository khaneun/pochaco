"""DB CRUD 레이어 (thread-safe)"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, Generator

from sqlalchemy.orm import Session

from .models import SessionLocal, Trade, Position, DailyReport


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
    ) -> Trade:
        with self._session() as db:
            trade = Trade(
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

    # ------------------------------------------------------------------ #
    #  Position                                                            #
    # ------------------------------------------------------------------ #
    def open_position(
        self,
        symbol: str,
        units: float,
        buy_price: float,
        buy_krw: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        agent_reason: str = "",
        llm_provider: str = "",
    ) -> Position:
        with self._session() as db:
            # 기존 오픈 포지션 강제 종료
            db.query(Position).filter(Position.is_open == True).update(
                {"is_open": False, "closed_at": datetime.utcnow()}
            )
            pos = Position(
                symbol=symbol, units=units, buy_price=buy_price,
                buy_krw=buy_krw, take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct, agent_reason=agent_reason,
                llm_provider=llm_provider,
            )
            db.add(pos)
            db.flush()
            db.refresh(pos)
            db.expunge(pos)
            return pos

    def get_open_position(self) -> Optional[Position]:
        with self._session() as db:
            pos = db.query(Position).filter(Position.is_open == True).first()
            if pos:
                db.expunge(pos)
            return pos

    def close_position(self, position_id: int) -> None:
        with self._session() as db:
            pos = db.query(Position).filter(Position.id == position_id).first()
            if pos:
                pos.is_open = False
                pos.closed_at = datetime.utcnow()

    def close_all_positions(self) -> None:
        with self._session() as db:
            db.query(Position).filter(Position.is_open == True).update(
                {"is_open": False, "closed_at": datetime.utcnow()}
            )

    def get_position_history(self, limit: int = 20) -> list[Position]:
        with self._session() as db:
            rows = db.query(Position).order_by(Position.opened_at.desc()).limit(limit).all()
            db.expunge_all()
            return rows

    def get_closed_positions(self, limit: int = 100) -> list[Position]:
        with self._session() as db:
            rows = (
                db.query(Position)
                .filter(Position.is_open == False, Position.closed_at.isnot(None))
                .order_by(Position.opened_at.desc())
                .limit(limit)
                .all()
            )
            db.expunge_all()
            return rows

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
            else:
                report = DailyReport(
                    date=date_str, starting_krw=starting_krw,
                    ending_krw=ending_krw, pnl_krw=pnl_krw, pnl_pct=pnl_pct,
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
        """일별 AI 행동 요약"""
        with self._session() as db:
            since = datetime.utcnow() - timedelta(days=days)

            positions = (
                db.query(Position).filter(Position.opened_at >= since)
                .order_by(Position.opened_at.asc()).all()
            )
            sell_trades = (
                db.query(Trade)
                .filter(Trade.side == "sell", Trade.created_at >= since).all()
            )
            reports = {r.date: r for r in db.query(DailyReport).all()}

            by_date: dict[str, dict] = {}
            for pos in positions:
                d_key = pos.opened_at.strftime("%Y-%m-%d")
                if d_key not in by_date:
                    by_date[d_key] = {
                        "date": d_key, "symbols": [], "total": 0,
                        "wins": 0, "losses": 0, "llm": "", "pnl_pct": 0.0,
                    }
                entry = by_date[d_key]
                entry["total"] += 1
                if pos.symbol not in entry["symbols"]:
                    entry["symbols"].append(pos.symbol)
                if pos.llm_provider:
                    entry["llm"] = pos.llm_provider

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

            return sorted(by_date.values(), key=lambda x: x["date"], reverse=True)

    def get_total_stats(self) -> dict:
        """전체 누적 통계"""
        with self._session() as db:
            all_trades = db.query(Trade).all()
            sell_trades = [t for t in all_trades if t.side == "sell"]

            win_count = sum(1 for t in sell_trades if "익절" in (t.note or ""))
            loss_count = sum(1 for t in sell_trades if "손절" in (t.note or ""))
            total_cycles = win_count + loss_count
            win_rate = win_count / total_cycles if total_cycles > 0 else 0.0

            closed = (
                db.query(Position)
                .filter(Position.is_open == False, Position.closed_at.isnot(None))
                .all()
            )
            hold_minutes = [
                (p.closed_at - p.opened_at).total_seconds() / 60
                for p in closed if p.opened_at and p.closed_at
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
                "win_rate": win_rate,
                "avg_hold_minutes": avg_hold,
                "total_pnl_krw": total_pnl,
                "initial_krw": initial_krw,
            }
