#!/usr/bin/env bash
# =============================================================================
# pochaco 코드 배포 스크립트 (로컬에서 실행)
#
# 사용법:
#   EC2_HOST=ubuntu@<퍼블릭IP>  bash deploy/deploy.sh
#   EC2_HOST=ubuntu@13.125.x.x bash deploy/deploy.sh
#
# 환경변수:
#   EC2_HOST  — SSH 접속 주소 (필수)
#   SSH_KEY   — PEM 키 경로 (기본: ~/.ssh/id_rsa)
#   APP_DIR   — EC2 앱 디렉터리 (기본: /opt/pochaco)
# =============================================================================
set -euo pipefail

EC2_HOST="${EC2_HOST:?'EC2_HOST 환경변수를 설정하세요. 예: EC2_HOST=ubuntu@1.2.3.4'}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_DIR="${APP_DIR:-/opt/pochaco}"
SSH_OPTS="-i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10"

info() { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[0;32m[OK]\033[0m    $*"; }

# ─────────────────────────────────────────────────────────────────────────────
# 1. rsync — 코드 동기화 (.env, venv, 데이터, 로그 제외)
# ─────────────────────────────────────────────────────────────────────────────
info "코드 동기화 중 → ${EC2_HOST}:${APP_DIR}"
rsync -avz --delete \
    -e "ssh ${SSH_OPTS}" \
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
    ./ "${EC2_HOST}:${APP_DIR}/"
ok "코드 동기화 완료"

# ─────────────────────────────────────────────────────────────────────────────
# 2. EC2 — 의존성 설치 및 서비스 재시작
# ─────────────────────────────────────────────────────────────────────────────
info "EC2 패키지 업데이트 및 서비스 재시작 중..."
ssh ${SSH_OPTS} "${EC2_HOST}" bash <<REMOTE
set -euo pipefail
cd "${APP_DIR}"

# 가상환경이 없으면 생성
if [[ ! -d ".venv" ]]; then
    echo "[EC2] venv 생성 중..."
    uv venv .venv --python python3.12
fi

# 의존성 설치/업데이트
echo "[EC2] 패키지 설치 중..."
uv pip install -r requirements.txt -q

# systemd 서비스 파일 갱신
if [[ -f "deploy/pochaco.service" ]]; then
    sudo cp deploy/pochaco.service /etc/systemd/system/pochaco.service
    sudo systemctl daemon-reload
fi

# 서비스 재시작
if sudo systemctl is-enabled pochaco &>/dev/null; then
    sudo systemctl restart pochaco
    sleep 2
    STATUS=\$(sudo systemctl is-active pochaco)
    echo "[EC2] 서비스 상태: \${STATUS}"
else
    sudo systemctl enable --now pochaco
    echo "[EC2] 서비스 시작됨"
fi
REMOTE

ok "=== 배포 완료 ==="
echo ""
echo "  로그 확인:    ssh ${SSH_OPTS} ${EC2_HOST} 'journalctl -u pochaco -f'"
echo "  서비스 상태:  ssh ${SSH_OPTS} ${EC2_HOST} 'systemctl status pochaco'"
