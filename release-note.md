# Release Notes

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
