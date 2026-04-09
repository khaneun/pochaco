"""SQLAlchemy ORM 모델 및 DB 엔진 설정

EC2 환경 안전성:
- SQLite: WAL 모드 활성화 (크래시 복구 + 동시 읽기 성능 향상)
- PostgreSQL: 커넥션 풀링, SSL 지원
- DATABASE_URL 환경변수로 외부 DB 전환 가능
"""
import os
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
class Trade(Base):
    """개별 거래 내역"""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)        # "buy" | "sell"
    price = Column(Float, nullable=False)
    units = Column(Float, nullable=False)
    krw_amount = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    order_id = Column(String(50), index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    note = Column(Text)


class Position(Base):
    """오픈 포지션 (최대 1개)"""
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    units = Column(Float, nullable=False)
    buy_price = Column(Float, nullable=False)
    buy_krw = Column(Float, nullable=False)
    take_profit_pct = Column(Float, nullable=False)
    stop_loss_1st_pct = Column(Float, nullable=True)   # 1차 손절 (50% 매도)
    stop_loss_pct = Column(Float, nullable=False)       # 2차 손절 (전량 매도)
    agent_reason = Column(Text)
    llm_provider = Column(String(50), default="")   # 사용된 LLM 기록
    opened_at = Column(DateTime, default=datetime.utcnow, index=True)
    closed_at = Column(DateTime, nullable=True)
    is_open = Column(Boolean, default=True, index=True)


class DailyReport(Base):
    """일별 성과 리포트"""
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), unique=True, nullable=False)
    starting_krw = Column(Float, default=0.0)   # 하루 시작 총자산 (KRW + 코인평가액)
    ending_krw = Column(Float, default=0.0)      # 하루 종료 총자산 (KRW + 코인평가액)
    pnl_krw = Column(Float, default=0.0)         # 당일 손익 (ending - starting)
    pnl_pct = Column(Float, default=0.0)
    total_fee = Column(Float, default=0.0)       # 당일 수수료 합계 (추정)
    trade_count = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    llm_provider = Column(String(50), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class StrategyEvaluation(Base):
    """매매 후 성과 평가 및 전략 조정 기록"""
    __tablename__ = "strategy_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(20), nullable=False)

    # 매매 결과
    buy_price = Column(Float, nullable=False)
    sell_price = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    held_minutes = Column(Float, nullable=False)
    exit_type = Column(String(10), nullable=False)      # "take_profit" | "stop_loss" | "timeout"

    # 원래 설정
    original_tp_pct = Column(Float, nullable=False)
    original_sl_1st_pct = Column(Float, nullable=True)  # 1차 손절%
    original_sl_pct = Column(Float, nullable=False)      # 2차 손절%

    # AI 평가 결과
    evaluation = Column(Text, nullable=False)            # AI 평가 텍스트
    suggested_tp_pct = Column(Float, nullable=False)     # 제안된 다음 익절%
    suggested_sl_pct = Column(Float, nullable=False)     # 제안된 다음 손절%
    lesson = Column(Text, default="")                    # 핵심 교훈 요약

    # 동적 조정 기록 (보유 중 조정이 있었다면)
    adjusted_tp_pct = Column(Float, nullable=True)       # 조정된 익절% (없으면 NULL)
    adjusted_sl_pct = Column(Float, nullable=True)       # 조정된 손절% (없으면 NULL)
    adjustment_reason = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AgentScore(Base):
    """전문가별 점수 기록 (6시간 주기 총괄 평가)"""
    __tablename__ = "agent_scores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_role = Column(String(30), nullable=False, index=True)
    # market_analyst | asset_manager | buy_strategist | sell_strategist | portfolio_evaluator
    score = Column(Float, nullable=False)           # 0~100
    previous_score = Column(Float, nullable=True)   # 직전 점수 (트렌드)
    strengths = Column(Text, default="")
    weaknesses = Column(Text, default="")
    directive = Column(Text, default="")            # 개선 지시
    priority = Column(String(20), default="")       # reinforce | improve | critical
    eval_period = Column(String(20), nullable=False) # "2026-04-09_06" (날짜_시)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AgentDecisionLog(Base):
    """전문가별 의사결정 기록 (MetaEvaluator 입력용)"""
    __tablename__ = "agent_decision_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_role = Column(String(30), nullable=False, index=True)
    decision_type = Column(String(30), nullable=False)  # market_analysis | allocation | coin_select | exit_adjust | evaluate
    input_summary = Column(Text, default="")       # 입력 요약
    output_summary = Column(Text, default="")      # 출력 요약
    position_id = Column(Integer, nullable=True)   # 관련 포지션
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
            pool_pre_ping=True,     # 연결 유효성 사전 확인 (EC2 재시작 후 끊김 방지)
            pool_recycle=3600,      # 1시간마다 커넥션 재생성
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
                "timeout": 30,          # 락 대기 타임아웃(초)
            },
            poolclass=NullPool,         # SQLite는 멀티스레드 풀 불필요
            echo=settings.DB_ECHO,
        )

        # WAL 모드: 크래시 복구 + 동시 읽기 허용
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


def _migrate_schema() -> None:
    """기존 DB에 누락된 컬럼 자동 추가 (SQLite ALTER TABLE)"""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    migrations = [
        ("positions", "stop_loss_1st_pct", "REAL"),
        ("strategy_evaluations", "original_sl_1st_pct", "REAL"),
        ("daily_reports", "total_fee", "REAL DEFAULT 0"),
    ]
    with engine.connect() as conn:
        for table, col, coltype in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                conn.commit()
                _log.info(f"[DB Migration] {table}.{col} 컬럼 추가 완료")
            except Exception:
                pass  # 이미 존재하는 컬럼은 무시


_migrate_schema()
