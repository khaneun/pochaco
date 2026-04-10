# Release Notes

---

## v4.0.0 (2026-04-11)

### 8코인 분산 포트폴리오 매매 시스템 전면 전환 + Agent 프롬프트 강화

#### 핵심 변경 — 포트폴리오 기반 매매
- **단일 코인 → 8코인 동시 포트폴리오**: 매 사이클마다 8개 코인을 동시에 매수·보유·매도
  - 포트폴리오 단위 랜덤 이름 부여 (예: "판다-07", "장미-42")
  - KRW 잔고를 8등분 (각 12.5%) 순차 매수, 일부 실패 시 성공 코인만으로 구성 (최소 3개)
- **포트폴리오 종합 P&L 기반 매도**:
  - 분할 손절: -1.0% → 전체 33% 매도, -1.5% → 잔여 50% 추가 매도
  - 최대 손절: -2.0% 하드캡 → 잔여 전량 청산 (절대 변경 불가)
  - 트레일링 익절: 종합 P&L이 TP 도달 → 고점 추적 → 하락 시 전량 청산

#### 신규 파일
- `strategy/portfolio_names.py` — 동물·색상·식물 풀에서 랜덤 포트폴리오 이름 생성기

#### DB 스키마 전면 개편
- **신규 테이블 `Portfolio`**: name, total_buy_krw, take_profit_pct, stop_loss_pct, is_open 등
- **`Position`**: portfolio_id FK 추가, TP/SL 컬럼 포트폴리오 레벨로 이동
- **`StrategyEvaluation`**: portfolio_id + portfolio_name + coins_summary(JSON) 포트폴리오 단위로 전환
- DB 마이그레이션: 기존 스키마 감지 시 `.v1_backup`으로 백업 후 클린 재생성

#### Repository 신규 메서드
- `open_portfolio()`, `get_open_portfolio()`, `get_portfolio_positions()`, `close_portfolio()`
- `update_portfolio_targets()`, `get_portfolio_history()`, `get_closed_portfolios()`

#### Agent 프롬프트 대폭 강화
- **매수 전문가**: 대형2+중형3+소형1 분산 철학, 4가지 금지 규칙, 섹터 분산 강조
- **매도 전문가**: 3단계 분할 손절 메커니즘 명시, 보유 시간별 5가지 가이드라인, -2% 하드캡 절대 원칙
- **포트폴리오 평가가**: 4점 평가 프레임워크 (승자/패자 비율 벤치마크, TP/SL 적정성, 보유 시간 효율)
- **시장 분석가**: 5단계 분석 절차, 절대 규칙 (BTC -3% → 필수 high risk, 7개+ 하락 → 필수 bearish)
- **자산 운용가**: 5가지 의사결정 기준, 연속 손절 시 비율 축소 규칙
- **총괄 평가가**: directive 작성 원칙 강화 (명령형·수치 포함·데이터 근거), 점수 부여 기준 명확화

#### 피드백 시스템 — 누적 요약 방식
- `BaseSpecialistAgent.update_feedback()`: 덮어쓰기 → 최근 3회 히스토리 누적
- 점수 추이 표시 (📈 상승 / 📉 하락 / ➡️ 유지) + 델타(+/-)
- 누적 패턴 섹션: 반복되는 강점·약점·지시사항 요약
- `restore_feedbacks_from_db()`: 재시작 시 최근 3회분을 시계열 순으로 누적 로드

#### TradingEngine 전면 재작성
- 메인 루프: `get_open_portfolio()` 기반 포트폴리오 감시
- `_PortfolioExitTracker`: phase, peak_pnl_pct, tier1_sold, tier2_sold 상태 추적
- `_select_and_buy_portfolio()`: 8코인 순차 매수 (0.5초 간격, 최소 3개 성공 필요)
- `_handle_monitoring()`: 3단계 낙폭별 분할 매도 상태 머신
- `_execute_portfolio_sell()`: 전량 매도 + 포트폴리오 종료 + 8개 심볼 쿨다운 일괄 등록

#### 기타 개선
- `CoinSelector._TOP_CANDIDATES`: 10 → 20 (8코인 선정을 위한 충분한 후보 확보)
- `StrategyOptimizer`: -2% 하드캡 반영, 포트폴리오 TP 범위 3.0~8.0%
- `config/settings.py`: `PORTFOLIO_SIZE: int = 8` 추가
- 대시보드·텔레그램·스케줄러: 포트폴리오 단위 상태·이력 표시로 전환

---

## v3.3.0 (2026-04-10)

### 특성 분석가(CoinProfileAnalyst) — 코인별 프로파일 누적 학습

#### 핵심 기능
- **코인 프로파일 파일 관리**: `data/coin_profiles/{SYMBOL}.md` 에 코인별 특성을 영구 저장
- **매도 후 자동 업데이트**: 매매가 완료될 때마다 LLM이 프로파일을 생성·갱신
  - 프로파일 구성: 가격 특성 / 매매 이력(최신 10건) / 전략 권고 / 주의사항
- **매수 전 자동 주입**: 매수 전문가(BuyStrategist)에게 후보 코인의 과거 프로파일을 컨텍스트로 제공
  - 과거 보유 이력이 있는 코인은 프로파일이 강조 표시되어 의사결정에 반영
- **투자 반복 누적**: 매매가 쌓일수록 코인별 최적 전략이 자동으로 개선

#### AgentCoordinator 확장
- 7번째 Agent로 통합 (`coin_profile_analyst`)
- `get_coin_profile(symbol)` / `list_coin_profiles()` 공개 메서드 추가

#### 전문가 실적표 UI
- 특성 분석가 카드 신설
- 관리 중인 코인 태그 목록 표시 (매매가 기록된 코인들)
- [📝 프롬프트] / [💬 대화] 버튼으로 분석가와 직접 인터랙션 가능

---

## v3.2.0 (2026-04-09)

### 종합 대시보드 — AI 성과 평가 개선 + 수동 청산 이력 섹션

#### AI 성과 평가 테이블 개편
- **컬럼 재구성**: 보유 시간 · 설정 TP/SL · 제안 TP/SL → 수익률 클릭 팝업으로 이동
- **수익금액 컬럼 추가**: 수익률 옆에 원화 수익금액 표시 (포지션 매수금액 기반 추정)
- **수익률 클릭 상세 팝업**: 코인/결과·수익률·보유시간·설정TP/SL·제안TP/SL·AI 평가·lesson 표시

#### 일별 성과 카드 제거
- 7일 일별 성과 카드 삭제 — 현재 DB 집계 방식의 한계로 정확도 낮음

#### 수동 청산 이력 섹션 신설
- 대시보드 "포지션 청산" 버튼으로 청산한 이력을 별도 테이블로 표시
- 컬럼: 시간 · 코인 · 수익률 · 수익금액 · 보유 시간
- 청산 노트에 `(pnl_pct%, pnl_krw원, held_min분)` 포맷으로 파싱 데이터 보존

---

## v3.1.0 (2026-04-09)

### 전문가 실적표 — 프롬프트 확인·수정 및 Agent 직접 대화 기능

#### 전문가 실적표 (/experts) UI 개선
- **[📝 프롬프트] 버튼**: 각 전문가 카드에 버튼 추가 → 클릭 시 모달 팝업
  - 기본 역할 프롬프트를 textarea에서 직접 수정 후 저장 가능
  - MetaEvaluator 주입 피드백 프롬프트도 읽기 전용으로 확인 가능
- **[💬 대화] 버튼**: 해당 전문가 Agent와 직접 채팅 인터페이스
  - 역할 특화 시스템 프롬프트 기반으로 전문적 답변 제공
  - 대화 이력 유지 (멀티턴 지원)
  - Enter: 전송 / Shift+Enter: 줄바꿈

#### 신규 API 엔드포인트
- `GET /api/agent/prompt?role=...` — 특정 Agent의 base_prompt + feedback_prompt 반환
- `POST /api/agent/chat` — `{role, message, history}` → Agent LLM 응답 반환 (멀티턴)
- `POST /api/agent/update_prompt` — `{role, new_prompt}` → base_prompt 즉시 업데이트

#### LLM 레이어 개선 (core/llm_provider.py)
- `BaseLLMProvider.chat_with_system(system, messages, max_tokens)` 추상 메서드 추가
- Anthropic: `system` 파라미터 + `messages` 배열로 멀티턴 지원
- OpenAI: system role 메시지 prepend 방식
- Gemini: 시스템 지침 + 대화 이력 단일 텍스트 구성

#### Agent 기반 클래스 확장 (strategy/agents/base_agent.py)
- `base_prompt` / `feedback_prompt` 프로퍼티 노출
- `update_base_prompt(new_prompt)` — 기본 프롬프트 런타임 수정
- `chat(message, history)` — 대화 모드 (역할 프롬프트 기반 자유 대화)

---

## v3.0.0 (2026-04-09)

### 6개 전문가 Agent 시스템 도입 — 멀티 에이전트 아키텍처

#### 아키텍처 전환: 단일 Agent → 6개 전문가 분업
- **기존**: `TradingAgent` 1개가 코인 선정·전략 조정·성과 평가를 모두 담당
- **변경**: 역할별 전문 Agent 6개 + `AgentCoordinator` 오케스트레이터 구조
  | Agent | 역할 |
  |-------|------|
  | MarketAnalyst (시장 분석가) | 시장 전반 흐름·리스크·투자 적합성 판단 |
  | AssetManager (자산 운용가) | 시장 상태 기반 투자 비율 결정 (0.3~0.95) |
  | BuyStrategist (매수 전문가) | 코인 선정 + TP/SL 초기값 결정 |
  | SellStrategist (매도 전문가) | 보유 중 TP/SL 동적 조정 |
  | PortfolioEvaluator (포트폴리오 평가가) | 매매 후 성과 분석·파라미터 제안 |
  | MetaEvaluator (총괄 평가가) | 6시간 주기 전체 전문가 평가·피드백·점수화 |

#### 매매 파이프라인 변경 (strategy/agent_coordinator.py)
- **코인 선정**: MarketAnalyst → AssetManager → BuyStrategist 3단계 파이프라인
  - 시장 분석 결과가 자산 배분에, 자산 배분이 매수 전략에 반영
  - AssetManager가 `should_invest=False` 판단 시 매수 보류 가능
- **투자 비율**: 기존 고정 95% → AssetManager가 시장 상태에 따라 30~95% 동적 결정
- **전략 조정**: SellStrategist 전담 (기존 프롬프트 구조 유지 + 피드백 반영)
- **성과 평가**: PortfolioEvaluator 전담 (기존 프롬프트 구조 유지 + 피드백 반영)

#### 총괄 평가 시스템 (strategy/agents/meta_evaluator.py · scheduler/jobs.py)
- 6시간 주기(0, 6, 12, 18시) 5개 전문가를 종합 평가
- 각 Agent에 0~100 점수 + 강점/약점/개선 지시(directive) 부여
- **잘하는 부분**: 구체적 칭찬 + 강화 방향 제시 (priority: reinforce)
- **못하는 부분**: 강한 피드백 + 즉시 개선 지시 (priority: critical)
- directive가 각 Agent 프롬프트에 동적 주입 → 지속 개선 루프
- 재시작 시 DB에서 최신 피드백 자동 복원

#### DB 스키마 확장 (database/models.py)
- `agent_scores` 테이블: 전문가별 점수·피드백·트렌드 기록
- `agent_decision_logs` 테이블: 의사결정 기록 (MetaEvaluator 입력용)
- 기존 테이블 변경 없음 (하위호환)

#### 웹 대시보드 개편 (dashboard/web_server.py)
- **상단 메뉴**: 종합 대시보드 / 전문가 실적표 탭 네비게이션
- **종합 대시보드**: Agent 점수 요약 카드 추가 (5개 전문가 점수 배지)
- **전문가 실적표** (`/experts`): 각 전문가별 점수 카드 + 트렌드 차트 + 피드백 내역
- **JSON API** (`/api/experts`): 전문가 데이터 엔드포인트 추가

#### 의존성 주입 변경 (main.py)
- 6개 전문가 Agent 생성 → AgentCoordinator로 조립 → TradingEngine에 주입
- LLM 공급자 1개를 6개 Agent가 공유 (토큰 효율)
- scheduler·web dashboard에도 coordinator 주입

---

## v2.5.0 (2026-04-09)

### 일별 성과 정확화 — 총자산 기반 손익 + 수수료 표시

#### 손익 계산 버그 수정 (trading_engine.py · scheduler/jobs.py)
- **기존**: `starting_krw` / `ending_krw`를 KRW 잔고만으로 기록 → 코인 보유 중 시작·종료 시 손익 누락
- **변경**: KRW + 오픈 포지션 코인 평가액(현재가 × 수량)을 합산한 **총자산** 기준으로 기록
- 결과: 7일 손익 합계가 `현재 총자산 - 최초 투하 자본`과 정확히 일치

#### 수수료 집계 추가 (scheduler/jobs.py · database/models.py)
- `DailyReport` 테이블에 `total_fee` 컬럼 추가 (기동 시 자동 마이그레이션)
- 매일 23:55 리포트 기록 시 당일 거래 수수료 합산
  - `Trade.fee > 0`이면 실제값 사용, 0이면 `krw_amount × 0.25%` 추정
- `repository.upsert_daily_report`에 `total_fee` 파라미터 추가

#### 웹 대시보드 일별 성과 테이블 개선 (dashboard/web_server.py)
- **기존**: 날짜 / 수익률 / 손익(원) / 승
- **변경**: 날짜 / 시작자산 / 종료자산 / 손익(원) / 수익률 / 수수료 / 승·패
- 7일 합계 행 추가 (손익 합계 + 수수료 합계)

#### 터미널 대시보드 AI 일별 보고서 개선 (dashboard/terminal_ui.py)
- 기존 "일 수익률(%)만" → **손익(원)** 컬럼과 **수익률(%)** 컬럼 분리 표시

---

## v2.4.0 (2026-04-09)

### 타이트 손절 + 단계별 트레일링 익절 전략 (10%+ 목표)

#### 전략 철학 전환
- **기존**: 넓은 손절(-1.5~-4.5%) + 중간 익절(2~6%)
- **변경**: 타이트 손절(-0.5~-2.5%) + 크게 트레일링 익절(5%+ 진입 → 10%+ 목표)
- 빠른 손실 인지·탈출로 드로다운 최소화, 수익 날 때 충분히 달리는 비대칭 전략

#### 트레일링 익절 구간별 드랍포인트 재설계 (`strategy/trading_engine.py`)
- **기존**: 오버슈트 기반 0.3~1.0% 단순 오프셋, 타임아웃 10분
- **변경**: 현재 수익 구간별 단계적 오프셋, 타임아웃 30분
  | 수익 구간 | 드랍포인트 오프셋 |
  |-----------|-----------------|
  | 5 ~ 7%   | 0.8% |
  | 7 ~ 10%  | 1.2% |
  | 10 ~ 15% | 1.8% |
  | 15%+     | 2.5% |
- 모멘텀 상실 안전망: TP의 50% → **20%** 수준으로 완화 (10%+ 트레일링 중 조기 탈출 방지)

#### 손절 파라미터 타이트화 (`strategy/strategy_optimizer.py`)
- `StrategyParams` 기본값 변경:
  - target_sl: -2.5% → **-1.5%**
  - sl_clamp_min: -4.5% → **-2.5%**
  - sl_clamp_max: -1.5% → **-0.8%**
- 연속 손절 시에도 SL2 **-2.5% 이상 넓히지 않음** (타이트 원칙 유지)
- LLM 프롬프트 지시: SL2 범위 -0.5~-2.5%

#### 익절 파라미터 상향 (`strategy/strategy_optimizer.py`)
- `StrategyParams` 기본값 변경:
  - target_tp: 3.5% → **5.0%**
  - tp_clamp_min: 2.0% → **4.0%**
  - tp_clamp_max: 6.0% → **10.0%**
- LLM 프롬프트: TP 범위 2~6% → **4~10%**, 상한 12.0%까지 허용

#### AI Agent 프롬프트 범위 갱신 (`strategy/ai_agent.py`)
- `select_coin()` 기본 TP 범위: 2~6% → **4~10%**, SL1: -1~-3% → **-0.5~-1.5%**
- `evaluate_trade()` 제안 TP: 2~7% → **4~12%**, 제안 SL1: -1~-3.5% → **-0.5~-1.5%**
- `should_adjust_strategy()` 안전장치: SL1 상한 -0.8% → **-0.5%**

---

## v2.3.0 (2026-04-07)

### 2단계 손절 시스템 도입 + 익절 범위 상향

#### 핵심 변경: 2단계 손절 (trading_engine.py · ai_agent.py)
- **기존**: 단일 손절선(-2~-6%) 도달 시 전량 매도 → 실효 손실 최대 5~6%
- **변경**: 1차 손절 도달 시 **50%만 매도**, 2차 도달 시 **나머지 전량 매도**
  - 1차 손절(`stop_loss_1st_pct`, 기준 -2% 전후): AI 결정 / 도달 시 즉시 절반 청산
  - 2차 손절(`stop_loss_pct`, 기준 -2.5% 전후): AI 결정 / 도달 시 잔여 전량 청산
  - **실효 최대 손실 ≈ -2.25%** (기존 대비 절반 수준으로 감소)
- `OBSERVING_SL` 상태(가짜 하락 관찰) 제거 — 2단계 구조가 노이즈 필터 역할 대체

#### 익절 범위 상향 (ai_agent.py · strategy_optimizer.py)
- 기존 1.0~3.5% → **2.0~6.0%** (손실 위험 감소로 더 큰 목표 추구 가능)
- SL1·SL2·TP 포인트 모두 AI Agent가 시장 상황 분석 후 결정

#### DB 스키마 확장 (database/models.py · repository.py)
- `positions.stop_loss_1st_pct` 컬럼 추가 (1차 손절%)
- `strategy_evaluations.original_sl_1st_pct` 컬럼 추가
- 기동 시 자동 마이그레이션 (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`)

#### AI 프롬프트 개선
- `select_coin()`: SL1/SL2 이중 손절 JSON 반환, 실효 R:R 검증 로직 추가
- `evaluate_trade()`: 1차 손절 실행 여부 컨텍스트 포함, SL1/SL2 독립 제안
- `should_adjust_strategy()`: 2단계 손절 인식 후 각각 재조정

#### 알림 개선 (telegram_bot.py)
- 매수 알림: `1차SL -2.0% (50%) → 2차SL -2.5% (전량)` 형식으로 표시

---

## v2.2.0 (2026-04-05)

### 익절·손절·수동 청산 후 동일 종목 재매수 방지 쿨다운

#### 쿨다운 레지스트리 (`strategy/cooldown.py` 신규)
- 프로세스 내 공유 싱글턴으로 TradingEngine·WebDashboard 두 스레드가 동일 레지스트리를 사용
- 매도 유형별 쿨다운:
  - **익절 후 30분**: 익절 후 되돌림 구간 재진입 방지
  - **손절 후 10분**: 직전 하락 패턴 즉시 재진입 방지
  - **수동 청산(대시보드 버튼) 후 60분**: 사용자 개입 의도 존중, 가장 보수적으로 적용
- Thread-safe: `threading.Lock` 보호, 만료된 항목 자동 정리

#### CoinSelector 하드 필터 (`strategy/coin_selector.py`)
- `cooldown_symbols` 파라미터 추가
- AI에게 후보 목록을 전달하기 전에 쿨다운 종목을 완전히 제거 → AI가 선택 자체 불가

#### AI 프롬프트 강화 (`strategy/ai_agent.py`)
- 과거 거래 목록에 `【익절 직후 — 반드시 제외】` / `【손절 직후 — 가급적 제외】` 레이블 추가
- 코인 선정 기준에 "최근 익절 종목 재선정 절대 금지" 항목 추가 (모멘텀 소진·되돌림 근거 명시)

---

## v2.1.0 (2026-04-05)

### 피드백 루프 구조 개선 + 자산 집계 버그 수정 + 대시보드 청산 기능

#### 피드백 루프 구조 개선
- **repository clamp 현행화**: 구식 범위(TP 2~8%, SL -3~-1%) → 현 전략(TP 0.8~4%, SL -7~-1.5%)
- **suggested 가중평균**: 최근 AI 제안에 2배 가중치 → 최신 피드백이 빠르게 반영
- **StrategyOptimizer ↔ repository merge**: 일방적 덮어쓰기 → 두 소스 중 넓은 범위 채택
- **연속 손절 시 시장 vs 전략 구분**: 평균 손실 크기로 판단
  - 소폭 연속 손절(avg < -3%): 손절이 좁아서 찍힌 것 → 손절 확대
  - 대폭 연속 손절(avg > -3%): 시장 악화 → 익절 낮춰 빠른 탈출
- **초기 데이터 부재 시**: 더미 count=3 삽입 → optimizer 기본값으로 clamp 직접 구성
- **clamp 적용 조건**: count >= 3 → clamp 존재 여부 기반 (첫 매매부터 optimizer 범위 적용)

#### 자산 집계 버그 수정 (`dashboard/web_server.py`)
- position 코인(CYS)이 total에 2번 합산되던 이중 계산 버그 수정
  - 1차: `total = krw + pos_value` (position 블록)
  - 2차: `total += kv` (holdings 루프)
  - 수정: holdings에서 position 코인 제외

#### 대시보드 기능 추가
- **🔥 포지션 청산 버튼**: 현재 포지션 카드에 표시, 해당 코인만 시장가 매도
- **POST /api/liquidate**: 오픈 포지션 시장가 매도 + DB 포지션 종료
- **릴리즈 버전 표기**: 헤더에 `release-note.md` 최신 버전 자동 표시
- **보유 코인 표시 개선**: 1000원 미만 미표기, 포지션 코인 중복 제거, `평가금액 (개수)` 형식

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
