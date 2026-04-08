# pochaco 🤖

빗썸(Bithumb) 거래소 기반 **AI 자동매매 시스템**.  
Claude / GPT-4o / Gemini 중 선택한 LLM이 실시간 시장 데이터를 분석해 코인을 선정하고,
익절·손절 기준을 결정하며 포지션을 자동으로 관리합니다.

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 🎯 종목 사전 필터링 | CoinSelector — 변동폭·거래량·모멘텀 기반 스코어링 → 상위 10개만 AI에 전달 |
| 🧠 AI 코인 선정 | 검증된 후보풀에서 1개 선정 + 익절/1차 손절/2차 손절% AI 결정 |
| 🔄 연속 매매 사이클 | AI 선정 → 전액 매수 → 2단계 손절/트레일링 익절 → 평가 → 반복 |
| 📈 자기 개선형 Agent | 매매 후 AI 성과 평가 → suggested 가중평균 → 다음 선정에 즉시 반영 (전략 vs 시장 악화 자동 구분) |
| ✂️ 타이트 2단계 손절 | 1차 SL(-0.5~-1.5%) 도달 시 50%만 매도·반등 대기, 2차 SL(-0.8~-2.5%) 도달 시 잔량 청산. 실효 손실 ≈ -1.5% |
| 🎣 단계별 트레일링 익절 | 5%+ 진입 후 구간별 드랍포인트 추적: 5~7%→0.8%, 7~10%→1.2%, 10~15%→1.8%, 15%+→2.5% (타임아웃 30분) |
| ⚡ 전략 최적화 | StrategyOptimizer — 매매 즉시 파라미터 재결정. 익절 4~10%, 2차 손절 -0.8~-2.5% 타이트 범위 |
| 🔧 동적 전략 조정 | 보유 중 30분 간격으로 AI가 익절/SL1/SL2 기준을 재평가·조정 |
| 🚫 재매수 쿨다운 | 익절 30분·손절 10분·수동청산 60분 동안 동일 종목 재매수 차단 (하드 필터 + AI 프롬프트 이중 보호) |
| 📊 터미널 대시보드 | Rich Live 기반 실시간 자산·포지션·차트·AI 보고서·성과 평가 패널 |
| 🌐 웹 대시보드 | 브라우저 접속 HTML 페이지 + JSON API + 포지션 청산 버튼 |
| 📱 텔레그램 봇 | 상태 조회·매매 제어·자동 알림 (전략 조정·평가 메시지 포함) |
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
│   ├── coin_selector.py     # CoinSelector — 변동성·모멘텀 기반 종목 사전 필터링
│   ├── ai_agent.py          # AI 의사결정 (코인 선정 + 성과 평가 + 동적 조정)
│   ├── strategy_optimizer.py # StrategyOptimizer — 익절/손절 파라미터 즉각 최적화
│   ├── market_analyzer.py   # 시장 데이터 수집·가공 (CoinSnapshot)
│   ├── cooldown.py          # 재매수 쿨다운 레지스트리 (익절·손절·수동청산 공유)
│   └── trading_engine.py    # 매매 루프 엔진 (스마트 매도 상태 머신 포함)
├── scheduler/
│   └── jobs.py              # APScheduler (23:50 백업 / 23:55 리포트)
├── database/
│   ├── models.py            # ORM 모델: Trade / Position / DailyReport / StrategyEvaluation
│   ├── repository.py        # CRUD 레이어 (평가 저장·통계 집계 포함)
│   └── backup.py            # SQLite 자동 백업
├── dashboard/
│   ├── terminal_ui.py       # Rich Live 터미널 대시보드 (평가 패널 포함)
│   └── web_server.py        # HTTP 웹 대시보드 (포트 8080, 평가 섹션 포함)
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
               │   기존 포지션 확인       │  포지션 있으면 감시 이어감
               └───────────┬────────────┘
                           │ (포지션 없을 때)
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
               │  - 쿨다운 종목 제외      │  ← 익절 30분·손절 10분·수동 60분
               │  - 스코어링 → 상위 10개  │
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   LLM 코인 선정         │
               │  검증된 후보풀에서 선택   │  ← StrategyOptimizer clamp
               │  - 익절% 결정 (4~10%)   │    + CoinSelector 스코어
               │  - 1차 SL% 결정 (~-1%) │
               │  - 2차 SL% 결정 (~-1.5%)│
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   시장가 매수           │
               │   KRW 잔고 95% 투입     │
               └───────────┬────────────┘
                           │
            ┌──────────────▼──────────────────┐
            │   스마트 포지션 감시 (10초 주기)    │
            │                                  │
            │  5%+ 돌파 ──► 트레일링 모드 🎣    │
            │    구간별 드랍포인트 추적           │
            │    5~7%:0.8% / 7~10%:1.2%       │
            │    10~15%:1.8% / 15%+:2.5%      │
            │                                  │
            │  1차 SL 터치 ──► 50% 즉시 매도 ✂️ │
            │    (~-1%, 타이트) 나머지 반등 대기  │
            │                                  │
            │  2차 SL 터치 ──► 잔량 전체 매도   │
            │    (~-1.5%, 1차 SL 실행 후에만)   │
            │                                  │
            │  보유 12시간 초과 ──► 강제 매도    │
            │  [30분 간격] AI 전략 동적 조정     │
            └──────────────┬──────────────────┘
                           │  매도 완료
                           │
               ┌───────────▼────────────┐
               │   AI 성과 평가          │
               │  - 이번 매매 평가       │
               │  - 다음 전략 파라미터   │
               │    제안 (TP/SL1/SL2)   │
               │  - 교훈 기록 → DB 저장  │
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   StrategyOptimizer    │  즉시 파라미터 재결정
               │   즉각 재최적화         │  → 다음 사이클에 반영
               └───────────┬────────────┘
                           │
               ┌───────────▼────────────┐
               │   쿨다운 레지스트리 등록  │  익절→30분 / 손절→10분
               │   (수동 청산 시 60분)    │  다음 사이클 CoinSelector가 차단
               └───────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  텔레그램    │  매매 결과 + 평가 + 최적화 알림
                    │  알림 발송   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  사이클 반복  │  ← CoinSelector 필터링부터 시작
                    └─────────────┘

[스케줄 작업 - 별도 스레드]
  23:50 ─── SQLite 자동 백업
  23:55 ─── 일별 성과 리포트 저장 + 텔레그램 발송
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
| `/status` | 총자산, 포지션, 승률, 매매 상태 요약 |
| `/balance` | KRW 잔고 |
| `/position` | 현재 포지션 상세 (손익, 보유시간, AI 이유) |
| `/stop` | 신규 매수 일시 중지 (포지션 감시는 유지) |
| `/resume` | 매수 재개 |
| `/report` | 최근 7일 성과 요약 |
| `/log [N]` | 최근 로그 N줄 조회 (기본 50, 최대 500) |
| `/dashboard` | 웹 대시보드 접속 주소 (EC2 퍼블릭 IP 자동 조회) |
| `/help` | 명령어 목록 |

**자동 알림 종류**

| 알림 | 내용 |
|------|------|
| 🟢 매수 완료 | 코인·매수가·수량·익절/손절 기준 |
| ✅ 익절 / 🔴 손절 | 수익률·손익 금액·보유 시간 |
| 🔄 전략 조정 | 보유 중 AI가 익절/손절 기준 변경 시 |
| 📊 매매 평가 | 매도 후 AI 성과 평가 + 다음 제안 TP/SL + 교훈 |
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
| `GET /api/status` | JSON 상태 (자산·포지션·거래내역·성과) |
| `POST /api/liquidate` | 현재 포지션 시장가 청산 |

텔레그램에서 `/dashboard` 입력 시 접속 주소를 알려줍니다.

---

## 데이터베이스

### 모델
| 테이블 | 설명 |
|--------|------|
| `trades` | 개별 매수·매도 내역 |
| `positions` | 포지션 이력 (최대 1개 오픈) |
| `daily_reports` | 일별 성과 (23:55 자동 저장) |
| `strategy_evaluations` | 매매 후 AI 성과 평가 + 제안 파라미터 + 교훈 |

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
