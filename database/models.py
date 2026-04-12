"""SQLAlchemy ORM 모델 및 DB 엔진 설정 (v4.0 — 포트폴리오 기반)

포트폴리오: 8개 코인을 동시 매수/매도하는 단위.
포지션: 포트폴리오 내 개별 코인 보유 기록.

EC2 환경 안전성:
- SQLite: WAL 모드 활성화 (크래시 복구 + 동시 읽기 성능 향상)
- PostgreSQL: 커넥션 풀링, SSL 지원
- DATABASE_URL 환경변수로 외부 DB 전환 가능
"""
import os
import shutil
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, event, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from config import settings


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ #
#  ORM 모델                                                            #
# ------------------------------------------------------------------ #
class Portfolio(Base):
    """포트폴리오 (8개 코인 묶음, 동시 매수/매도 단위)"""
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)             # 랜덤 이름 (예: "판다-07")
    total_buy_krw = Column(Float, nullable=False)         # 총 투입 KRW
    take_profit_pct = Column(Float, nullable=False)       # 포트폴리오 익절%
    stop_loss_pct = Column(Float, nullable=False)         # 포트폴리오 최대 손절% (max -2%)
    agent_reason = Column(Text)                           # 포트폴리오 구성 이유
    llm_provider = Column(String(50), default="")
    opened_at = Column(DateTime, default=datetime.utcnow, index=True)
    closed_at = Column(DateTime, nullable=True)
    is_open = Column(Boolean, default=True, index=True)


class Position(Base):
    """포트폴리오 내 개별 코인 포지션"""
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, nullable=False, index=True)  # FK → portfolios.id
    symbol = Column(String(20), nullable=False, index=True)
    units = Column(Float, nullable=False)
    buy_price = Column(Float, nullable=False)
    buy_krw = Column(Float, nullable=False)               # 개별 투입금 (전체의 12.5%)
    agent_reason = Column(Text)                            # 개별 코인 선정 이유
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    is_open = Column(Boolean, default=True, index=True)


class Trade(Base):
    """개별 거래 내역"""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, nullable=True, index=True)  # 소속 포트폴리오
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)        # "buy" | "sell"
    price = Column(Float, nullable=False)
    units = Column(Float, nullable=False)
    krw_amount = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    order_id = Column(String(50), index=True)
    target_price = Column(Float, nullable=True)   # 목표 체결가 (지정가 매도 기준가)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    note = Column(Text)


class DailyReport(Base):
    """일별 성과 리포트"""
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), unique=True, nullable=False)
    starting_krw = Column(Float, default=0.0)
    ending_krw = Column(Float, default=0.0)
    pnl_krw = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    total_fee = Column(Float, default=0.0)
    trade_count = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    llm_provider = Column(String(50), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class StrategyEvaluation(Base):
    """포트폴리오 매매 후 성과 평가 및 전략 조정 기록"""
    __tablename__ = "strategy_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, nullable=False, index=True)
    portfolio_name = Column(String(50), nullable=False)

    # 포트폴리오 매매 결과
    total_buy_krw = Column(Float, nullable=False)
    total_sell_krw = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    held_minutes = Column(Float, nullable=False)
    exit_type = Column(String(10), nullable=False)      # "take_profit" | "stop_loss" | "timeout"
    coins_summary = Column(Text, default="")            # JSON — 8개 코인별 상세 결과

    # 원래 설정
    original_tp_pct = Column(Float, nullable=False)
    original_sl_pct = Column(Float, nullable=False)

    # AI 평가 결과
    evaluation = Column(Text, nullable=False)
    suggested_tp_pct = Column(Float, nullable=False)
    suggested_sl_pct = Column(Float, nullable=False)
    lesson = Column(Text, default="")

    # 동적 조정 기록
    adjusted_tp_pct = Column(Float, nullable=True)
    adjusted_sl_pct = Column(Float, nullable=True)
    adjustment_reason = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AgentScore(Base):
    """전문가별 점수 기록 (6시간 주기 총괄 평가)"""
    __tablename__ = "agent_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_role = Column(String(30), nullable=False, index=True)
    score = Column(Float, nullable=False)
    previous_score = Column(Float, nullable=True)
    strengths = Column(Text, default="")
    weaknesses = Column(Text, default="")
    directive = Column(Text, default="")
    priority = Column(String(20), default="")
    eval_period = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AgentDecisionLog(Base):
    """전문가별 의사결정 기록 (MetaEvaluator 입력용)"""
    __tablename__ = "agent_decision_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_role = Column(String(30), nullable=False, index=True)
    decision_type = Column(String(30), nullable=False)
    input_summary = Column(Text, default="")
    output_summary = Column(Text, default="")
    portfolio_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ------------------------------------------------------------------ #
#  엔진 팩토리                                                          #
# ------------------------------------------------------------------ #
def _make_engine() -> Engine:
    db_url = settings.DATABASE_URL

    if db_url:
        # ── PostgreSQL / MySQL 등 외부 DB ───────────────────────────
        engine = create_engine(
            db_url,
            poolclass=QueuePool,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=settings.DB_ECHO,
        )
    else:
        # ── SQLite ──────────────────────────────────────────────────
        db_path = settings.DB_PATH
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
                "timeout": 30,
            },
            poolclass=NullPool,
            echo=settings.DB_ECHO,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def _backup_and_reset_db() -> None:
    """v4.0 포트폴리오 전환: 기존 DB를 백업하고 클린 스타트

    portfolios 테이블이 없으면 구 스키마로 간주하여 백업 후 삭제합니다.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    db_url = settings.DATABASE_URL
    if db_url:
        return  # 외부 DB는 수동 마이그레이션 필요

    db_path = settings.DB_PATH
    if not os.path.exists(db_path):
        return  # 신규 설치

    # 기존 DB에 portfolios 테이블이 있는지 확인
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolios'"
        )
        has_portfolio_table = cursor.fetchone() is not None
        conn.close()
    except Exception:
        has_portfolio_table = False

    if has_portfolio_table:
        return  # 이미 v4.0 스키마

    # 구 스키마 → 백업 후 삭제
    backup_path = db_path + ".v1_backup"
    if not os.path.exists(backup_path):
        shutil.copy2(db_path, backup_path)
        _log.info(f"[DB 마이그레이션] 기존 DB → {backup_path} 백업 완료")
    os.remove(db_path)
    _log.info("[DB 마이그레이션] 구 스키마 삭제 → 포트폴리오 스키마로 클린 스타트")


_backup_and_reset_db()
engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


def _ensure_schema_updates() -> None:
    """create_all이 기존 테이블에 추가하지 못하는 컬럼을 ALTER로 보완"""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    db_url = settings.DATABASE_URL
    if db_url:
        return  # 외부 DB는 수동 마이그레이션 필요

    db_path = settings.DB_PATH
    if not os.path.exists(db_path):
        return

    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        if "target_price" not in columns:
            conn.execute("ALTER TABLE trades ADD COLUMN target_price FLOAT")
            conn.commit()
            _log.info("[DB 스키마] trades.target_price 컬럼 추가 완료")
    finally:
        conn.close()


_ensure_schema_updates()
