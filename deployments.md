# Deployments — 배포 가이드

pochaco를 AWS EC2에 배포하는 표준 절차입니다.
**이 문서의 흐름대로 따라하면 실수 없이 배포할 수 있습니다.**

---

## 인프라 정보

| 항목 | 값 |
|------|-----|
| 인스턴스 이름 | `pochaco-trader` |
| 인스턴스 ID | `i-00d817b47d8c2672a` |
| 퍼블릭 IP | `3.34.125.179` (Elastic IP 아닌 경우 재시작 시 변경될 수 있음) |
| 리전 | `ap-northeast-2` (서울) |
| AMI | Amazon Linux 2023 |
| SSH 유저 | `ec2-user` |
| SSH 키 | `~/kitty-key.pem` |
| 앱 디렉터리 | `/opt/pochaco` |
| DB 경로 | `/opt/pochaco/data/pochaco.db` |
| 로그 경로 | `/opt/pochaco/logs/pochaco.log` |
| systemd 서비스 | `pochaco` |
| 웹 대시보드 | `http://3.34.125.179:8080` |

---

## 사전 조건

- `~/kitty-key.pem` — EC2 SSH 키 (`chmod 400` 필수)
- AWS CLI 인증 완료 (`aws sts get-caller-identity`로 확인)
- 로컬 git remote: `https://github.com/khaneun/pochaco.git`

---

## 표준 배포 절차 (코드 업데이트 시)

### 0. 현재 퍼블릭 IP 확인 (재시작 후 IP가 바뀐 경우)

```bash
EC2_IP=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=pochaco-trader" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
echo $EC2_IP
```

### 1. 개발 완료 후 git commit & push

```bash
cd ~/project/pochaco

git add <변경된 파일들>
git commit -m "feat: ..."
git push origin main
```

### 2. EC2 서비스 중지

```bash
SSH="ssh -i ~/kitty-key.pem -o StrictHostKeyChecking=no ec2-user@${EC2_IP}"

$SSH "sudo systemctl stop pochaco && echo stopped"

# 완전 종료 확인
$SSH "sudo systemctl is-active pochaco || echo confirmed: inactive"
```

> **주의**: 포지션을 보유 중인 경우에도 `systemctl stop` 은 SIGTERM → TradingEngine.stop() → graceful 종료 흐름입니다.
> 서비스가 멈추면 포지션 감시가 중단되므로, **배포는 빠르게** 마쳐야 합니다.

### 3. 코드 rsync 동기화

```bash
rsync -avz --delete \
  -e "ssh -i ~/kitty-key.pem -o StrictHostKeyChecking=no" \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='.git/' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  /home/rudolph/project/pochaco/ "ec2-user@${EC2_IP}:/opt/pochaco/"
```

### 4. EC2에서 의존성 설치

```bash
$SSH "cd /opt/pochaco && uv pip install -r requirements.txt -q && echo done"
```

### 5. systemd 서비스 파일 갱신 및 시작

```bash
$SSH bash <<'REMOTE'
cd /opt/pochaco
sudo cp deploy/pochaco.service /etc/systemd/system/pochaco.service
sudo systemctl daemon-reload
sudo systemctl start pochaco
sleep 3
sudo systemctl is-active pochaco
REMOTE
```

### 6. 기동 로그 확인

```bash
$SSH "journalctl -u pochaco --since '1 minute ago' --no-pager | tail -30"
```

정상 기동 시 아래 메시지들이 로그에 나타납니다:
```
[INFO] strategy.trading_engine: === TradingEngine 시작 ===
[INFO] strategy.trading_engine: [현금화 완료] ...
[INFO] strategy.ai_agent: AI Agent (...): 코인 선정 분석 중...
[INFO] strategy.trading_engine: [매수 완료] ...
```

---

## 한 번에 실행하는 스크립트 (deploy.sh 사용)

기존 `deploy/deploy.sh`를 쓸 경우:

```bash
cd ~/project/pochaco

# 서비스 먼저 중지
SSH="ssh -i ~/kitty-key.pem -o StrictHostKeyChecking=no ec2-user@${EC2_IP}"
$SSH "sudo systemctl stop pochaco"

# 배포
EC2_HOST="ec2-user@${EC2_IP}" SSH_KEY="~/kitty-key.pem" bash deploy/deploy.sh
```

> `deploy.sh`는 내부적으로 rsync → pip install → systemctl restart 를 수행합니다.
> 단, **서비스 중지를 먼저 수동으로** 해야 합니다 (스크립트는 restart만 함).

---

## 서비스 상태 확인 명령어

```bash
SSH="ssh -i ~/kitty-key.pem -o StrictHostKeyChecking=no ec2-user@${EC2_IP}"

# 서비스 상태
$SSH "sudo systemctl status pochaco --no-pager"

# 실시간 로그 (Ctrl+C로 종료)
$SSH "journalctl -u pochaco -f"

# 최근 50줄 로그
$SSH "journalctl -u pochaco --no-pager -n 50"
```

---

## DB / 데이터 보호 주의사항

- `rsync --exclude='data/'` 옵션으로 DB 파일은 절대 덮어쓰지 않음
- `--exclude='logs/'` 로 로그 파일도 보존
- DB 백업은 매일 23:50 자동 실행 → `/opt/pochaco/backup/`

---

## 배포 이력

| 날짜 | 버전 | 내용 |
|------|------|------|
| 2026-04-03 | v1.7.0 | 자기 개선형 AI Agent, 성과 기반 전략 피드백 루프, 대시보드 평가 패널 |
| 2026-04-03 | v1.6.0 | 빗썸 API v2 JWT 인증 마이그레이션 |
| 2026-04-03 | v1.5.0 | 안정성 개선, 텔레그램 /log 명령 |
| 2026-04-03 | v1.4.0 | AWS EC2 + Secrets Manager 배포 체계 구축 |
