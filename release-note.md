# Release Notes

---

## v2.0.0 (2026-04-04)

### 전략 Agent 3분화 + 스마트 매도 도입

기존 `TradingAgent` 하나가 모든 결정을 담당 → 3개 전담 Agent로 분리:
- **CoinSelector**: 종목 사전 필터링 (변동성·모멘텀·거래량)
- **StrategyOptimizer**: 익절/손절 파라미터 즉각 최적화
- **TradingAgent**: AI 최종 코인 선정 (검증된 후보풀에서만 선택)

#### `strategy/coin_selector.py` (신규) — 종목 선정 전담 Agent
- **핵심 문제 해결**: 24h 등락폭 ±1% 코인에 2~3% 익절 기대 → 변동폭 < TP×1.5인 코인 자동 제외
- **5단계 필터링**:
  1. 변동폭 필터: 목표 익절의 1.5배 이상 변동폭 필수
  2. 거래대금 필터: 50억원/24h 미만 제외
  3. 하락 추세 필터: 24h 변동 < -1% 제외
  4. 저가 추락 필터: 현재가가 저가 근처(하위 20%) + 하락 중 제외
  5. 캔들 모멘텀 분석: 최근 6개 캔들 종가 추세 + 연속 상승/하락 스트릭
- **가중 스코어링**: 변동성(25%) + 상승추세(25%) + 모멘텀(25%) + 거래량(15%) + 가격위치(10%)
- 상위 10개만 AI에게 전달 → AI는 검증된 후보풀에서만 선택

#### 스마트 매도 로직 — 트레일링 익절 + 손절 관찰 (`trading_engine.py`)
- **상태 머신 도입**: `MONITORING` → `TRAILING_TP` / `OBSERVING_SL`
- **트레일링 익절 (낚시: 줄 풀어주기)**:
  - TP 돌파 시 즉시 매도하지 않고 고점 추적 모드 진입
  - 고점 대비 오프셋만큼 하락 시 매도 (오버익절 포착)
  - 오프셋 동적 조정: 갓 돌파 0.3% → 2%+ 초과 시 1.0%
  - 안전장치: 10분 타임아웃, TP의 50% 이하 급락 시 즉시 매도
  - 텔레그램 알림: 🎣 트레일링 진입
- **손절 관찰 (낚시: 찍고 반등 확인)**:
  - SL 터치 시 즉시 매도하지 않고 3회(30초) 관찰
  - 반등 감지(SL+15% 회복) → 손절 취소 + 텔레그램 알림 🔄
  - 관찰 중 급락(SL×1.5 이하) → 즉시 손절 (심화 손절)
  - 3회 관찰 후 반등 없음 → 하락 확인, 손절 실행

#### `strategy/ai_agent.py` 프롬프트 강화
- 스냅샷 텍스트에 변동폭%, 현재가 위치%, 모멘텀 스코어 추가
- 코인 선정 기준: "변동폭 vs 익절 현실성" 최우선 (변동폭 < TP×1.5 선정 금지)
- `select_coin(coin_scores=...)`: CoinSelector 스코어 데이터 수신

#### `strategy/strategy_optimizer.py` (v1.9.0에서 도입)
- 수익 극대화 전담 Agent: 낮은 익절(1~3.5%) + 넓은 손절(-2~-6%)
- 휴리스틱 즉각 판단 + LLM 심층 분석 2단계

#### 기본 파라미터 변경 (v1.8.0 → v2.0.0)
| 항목 | 기존 | 신규 |
|------|------|------|
| 익절 기본 범위 | 2%~8% | **1%~3.5%** |
| 손절 기본 범위 | -1%~-3% | **-2%~-6%** |
| R:R 요구 | 2:1 이상 | **1:1 이상** (승률 중심) |
| 익절 도달 시 | 즉시 매도 | **트레일링 추적** (오버익절 포착) |
| 손절 도달 시 | 즉시 매도 | **3회 관찰** (가짜 하락 필터링) |
| 코인 선정 | 전체 30개 → AI | **필터링 상위 10개 → AI** |

#### `main.py` 수정
- `CoinSelector()`, `StrategyOptimizer()` 생성 후 `TradingEngine`에 주입

---

## v1.8.0 (2026-04-04)

### 피드백 루프 강화 + 웹 대시보드 UX 개선

#### 피드백 루프 구조적 취약점 5건 수정
- **적응형 Clamp**: AI suggested 평균 ±1.5% 범위로 TP/SL clamp을 동적 수축 — AI 제안이 다음 결정에 실질적으로 강제 반영됨 (기존: 프롬프트에 "참고하되"로만 전달)
- **최근 거래 코인 정보**: 최근 5건의 코인명·결과·수익률·보유시간을 프롬프트에 주입 → 같은 코인 반복 선정·같은 실수 반복 방지
- **추세 방향**: 최근 5건 suggested_tp의 전반/후반 평균 비교로 하향·상향·유지 판정, 학습 방향성 전달
- **시간 기반 강제 탈출**: 보유 12시간(720분) 초과 시 AI 응답 무관하게 강제 매도 (exit_type="timeout")
- **동적 조정 기록 저장**: `_last_adjustment` 보존 → `save_evaluation()` 시 adjusted_tp/sl/reason 필드가 실제로 채워짐

#### 기동 전체 현금화 제거 (`strategy/trading_engine.py`)
- `run()` 시작 시 `_liquidate_all()` 호출 삭제 — 기존 포지션이 있으면 그대로 감시 이어감

#### 웹 대시보드 UX 개선 (`dashboard/web_server.py`)
- **타이틀**: `Pochaco Monitor` + profile.png 원형 프로필 이미지 + 초록 헬스 신호등
- **시간 표시**: 모든 시간에서 연도 제거 (mm-dd HH:MM:SS), 날짜/시간 2줄 분리
- **거래 내역**: 2줄 레이아웃 (1행: 시간·심볼·가격·수량·금액 / 2행: 구분 배지·비고), 그룹 구분선, 10건 단위 페이지네이션
- **AI 평가 섹션**: 2줄 레이아웃 (1행: 시간·코인·수익률·보유·TP/SL / 2행: 결과 배지·교훈), 3건 단위 페이지네이션
- **수량 표시**: 소수점 6자리 → 2자리로 통일
- **비고·교훈**: 35자 초과 시 말줄임 + [more]/[접기] 토글

---

## v1.7.0 (2026-04-03)

### 자기 개선형 AI Agent — 성과 기반 전략 피드백 루프

#### 핵심 개념
매매 결과가 다음 의사결정에 반영되는 **폐쇄형 학습 루프**를 도입했습니다.
`매수 → 감시(동적 조정) → 매도 → AI 평가 → 다음 코인 선정에 반영 → 반복`

#### StrategyEvaluation DB 모델 (`database/models.py`)
- 신규 테이블 `strategy_evaluations` 추가
- 저장 항목: 매매 결과(수익률·보유시간·종료유형), 원래 설정(TP/SL), AI 평가 텍스트, 제안 파라미터(suggested_tp/sl), 핵심 교훈, 동적 조정 이력

#### Repository 평가 CRUD (`database/repository.py`)
- `save_evaluation()` — 매매 후 평가 결과 저장
- `get_recent_evaluations(limit)` — 최근 N건 평가 조회
- `get_evaluation_stats(last_n)` — Agent 프롬프트 주입용 통계 집계
  (승률·평균 수익률·평균 보유시간·AI 제안 평균 TP/SL·최근 교훈)

#### AI Agent 고도화 (`strategy/ai_agent.py`)
- **`select_coin()` 과거 성과 반영**: `eval_stats` 파라미터 추가, 최근 매매 통계를 프롬프트에 주입
- **R:R 강제 완화**: 3:1 고정 → 2:1 이상 (익절 2~8%, 손절 -1~-3%)으로 현실적 조정
- **`evaluate_trade()` 신규**: 매도 완료 후 AI 성과 평가 + 다음 전략 파라미터 제안
- **`should_adjust_strategy()` 개선**: 보유 시간대별 가이드라인 (30분/2시간/6시간 임계)

#### TradingEngine 피드백 루프 연동 (`strategy/trading_engine.py`)
- **Post-Trade Evaluation**: 매도 직후 AI 평가 + DB 저장 + 텔레그램 알림
- **보유 중 동적 전략 조정**: 30분 간격 AI 재평가 → DB 업데이트 + 텔레그램 알림
- **코인 선정 시 과거 성과 전달**: `select_coin(snapshots, eval_stats=...)`

#### 터미널 대시보드 (`dashboard/terminal_ui.py`)
- **"AI 성과 평가 & 전략 조정" 패널 신설**

#### 웹 대시보드 (`dashboard/web_server.py`)
- JSON API에 `evaluations`, `eval_stats` 필드 추가
- HTML 페이지에 "AI 성과 평가 & 전략 조정" 섹션 추가

---

## v1.6.0 (2026-04-03)

### 빗썸 API v2 마이그레이션

#### BithumbClient 전면 교체 (`core/bithumb_client.py`)
- **인증 방식 변경**: HMAC-SHA512(v1) → JWT HS256(v2)
  - `_sign` 제거, `_jwt_header(params)` 추가
  - `Authorization: Bearer {jwt_token}` 헤더 방식
  - UUID nonce + millisecond timestamp + SHA512 query_hash
- **Private API 엔드포인트 변경**
  - `POST /info/balance` → `GET /v1/accounts`
  - `POST /trade/market_buy` → `POST /v1/orders` (`ord_type=price`, KRW 금액 직접 지정)
  - `POST /trade/market_sell` → `POST /v1/orders` (`ord_type=market`, 코인 수량 지정)
  - `POST /trade/place` → `POST /v1/orders` (`ord_type=limit`)
  - `POST /trade/cancel` → `DELETE /v1/order`
  - `POST /info/orders` → `GET /v1/orders`
- **Public API (ticker, orderbook, candlestick)**: v1 엔드포인트 그대로 유지 (인증 불필요)
- **하위 호환성 유지**: `get_balance()` 반환 포맷을 v1 스타일로 정규화, trading_engine.py 변경 없음
- **의존성 추가**: `PyJWT>=2.8.0`

---

## v1.5.0 (2026-04-03)

### 안정성 개선 + 텔레그램 로그 조회

#### BithumbClient 리팩토링 (`core/bithumb_client.py`)
- **`_private_post` 공통 메서드 추가**: 모든 Private API 호출이 단일 메서드를 통해 서명·요청·응답 처리 (코드 중복 제거)
- **`_sign` 개선**: nonce를 마이크로초 단위로 변경 (충돌 방지), `params` 원본 불변성 보장 (`sign_params` 분리)
- **`market_buy` 방식 변경**: 호가 창(orderbook) 기반으로 수량 계산 후 매수 (수수료 안전마진 0.15% 적용)
- **`cancel_all_orders` 신규 추가**: 심볼별 미체결 주문 일괄 취소
- **`get_krw_balance_detail` 신규 추가**: available / total / in_use KRW 상세 반환
- `get_krw_balance`에 available·total·in_use 로깅 추가

#### TradingEngine 안정성 강화 (`strategy/trading_engine.py`)
- **`_cancel_stuck_orders` 신규 추가**: `in_use_krw > 0` 감지 시 미체결 주문 일괄 취소
- 기동 시 `_liquidate_all` 전 미체결 주문 정리 순서 보장
- 매수 실패 시 미체결 취소 후 1회 자동 재시도

#### Repository 멀티스레드 안전성 (`database/repository.py`)
- **요청별 독립 세션** 패턴 도입: 단일 공유 `Session` → `@contextmanager _session()` 교체
- 모든 CRUD 메서드가 별도 세션·커밋·롤백·close를 자동 처리
- `get_daily_activity_summary`, `get_total_stats` 등 통계 조회 메서드 추가

#### DB 엔진 개선 (`database/models.py`)
- SQLite PRAGMA 설정 방식 수정: `conn.execute` → `cursor.execute` (SQLAlchemy 최신 API 호환)

#### 텔레그램 봇 — `/log` 명령어 추가 (`core/telegram_bot.py`)
- `/log` — 최근 50줄 조회 (기본값)
- `/log N` — 최근 N줄 조회 (1~500 범위)
- 텔레그램 4096자 제한 자동 처리 (초과 시 앞부분 생략)
- `LOG_FILE` 경로(`settings.LOG_FILE`)에서 직접 읽기

---

## v1.4.0 (2026-04-02)

### AWS EC2 배포 및 Secrets Manager 연동

#### AWS 인프라
- **AWS Secrets Manager 연동** (`config/settings.py`): 앱 기동 시 `AWS_SECRET_NAME` 환경변수가 있으면 Secrets Manager에서 민감 정보를 자동으로 `os.environ`에 주입, `.env` 파일에는 비민감 설정만 보관
- **IAM 역할 자동 설정** (`deploy/create_secrets.sh`): EC2 인스턴스 프로파일, Secrets Manager 읽기 전용 정책, 인스턴스 프로파일 연결을 스크립트 1회 실행으로 완성
- Secrets Manager에 저장되는 키: `BITHUMB_API_KEY`, `BITHUMB_SECRET_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

#### 배포 자동화
- **`deploy/setup_ec2.sh`**: Ubuntu 22.04 EC2에서 Python 3.12·uv·systemd 서비스까지 1회 실행으로 초기화
- **`deploy/deploy.sh`**: 로컬 → EC2 rsync 코드 동기화 + 의존성 설치 + 서비스 재시작 자동화
- **`deploy/pochaco.service`**: systemd 유닛 파일 (자동 재시작, journald 로그 수집, 보안 강화 옵션)

#### 헤드리스 모드
- `HEADLESS=true` 설정 시 터미널 UI 없이 서비스로 실행 (systemd 환경 대응)
- 웹 대시보드 + 텔레그램으로 모든 모니터링 가능

#### 텔레그램 `/dashboard` 개선
- 기존 ipify.org 대신 EC2 IMDSv2 메타데이터 API로 공인 IP 조회 (빠르고 신뢰성 높음)
- IMDSv2 실패 시 IMDSv1 → ipify.org 순으로 폴백

#### 신규 설정값
| 환경변수 | 설명 | 기본값 |
|---------|------|--------|
| `HEADLESS` | 터미널 UI 비활성화 (서비스 모드) | `false` |
| `AWS_SECRET_NAME` | Secrets Manager 시크릿 이름 | `""` |
| `AWS_REGION` | AWS 리전 | `ap-northeast-2` |

---

## v1.3.0 (2026-04-02)

### 텔레그램 봇 + 웹 대시보드

#### 텔레그램 봇 (`core/telegram_bot.py`)
- 허가된 `TELEGRAM_CHAT_ID`에서만 명령어 수신 (보안)
- Long-polling 동기 구현 (추가 비동기 라이브러리 불필요)
- **명령어**: `/status`, `/balance`, `/position`, `/stop`, `/resume`, `/report`, `/dashboard`, `/help`
- **자동 알림**: 매수 완료 🟢 / 익절 ✅ / 손절 🔴 / 오류 ⚠️ / 일별 리포트 📈

#### 웹 대시보드 (`dashboard/web_server.py`)
- Python 내장 `http.server` 기반, 추가 의존성 없음
- `GET /` — 자산·포지션·일별성과·거래내역 HTML 페이지 (30초 자동 갱신)
- `GET /api/status` — JSON 상태 API
- 다크 테마 반응형 UI (인라인 CSS)

#### TradingEngine 제어 인터페이스
- `pause()` / `resume()` / `is_paused` 추가 — 텔레그램 `/stop`, `/resume`으로 신규 매수 일시 중지 (기존 포지션 감시 유지)
- `set_notifier(bot)` — 텔레그램 봇 주입, 매수·매도·오류 시 자동 알림
- 일별 리포트 저장 시 텔레그램 자동 발송 (`scheduler/jobs.py`)

#### 신규 설정값
| 환경변수 | 설명 | 기본값 |
|---------|------|--------|
| `TELEGRAM_ENABLED` | 텔레그램 봇 활성화 | `false` |
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 | `""` |
| `TELEGRAM_CHAT_ID` | 허가된 채팅 ID | `""` |
| `DASHBOARD_ENABLED` | 웹 대시보드 활성화 | `true` |
| `DASHBOARD_HOST` | 바인딩 주소 | `0.0.0.0` |
| `DASHBOARD_PORT` | 포트 | `8080` |

---

## v1.2.0 (2026-04-02)

### 대시보드 전면 개편 + AI 프롬프트 고도화

#### AI 프롬프트 개선 (`strategy/ai_agent.py`)
- **공격적 수익 기준 적용**: 손절 최대 -2% 강제 제한, R:R 3:1 이상 요구
- **코인 선정 기준 강화**: 상승 모멘텀 중인 코인 우선, 하락 추세 코인 명시 제외
- **거래대금 기준 상향**: 50억/24h 이상으로 강화 (슬리피지 최소화)
- 익절 기준 최소 3% 이상, 수수료(0.4%) 감안 순이익 보장 명시

#### 터미널 대시보드 전면 재작성 (`dashboard/terminal_ui.py`)
| 패널 | 내용 |
|------|------|
| 자산 평가 | 총자산(KRW+코인평가), 누적손익·수익률, 승률, 평균 보유시간, 총 매매횟수 |
| 현재 포지션 | 매수가→현재가, 손익, 익절·손절 기준, 보유시간, 익절달성률 바, AI 선정이유 |
| 자산 변동 차트 | ASCII 바 차트 (일별 총자산, 상승=green, 하락=red, 오늘=▶실시간) |
| AI 일별 보고서 | 날짜별 선정코인, 매매수, 익절/손절, 승률, 일수익률, LLM 정보 |
| 전체 거래 내역 | 최근 30건 (시간·심볼·구분·가격·수량·금액·비고) |

#### Repository 개선 (`database/repository.py`)
- **버그 수정**: `open_position()`에 `llm_provider` 파라미터 누락 수정 (엔진에서 전달하나 저장 안 되던 문제)
- `get_all_trades()`, `get_closed_positions()`, `get_all_daily_reports()` 메서드 추가
- `get_daily_activity_summary()` 신설: 포지션+거래 기반 일별 AI 행동 집계
- `get_total_stats()` 재작성: 승률, 평균 보유시간, 누적 손익, 초기 자본 포함

---

## v1.1.0 (2026-04-02)

### 전략 변경

- **9시 리셋 전략 제거**: 매일 오전 9시 현금화 + 5분 대기 방식 폐기
- **연속 사이클 전략 도입**: 기동 즉시 현금화 → AI 선정 → 매수 → 익절/손절 → 즉시 반복
- `TradingEngine` 신설 (`strategy/trading_engine.py`): 단일 루프로 전체 사이클 관리, 스케줄러 의존 제거
- `PositionManager` 통합: 포지션 감시·매도 로직을 `TradingEngine` 내부로 흡수
- `TradingScheduler` 역할 축소: 백업(23:50) + 일별 리포트(23:55) 전용으로 단순화
- **AI 코인 선정 프롬프트 개선**: 변동성(고저폭·등락률) + 거래대금 + 모멘텀 방향 + R/R 비율 기준으로 교체
- `settings.py`에서 `DAILY_RESET_HOUR`, `HOLD_AFTER_RESET_SECONDS` 제거
- `extra="ignore"` 설정 추가로 `.env` 잔류 키 허용

### 버그 수정
- `pydantic_settings` extra 필드 오류 수정 (`extra="ignore"`)

---

## v1.0.0 (2026-04-02)

### 개요
빗썸(Bithumb) 거래소 API를 기반으로 하는 AI 자동매매 시스템 초기 릴리즈.
Claude / ChatGPT / Gemini 중 선택한 LLM이 코인을 선정하고 익절·손절 기준을 결정하며,
스케줄러가 매일 오전 9시에 포트폴리오를 리셋한 뒤 새로운 포지션을 구성한다.

### 주요 기능
- 빗썸 REST API + WebSocket 연동 (HMAC-SHA512 인증)
- LLM 다중 공급자 추상화 (Anthropic / OpenAI / Gemini)
- AI 코인 선정 + 익절·손절 자동 매매
- SQLite WAL 모드 / PostgreSQL 전환 지원
- Rich 터미널 대시보드
- APScheduler 기반 스케줄 작업

### 버그 수정
- 빗썸 `Api-Sign` 헤더: `hexdigest()` → `Base64(hexdigest())` 수정
- KRW 잔고 조회: `currency=ALL` 응답에서 추출하도록 수정
