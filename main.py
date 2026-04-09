"""pochaco - 빗썸 AI 자동매매 프로그램 진입점

매매 사이클:
  기동 → 전체 현금화 → AI 코인 선정 → 전액 매수
  → 익절/손절 감시 → 매도 → AI 코인 선정 → ... (무한 반복)
"""
import logging
import os
import signal
import sys
import threading
import time

from config import settings
from core import BithumbClient
from core.llm_provider import get_llm_provider
from core.telegram_bot import TelegramBot
from database import TradeRepository
from strategy import MarketAnalyzer, TradingEngine, StrategyOptimizer, CoinSelector, AgentCoordinator
from strategy.agents import (
    MarketAnalyst, AssetManager, BuyStrategist,
    SellStrategist, PortfolioEvaluator, MetaEvaluator,
)
from scheduler import TradingScheduler
from dashboard import Dashboard
from dashboard.web_server import WebDashboard

# ------------------------------------------------------------------ #
#  로깅 설정                                                            #
# ------------------------------------------------------------------ #
os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(settings.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def check_config() -> None:
    """필수 설정값 확인"""
    missing = []
    if not settings.BITHUMB_API_KEY:
        missing.append("BITHUMB_API_KEY")
    if not settings.BITHUMB_SECRET_KEY:
        missing.append("BITHUMB_SECRET_KEY")

    provider = settings.LLM_PROVIDER
    if provider == "anthropic" and not settings.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    elif provider == "openai" and not settings.OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    elif provider == "gemini" and not settings.GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

    if missing:
        logger.error(f".env 파일에 다음 키가 없습니다: {', '.join(missing)}")
        sys.exit(1)


def main() -> None:
    check_config()

    # 인프라 의존성
    client    = BithumbClient()
    repo      = TradeRepository()
    analyzer  = MarketAnalyzer(client)
    optimizer = StrategyOptimizer()
    selector  = CoinSelector()

    # LLM 공급자 (공유)
    llm = get_llm_provider()

    # 6개 전문가 Agent 생성
    market_analyst      = MarketAnalyst(llm=llm)
    asset_manager       = AssetManager(llm=llm)
    buy_strategist      = BuyStrategist(llm=llm)
    sell_strategist     = SellStrategist(llm=llm)
    portfolio_evaluator = PortfolioEvaluator(llm=llm)
    meta_evaluator      = MetaEvaluator(llm=llm)

    # 코디네이터 (기존 TradingAgent 대체)
    coordinator = AgentCoordinator(
        market_analyst=market_analyst,
        asset_manager=asset_manager,
        buy_strategist=buy_strategist,
        sell_strategist=sell_strategist,
        portfolio_evaluator=portfolio_evaluator,
        meta_evaluator=meta_evaluator,
        repo=repo,
    )

    # DB에서 최신 피드백 로드 → Agent에 주입 (재시작 시 피드백 유지)
    coordinator.restore_feedbacks_from_db()

    # 매매 엔진 (coordinator로 교체)
    engine = TradingEngine(client, repo, coordinator, analyzer, optimizer, selector)

    # 텔레그램 봇 초기화 (TELEGRAM_ENABLED=true 시)
    telegram: TelegramBot | None = None
    if settings.TELEGRAM_ENABLED and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        telegram = TelegramBot(
            token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            client=client,
            repo=repo,
            engine=engine,
        )
        engine.set_notifier(telegram)
    else:
        logger.info("텔레그램 봇 비활성화 (.env에서 TELEGRAM_ENABLED=true 설정)")

    scheduler = TradingScheduler(
        client=client,
        repo=repo,
        get_daily_start_krw=lambda: engine.daily_start_krw,
        notifier=telegram,
        coordinator=coordinator,
    )
    dashboard = Dashboard(client, repo)

    # 웹 대시보드 초기화 (DASHBOARD_ENABLED=true 시)
    web: WebDashboard | None = None
    if settings.DASHBOARD_ENABLED:
        web = WebDashboard(
            client, settings.DASHBOARD_HOST, settings.DASHBOARD_PORT,
            coordinator=coordinator,
        )

    # Graceful shutdown
    def handle_signal(sig, frame):
        logger.info("종료 신호 수신, 정리 중...")
        engine.stop()
        scheduler.stop()
        dashboard.stop()
        if telegram:
            telegram.stop()
        repo.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 웹 대시보드 시작 (백그라운드)
    if web:
        web.start()
        logger.info(f"웹 대시보드: http://{settings.DASHBOARD_HOST}:{settings.DASHBOARD_PORT}")

    # 텔레그램 봇 시작 (백그라운드)
    if telegram:
        telegram.start()
        telegram.notify_start()

    # 스케줄러 시작 (백그라운드: 백업·리포트·총괄평가)
    scheduler.start()

    # 매매 엔진 시작 (백그라운드 스레드)
    engine_thread = threading.Thread(
        target=engine.run,
        daemon=True,
        name="trading-engine",
    )
    engine_thread.start()

    logger.info(f"pochaco 시작 (HEADLESS={settings.HEADLESS})")
    logger.info(f"LLM 공급자: {settings.LLM_PROVIDER} / 감시 주기: {settings.POSITION_CHECK_INTERVAL}초")
    logger.info("6개 전문가 Agent 시스템 활성화")

    if settings.HEADLESS:
        logger.info("헤드리스 모드 — 웹 대시보드 및 텔레그램으로 모니터링하세요.")
        try:
            while True:
                time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            pass
    else:
        dashboard.run()

    # 종료 정리
    engine.stop()
    scheduler.stop()
    if telegram:
        telegram.stop()
    repo.close()
    logger.info("pochaco 정상 종료")


if __name__ == "__main__":
    main()
