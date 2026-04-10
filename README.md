# pochaco 🤖

빗썸(Bithumb) 거래소 기반 **AI 자동매매 시스템**.  
Claude / GPT-4o / Gemini 중 선택한 LLM이 실시간 시장 데이터를 분석해 **8코인 분산 포트폴리오**를 구성하고,
포트폴리오 종합 P&L을 기반으로 익절·손절을 자동으로 관리합니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 🤖 7개 전문가 Agent | 시장 분석가·자산 운용가·매수 전문가·매도 전문가·포트폴리오 평가가·총괄 평가가·특성 분석가 분업 체계 |
| 🗂️ 8코인 분산 포트폴리오 | 매 사이클마다 8개 코인을 동시 매수. 랜덤 이름 부여 (예: "판다-07"). 포트폴리오 단위 손익 통합 관리 |
| 📊 총괄 평가 시스템 | 6시간 주기(0·6·12·18시) 전문가별 0~100 점수화 + 누적 피드백(최근 3회) → 프롬프트에 동적 주입 (지속 개선) |
| 🎯 종목 사전 필터링 | CoinSelector — 변동폭·거래량·모멘텀 기반 스코어링 → 상위 20개 후보 → AI에 전달 |
| 🧠 3단계 매수 파이프라인 | 시장 분석 → 자산 배분(투자 비율 동적 결정) → 8코인 포트폴리오 선정·TP/SL 결정 |
| 🔄 연속 매매 사이클 | 포트폴리오 구성 → 8코인 동시 보유 → 3단계 손절/트레일링 익절 → 평가 → 반복 |
| 📈 자기 개선형 Agent | 매매 후 AI 성과 평가 → 다음 포트폴리오에 즉시 반영 + 누적 피드백 히스토리 추이 추적 |
| ✂️ 3단계 분할 손절 | -1.0%→33% 매도 / -1.5%→잔여 50% 매도 / -2.0% 하드캡→잔여 전량 청산. 최대 손실 -2% |
| 🎣 트레일링 익절 | 포트폴리오 P&L이 TP 도달 → 고점 추적 → 설정 폭 하락 시 전량 청산 (타임아웃 30분) |
| ⚡ 전략 최적화 | StrategyOptimizer — 매매 즉시 다음 포트폴리오 TP/SL 파라미터 재결정 (TP 3~8%) |
| 🔧 동적 전략 조정 | 보유 중 30분 간격으로 AI가 포트폴리오 익절/손절 기준을 재평가·조정 |
| 🚫 재매수 쿨다운 | 포트폴리오 청산 시 8개 심볼 전체 쿨다운 등록. 동일 종목 조기 재편입 차단 |
| 🧬 코인 특성 학습 | CoinProfileAnalyst — 매매마다 코인별 프로파일 누적·갱신. 매수 전 이력 코인 자동 주입 |
| 📊 터미널 대시보드 | Rich Live 기반 실시간 자산·포트폴리오·성과 패널 |
| 🌐 웹 대시보드 | 종합 대시보드 + 전문가 실적표 탭 분리. 각 전문가 카드에서 프롬프트 확인·수정, Agent와 직접 대화 가능 |
| 📱 텔레그램 봇 | 상태 조회·매매 제어·자동 알림 (포트폴리오 구성·종료·전략 조정 포함) |
| ☁️ AWS 연동 | Secrets Manager API 키 관리 + EC2 systemd 배포 |
| 🗄️ 다중 DB | SQLite (기본, WAL 모드) / PostgreSQL 전환 지원 |
| 🔌 다중 LLM | Anthropic / OpenAI / Gemini — `.env` 한 줄로 교체 |

---

## 시스템 구조

```
pochaco/
├── config/
│   └── settings.py          # pydantic-settings + AWS Secrets Manager 연동
├── core/
│   ├── bithumb_client.py    # 빗썸 REST API (JWT HS256 v2 인증)
│   ├── websocket_client.py  # 빗썸 실시간 WebSocket
│   ├── llm_provider.py      # LLM 추상화 (Anthropic / OpenAI / Gemini)
│   └── telegram_bot.py      # 텔레그램 봇 (명령어 + 알림)
├── strategy/
│   ├── agents/              # 7개 전문가 Agent
│   │   ├── base_agent.py    # BaseSpecialistAgent 추상 기반 (누적 피드백 히스토리)
│   │   ├── market_analyst.py      # 시장 분석가 (시장 흐름·리스크 판단)
│   │   ├── asset_manager.py       # 자산 운용가 (투자 비율 동적 결정)
│   │   ├── buy_strategist.py      # 매수 전문가 (8코인 포트폴리오 선정·TP/SL)
│   │   ├── sell_strategist.py     # 매도 전문가 (포트폴리오 TP/SL 동적 조정)
│   │   ├── portfolio_evaluator.py # 포트폴리오 평가가 (종합 성과 분석·파라미터 제안)
│   │   ├── meta_evaluator.py      # 총괄 평가가 (6시간 주기 전문가 평가·피드백)
│   │   └── coin_profile_analyst.py # 특성 분석가 (코인별 프로파일 누적 학습)
│   ├── agent_coordinator.py # AgentCoordinator — 7개 전문가 오케스트레이션
│   ├── ai_agent.py          # AI dataclass (PortfolioDecision, PortfolioCoinPick, TradeEvaluation)
│   ├── portfolio_names.py   # 랜덤 포트폴리오 이름 생성기 (동물·색상·식물)
│   ├── coin_selector.py     # CoinSelector — 변동성·모멘텀 기반 종목 사전 필터링 (상위 20개)
│   ├── strategy_optimizer.py # StrategyOptimizer — 포트폴리오 TP/SL 파라미터 최적화
│   ├── market_analyzer.py   # 시장 데이터 수집·가공 (CoinSnapshot)
│   ├── cooldown.py          # 재매수 쿨다운 레지스트리 (포트폴리오 8개 심볼 일괄 등록)
│   └── trading_engine.py    # 매매 루프 엔진 (포트폴리오 상태 머신 + 3단계 분할 손절)
├── scheduler/
│   └── jobs.py              # APScheduler (백업 / 리포트 / 총괄 전문가 평가)
├── database/
│   ├── models.py            # ORM: Portfolio / Position / Trade / StrategyEvaluation / AgentScore / AgentDecisionLog
│   ├── repository.py        # CRUD 레이어 (포트폴리오 단위 CRUD 포함)
│   └── backup.py            # SQLite 자동 백업
├── data/
│   └── coin_profiles/       # 코인별 특성 프로파일 (SYMBOL.md, 매매마다 누적 갱신)
├── dashboard/
│   ├── terminal_ui.py       # Rich Live 터미널 대시보드 (포트폴리오 요약 패널)
│   └── web_server.py        # HTTP 웹 대시보드 (종합 + 전문가 실적표 2페이지)
├── deploy/
│   ├── create_secrets.sh    # AWS Secrets Manager + IAM 초기 설정
│   ├── setup_ec2.sh         # EC2 환경 초기화 (1회)
│   ├── deploy.sh            # 코드 배포 + 서비스 재시작
│   └── pochaco.service      # systemd 유닛 파일
├── main.py                  # 진입점 (의존성 주입 + 스레드 조율)
├── requirements.txt
└── .env.example
```

---

## 매매 흐름도

```
┌─────────────────────────────────────────────────────────────┐
│                        pochaco 기동                          │
└──────────────────────────┬──────────────────────────────────┘
                           │
               ┌───────────▼────────────┐
               │   StrategyOptimizer    │  기존 매매 데이터로
               │   초기 파라미터 결정    │  즉시 TP/SL 범위 설정
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │  기존 포트폴리오 확인    │  열린 포트폴리오 있으면 감시 이어감
               └───────────┬────────────┘
                           │ (포트폴리오 없을 때)
               ┌───────────▼────────────┐
               │   시장 데이터 수집       │
               │  거래대금 상위 30개 코인 │  현재가·등락률·캔들 수집
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   CoinSelector 필터링   │
               │  - 변동폭 < TP×1.5 제외 │  ← 등락폭 vs 기대수익 검증
               │  - 거래대금 50억 미만 제외│
               │  - 하락 추세 제외        │
               │  - 쿨다운 종목 제외      │
               │  - 스코어링 → 상위 20개  │
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   3단계 AI 파이프라인    │
               │  1) 시장 분석가          │  시장 심리·리스크 판단
               │  2) 자산 운용가          │  투자 비율 동적 결정
               │  3) 매수 전문가          │  8코인 포트폴리오 선정
               │     + 특성 분석가 주입   │  ← 이력 코인 프로파일 참고
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   8코인 동시 매수       │  KRW를 8등분 (12.5%씩)
               │   포트폴리오 이름 부여   │  예: "판다-07"
               └───────────┬────────────┘
                           │
            ┌──────────────▼──────────────────────┐
            │   포트폴리오 종합 P&L 감시 (10초 주기) │
            │                                      │
            │  TP 도달 ──► 트레일링 모드 🎣          │
            │    종합 P&L 고점 추적 → 하락 시 전량   │
            │                                      │
            │  -1.0% ──► 1차 분할 매도 ✂️           │
            │    8코인 각각 33% 매도                 │
            │                                      │
            │  -1.5% ──► 2차 분할 매도 ✂️           │
            │    잔여 50% 추가 매도                  │
            │                                      │
            │  -2.0% ──► 전량 청산 (하드캡) 🔴       │
            │                                      │
            │  [30분 간격] AI 전략 동적 조정          │
            └──────────────┬──────────────────────┘
                           │  포트폴리오 종료
                           │
               ┌───────────▼────────────┐
               │   AI 성과 평가          │
               │  - 포트폴리오 종합 평가  │
               │  - 다음 포트폴리오 TP/SL │
               │  - 8개 코인 프로파일 갱신│  ← 특성 분석가
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   StrategyOptimizer    │  즉시 파라미터 재결정
               │   즉각 재최적화         │  → 다음 포트폴리오에 반영
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   쿨다운 등록           │  8개 심볼 전체 쿨다운 일괄 등록
               └───────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  텔레그램    │  포트폴리오 결과 + 평가 알림
                    │  알림 발송   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  사이클 반복  │  ← CoinSelector 필터링부터 시작
                    └─────────────┘

[스케줄 작업 - 별도 스레드]
  23:50 ─── SQLite 자동 백업
  23:55 ─── 일별 성과 리포트 저장 + 텔레그램 발송
  0·6·12·18시 ─── 총괄 평가 (MetaEvaluator) → 7개 전문가 피드백 주입
```

---

## 설치 및 실행

### 사전 조건
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) 패키지 매니저
- 빗썸 API 키 (Connect IP 등록 필요)
- LLM API 키 (Anthropic / OpenAI / Gemini 중 하나)

### 로컬 실행

```bash
# 1. 저장소 클론
git clone https://github.com/khaneun/pochaco.git
cd pochaco

# 2. 가상환경 + 패키지 설치
uv venv .venv --python python3.12
uv pip install -r requirements.txt

# 3. 환경 설정
cp .env.example .env
# .env 열어서 API 키 입력

# 4. 실행 (터미널 대시보드 모드)
.venv/bin/python main.py
```

### 환경 설정 (`.env`)

```dotenv
# ── 빗썸 API ─────────────────────────────
BITHUMB_API_KEY=your_key
BITHUMB_SECRET_KEY=your_secret

# ── LLM 공급자 ────────────────────────────
LLM_PROVIDER=openai          # anthropic | openai | gemini
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...

# ── 텔레그램 봇 ───────────────────────────
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=1234567890:ABC...
TELEGRAM_CHAT_ID=123456789

# ── 웹 대시보드 ───────────────────────────
DASHBOARD_ENABLED=true
DASHBOARD_PORT=8080

# ── 실행 모드 (EC2 서비스는 true) ──────────
HEADLESS=false
```

전체 설정 목록은 `.env.example` 참조.

---

## 텔레그램 봇

| 명령어 | 기능 |
|--------|------|
| `/status` | 총자산, 포트폴리오 현황, 승률, 매매 상태 요약 |
| `/balance` | KRW 잔고 |
| `/position` | 현재 포트폴리오 상세 (종합 손익, 8개 코인, 보유시간) |
| `/stop` | 신규 매수 일시 중지 (포트폴리오 감시는 유지) |
| `/resume` | 매수 재개 |
| `/report` | 최근 7일 성과 요약 |
| `/log [N]` | 최근 로그 N줄 조회 (기본 50, 최대 500) |
| `/dashboard` | 웹 대시보드 접속 주소 (EC2 퍼블릭 IP 자동 조회) |
| `/help` | 명령어 목록 |

**자동 알림 종류**

| 알림 | 내용 |
|------|------|
| 🟢 포트폴리오 매수 | 이름(예: 판다-07)·8개 코인 목록·총 투입금·TP/SL |
| ✂️ 분할 매도 | 낙폭(-1.0%/-1.5%) + 매도 비율 |
| ✅ 익절 / 🔴 손절 | 포트폴리오 종합 수익률·손익 금액·8개 코인 결과 요약 |
| 🔄 전략 조정 | 보유 중 AI가 포트폴리오 익절/손절 기준 변경 시 |
| 📊 포트폴리오 평가 | 매도 후 AI 성과 평가 + 다음 제안 TP/SL + 교훈 |
| 📈 일별 리포트 | 당일 성과 요약 (23:55) |
| ⚠️ 오류 | 매도 실패 등 이상 상황 |

**텔레그램 봇 설정 방법**
1. `@BotFather` → `/newbot` → 토큰 발급
2. 봇에게 아무 메시지 전송
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` 에서 `chat.id` 확인
4. `.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 입력

---

## AWS EC2 배포

### 1. Secrets Manager + IAM 초기 설정 (로컬, 1회)

```bash
# EC2 인스턴스 ID 없이 실행하면 Secrets Manager + IAM 역할만 생성
bash deploy/create_secrets.sh

# EC2 인스턴스에 IAM 프로파일 연결
EC2_INSTANCE_ID=i-xxxxxxxxxxxx bash deploy/create_secrets.sh
```

이 스크립트가 하는 일:
- `.env`의 API 키 7개를 `pochaco/production` 시크릿에 저장
- EC2용 IAM 역할 + Secrets Manager 읽기 정책 생성
- EC2 인스턴스에 IAM 프로파일 연결

### 2. EC2 초기 환경 설정 (EC2에서, 1회)

```bash
ssh -i ~/.ssh/key.pem ubuntu@<EC2-IP>

# 코드 먼저 배포 후
bash /opt/pochaco/deploy/setup_ec2.sh
```

### 3. 코드 배포 (업데이트마다)

```bash
EC2_HOST=ubuntu@<EC2-IP> SSH_KEY=~/.ssh/key.pem bash deploy/deploy.sh
```

### EC2 `.env` (민감 정보 없음)

```dotenv
# AWS Secrets Manager가 API 키를 자동 주입
AWS_SECRET_NAME=pochaco/production
AWS_REGION=ap-northeast-2

HEADLESS=true
LLM_PROVIDER=openai
TELEGRAM_ENABLED=true
DASHBOARD_ENABLED=true
DASHBOARD_PORT=8080
DB_PATH=/opt/pochaco/data/pochaco.db
LOG_FILE=/opt/pochaco/logs/pochaco.log
```

### EC2 서비스 관리

```bash
# 상태 확인
sudo systemctl status pochaco

# 실시간 로그
journalctl -u pochaco -f

# 재시작 / 중지
sudo systemctl restart pochaco
sudo systemctl stop pochaco
```

### 보안 그룹 (최소 오픈 포트)

| 포트 | 프로토콜 | 용도 |
|------|---------|------|
| 22 | TCP | SSH 접속 |
| 8080 | TCP | 웹 대시보드 |

---

## 웹 대시보드

서버 기동 후 `http://<서버IP>:8080` 접속

| 엔드포인트 | 내용 |
|-----------|------|
| `GET /` | HTML 대시보드 (30초 자동 갱신) |
| `GET /experts` | 전문가 실적표 (점수·피드백·프롬프트·대화) |
| `GET /api/status` | JSON 상태 (자산·포트폴리오·거래내역·성과) |
| `POST /api/liquidate` | 현재 포트폴리오 전량 시장가 청산 |
| `GET /api/agent/prompt?role=...` | 특정 전문가 base_prompt + feedback_prompt 조회 |
| `POST /api/agent/chat` | `{role, message, history}` → Agent LLM 응답 (멀티턴) |
| `POST /api/agent/update_prompt` | `{role, new_prompt}` → base_prompt 즉시 업데이트 |

텔레그램에서 `/dashboard` 입력 시 접속 주소를 알려줍니다.

---

## 데이터베이스

### 모델
| 테이블 | 설명 |
|--------|------|
| `portfolios` | 포트폴리오 이력 (이름, 총 투입금액, TP/SL, is_open) |
| `positions` | 포트폴리오 하위 개별 코인 포지션 (portfolio_id FK) |
| `trades` | 개별 매수·매도 내역 (portfolio_id 참조) |
| `daily_reports` | 일별 성과 (23:55 자동 저장) |
| `strategy_evaluations` | 포트폴리오 종합 평가 + 제안 TP/SL + 교훈 + 코인별 요약 |
| `agent_scores` | 전문가별 점수 이력 (6시간 주기) |
| `agent_decision_logs` | 전문가별 의사결정 기록 (입력/출력 요약) |

### SQLite 안전 설정 (기본)
- **WAL 모드**: 크래시 후 데이터 손실 방지
- `synchronous=NORMAL`, `busy_timeout=30000`
- EC2 재시작 후에도 데이터 유지: DB 경로를 EBS 볼륨 마운트 경로로 설정

### PostgreSQL 전환
```dotenv
DATABASE_URL=postgresql+psycopg2://user:password@host:5432/pochaco
```

---

## 전체 설정값

| 환경변수 | 설명 | 기본값 |
|---------|------|--------|
| `LLM_PROVIDER` | LLM 공급자 | `anthropic` |
| `ANTHROPIC_MODEL` | Claude 모델 | `claude-opus-4-6` |
| `OPENAI_MODEL` | OpenAI 모델 | `gpt-4o` |
| `GEMINI_MODEL` | Gemini 모델 | `gemini-1.5-pro` |
| `MIN_ORDER_KRW` | 최소 주문금액 | `5000` |
| `POSITION_CHECK_INTERVAL` | 포지션 감시 주기(초) | `10` |
| `HEADLESS` | 터미널 UI 비활성화 | `false` |
| `TELEGRAM_ENABLED` | 텔레그램 봇 활성화 | `false` |
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 | `""` |
| `TELEGRAM_CHAT_ID` | 허가된 채팅 ID | `""` |
| `DASHBOARD_ENABLED` | 웹 대시보드 활성화 | `true` |
| `DASHBOARD_HOST` | 바인딩 주소 | `0.0.0.0` |
| `DASHBOARD_PORT` | 웹 대시보드 포트 | `8080` |
| `DATABASE_URL` | 외부 DB (비우면 SQLite) | `""` |
| `DB_PATH` | SQLite 경로 | `/opt/pochaco/data/pochaco.db` |
| `DB_BACKUP_DIR` | 백업 디렉터리 | `/opt/pochaco/backup` |
| `DB_BACKUP_KEEP_DAYS` | 백업 보관 일수 | `7` |
| `AWS_SECRET_NAME` | Secrets Manager 시크릿 이름 | `""` |
| `AWS_REGION` | AWS 리전 | `ap-northeast-2` |
| `LOG_LEVEL` | 로그 레벨 | `INFO` |
| `LOG_FILE` | 로그 파일 경로 | `/opt/pochaco/logs/pochaco.log` |

---

## 의존 패키지

```
requests, websockets, APScheduler, SQLAlchemy, rich, python-dotenv, pydantic-settings
anthropic, openai, google-generativeai   ← 사용하는 LLM만 설치
boto3                                    ← AWS Secrets Manager 연동
PyJWT                                    ← 빗썸 API v2 JWT 인증
```
