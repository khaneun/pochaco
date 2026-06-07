"""백테스트 패키지 — 캔들 수집·시뮬레이터·지표 (next_plan.md 1.A).

현재 구현:
  - data_collector : 5분봉 누적 수집기 (EC2 systemd 상시 운영)
  - data_loader    : candles.db 읽기 헬퍼

예정:
  - engine / metrics / scenarios / cli
"""
