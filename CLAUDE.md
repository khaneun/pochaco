# pochaco — Claude Code 가이드

빗썸 기반 AI 자동매매 시스템. Python 3.10+, 단일 진입점(`main.py`), 의존성 주입 구조.

---

## 프로젝트 구조

```
main.py                  # 진입점 — 6개 Agent 조립 및 스레드 조율
config/settings.py       # Pydantic-settings 싱글톤 (AWS Secrets Manager 연동)
core/
  bithumb_client.py      # 빗썸 REST/WebSocket API (JWT HS256 v2 인증)
  llm_provider.py        # LLM 추상화 (Anthropic/OpenAI/Gemini 교체 가능)
  telegram_bot.py        # 텔레그램 알림 + 명령어 봇
  websocket_client.py    # 실시간 시세 WebSocket
strategy/
  agents/                # 6개 전문가 Agent
    base_agent.py        # BaseSpecialistAgent 추상 기반 (LLM 호출, 피드백 관리)
    market_analyst.py    # 시장 분석가 (시장 흐름·리스크·투자 적합성 판단)
    asset_manager.py     # 자산 운용가 (투자 비율 동적 결정 30~95%)
    buy_strategist.py    # 매수 전문가 (코인 선정·TP/SL 결정)
    sell_strategist.py   # 매도 전문가 (보유 중 TP/SL 동적 조정)
    portfolio_evaluator.py # 포트폴리오 평가가 (매매 성과 분석·파라미터 제안)
    meta_evaluator.py    # 총괄 평가가 (6시간 주기 전문가 평가·점수화·피드백)
  agent_coordinator.py   # AgentCoordinator — 6개 Agent 오케스트레이션
  ai_agent.py            # 데이터 클래스 (AgentDecision, TradeEvaluation) + 레거시 TradingAgent
  trading_engine.py      # 핵심 매매 루프 + 스마트 매도 상태 머신
  strategy_optimizer.py  # 매매 직후 다음 TP/SL 최적화
  coin_selector.py       # 변동성·모멘텀·거래량 수학적 사전 필터링
  market_analyzer.py     # 시장 데이터 수집/가공
  position_manager.py    # 포지션 감시 및 익절/손절 실행
  cooldown.py            # 재매수 쿨다운 (thread-safe 레지스트리)
database/
  models.py              # SQLAlchemy ORM (Trade, Position, DailyReport, StrategyEvaluation, AgentScore, AgentDecisionLog)
  repository.py          # Thread-safe CRUD (전문가 점수·의사결정 로그 포함)
  backup.py              # SQLite 자동 백업
scheduler/jobs.py        # APScheduler (리포트, 백업, 6시간 총괄 평가)
dashboard/
  terminal_ui.py         # Rich 터미널 실시간 대시보드
  web_server.py          # HTTP 웹 대시보드 (종합 + 전문가 실적표 2페이지)
deploy/                  # EC2 배포 스크립트 + systemd 유닛 파일
```

---

## 코딩 스타일

### 명명 규칙
| 범주 | 스타일 | 예시 |
|------|--------|------|
| 함수/변수/메서드 | `snake_case` | `get_krw_balance()`, `daily_start_krw` |
| 클래스/예외/Enum | `PascalCase` | `TradingEngine`, `AgentDecision`, `_ExitPhase` |
| 모듈 레벨 상수 | `_UPPER_SNAKE` (private) | `_MIN_VOLUME_KRW`, `_ADJUST_INTERVAL_SEC` |
| Private 멤버 | `_` 접두사 | `_jwt_header()`, `_price_fail_count` |

### 주석/문서
- **한국어** 사용 (주석, docstring, 로그 메시지 모두)
- 섹션 구분: `# ────────────────────────────────────────── (80자)`
- 소섹션: `# ── 소제목`
- Docstring: Args/Returns 명시

```python
def example(param: str) -> str | None:
    """짧은 요약.

    상세 설명:
      - 항목 1
      - 항목 2

    Args:
        param: 설명

    Returns:
        반환값 설명
    """
```

### 타입 힌팅
Python 3.10+ 문법 사용:
```python
def foo(x: str | None = None) -> tuple[list[str], dict]:
    ...
```

### 에러 핸들링
```python
# 부분 실패 허용 — 폴백값 설정
try:
    data = client.get_data()
except Exception:
    data = []  # 폴백, 매매 로직은 계속

# 연쇄 에러 방지 — 알림 실패가 매매 중단 유발 금지
try:
    notifier.send(msg)
except Exception as e:
    logger.warning(f"알림 발송 실패: {e}")
```

### 로깅
```python
logger = logging.getLogger(__name__)

logger.info("=== 모듈명 시작 ===")
logger.debug("[기능명] 상세값: {변수}")
logger.warning("[경고 상황] 이유")
logger.error(f"[오류 설명] {e}", exc_info=True)
```

메시지 형식: `[모듈/기능] 설명: 값`

---

## 핵심 패턴

### 1. 6개 전문가 Agent 시스템
```python
# strategy/agents/ — BaseSpecialistAgent를 상속하는 6개 전문가
# strategy/agent_coordinator.py — 오케스트레이터
# 매매 파이프라인: MarketAnalyst → AssetManager → BuyStrategist → (보유) → SellStrategist → PortfolioEvaluator
# 6시간 주기: MetaEvaluator가 5개 전문가 평가 → feedback_prompt 동적 주입

coordinator.select_coin(snapshots, eval_stats, coin_scores)   # 시장분석→배분→코인선정
coordinator.should_adjust_strategy(...)                        # 매도 전문가 조정
coordinator.evaluate_trade(...)                                # 포트폴리오 평가
coordinator.run_meta_evaluation()                              # 총괄 평가 (스케줄러)
```

### 2. 의존성 주입 (main.py에서 조립)
모든 컴포넌트는 `main.py`에서 생성 후 생성자로 주입. 6개 Agent → AgentCoordinator → TradingEngine.

### 2. Thread-safe DB 세션
```python
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

# 사용
with self._session() as db:
    db.add(record)
```

### 3. 데이터 클래스
전달용 값 객체는 `@dataclass`:
```python
@dataclass
class AgentDecision:
    symbol: str
    take_profit_pct: float
    stop_loss_1st_pct: float
    stop_loss_pct: float
    confidence: float
    reason: str
    llm_provider: str = ""
```

### 4. 추상화 레이어 (LLM 교체)
```python
class BaseLLMProvider(ABC):
    @abstractmethod
    def chat(self, prompt: str, max_tokens: int = 1024) -> str: ...
    
    @property
    @abstractmethod
    def provider_name(self) -> str: ...
```
`.env`의 `LLM_PROVIDER=anthropic|openai|gemini`로 전환.

### 5. 상태 머신 (매도 로직)
```python
class _ExitPhase(Enum):
    MONITORING = "monitoring"    # 일반 감시 (손절 포함)
    TRAILING_TP = "trailing_tp"  # 익절 진입 후 트레일링
```

### 6. 쿨다운 레지스트리
```python
# strategy/cooldown.py — 모듈 레벨 싱글톤
_cooldowns: dict[str, float] = {}
_lock = threading.Lock()

register_cooldown(symbol, minutes)  # 재매수 금지
is_on_cooldown(symbol) -> bool
```

---

## 매매 전략 핵심 수치 (trading_engine.py)

| 파라미터 | 범위 |
|---------|------|
| 1차 손절 | -0.5% ~ -1.5% (50% 매도) |
| 2차 손절 | -0.8% ~ -2.5% (전량 매도) |
| 익절 진입 | 5%+ 도달 시 트레일링 시작 |
| 트레일링 타임아웃 | 30분 |
| AI 동적 조정 간격 | 30분 |
| TP 범위 (optimizer) | 4.0% ~ 10.0% |

---

## 설정 (config/settings.py)

```python
from config.settings import settings

settings.BITHUMB_API_KEY
settings.LLM_PROVIDER        # "anthropic" | "openai" | "gemini"
settings.HEADLESS            # True = EC2 서비스 모드 (터미널 UI 없음)
settings.POSITION_CHECK_INTERVAL  # 포지션 점검 주기(초)
settings.DASHBOARD_PORT      # 기본 8080
```

`.env` 파일 또는 AWS Secrets Manager(`AWS_SECRET_NAME` 환경변수)로 설정 주입.

---

## 커밋 메시지 규칙

```
feat: 기능 설명 (v버전)
fix: 버그 설명
docs: 문서 업데이트
style: 코드 스타일 변경 (로직 없음)
refactor: 리팩토링
```

- 한국어 사용
- 한 줄, 80자 이내
- 버전 변경 시 끝에 `(v2.4.0)` 표기

---

## 실행

```bash
python main.py                  # 기본 (터미널 UI)
HEADLESS=true python main.py    # 서비스 모드
```

---

## 작업 시 주의사항

- **테스트 없음** — 수동 검증 필요, 새 테스트 추가 시 `tests/` 디렉토리 생성
- **동시성** — DB 세션은 반드시 `_session()` context manager 사용; 공유 상태에는 `threading.Lock()` 적용
- **부분 실패 허용** — 코인 1개 조회 실패가 전체 사이클을 중단시키면 안 됨
- **알림 이중 장애 방지** — 텔레그램 발송 실패가 매매 로직 예외로 전파되지 않도록 분리
- **파라미터 범위 준수** — TP/SL 수정 시 optimizer의 clamp 범위(tp: 4~10%, sl: -0.8~-2.5%) 내에서 변경
- **KST 시간** — UI/로그 출력은 KST, DB 저장은 UTC
