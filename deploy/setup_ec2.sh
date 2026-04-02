#!/usr/bin/env bash
# =============================================================================
# pochaco EC2 초기 환경 설정 스크립트 (EC2 인스턴스에서 1회 실행)
#
# 사전 조건:
#   - Ubuntu 22.04 LTS
#   - EC2 인스턴스에 pochaco-ec2-profile IAM 프로파일 연결
#   - sudo 권한 보유
#
# 실행 방법 (EC2 SSH 접속 후):
#   curl -fsSL https://raw.githubusercontent.com/... 또는
#   bash /tmp/setup_ec2.sh
# =============================================================================
set -euo pipefail

APP_DIR="/opt/pochaco"
APP_USER="ubuntu"
REGION="${AWS_REGION:-ap-northeast-2}"

info()  { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[0;32m[OK]\033[0m    $*"; }
die()   { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

[[ $(uname -s) == "Linux" ]] || die "Linux 환경에서만 실행하세요."

# ─────────────────────────────────────────────────────────────────────────────
# 1. 시스템 패키지
# ─────────────────────────────────────────────────────────────────────────────
info "시스템 패키지 설치 중..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.12 python3.12-venv python3.12-dev \
    git curl tmux build-essential
ok "패키지 설치 완료"

# uv 설치 (Python 패키지 매니저)
if ! command -v uv &>/dev/null; then
    info "uv 설치 중..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version)"

# ─────────────────────────────────────────────────────────────────────────────
# 2. 앱 디렉터리 구조 생성
# ─────────────────────────────────────────────────────────────────────────────
info "디렉터리 생성: ${APP_DIR}"
sudo mkdir -p "${APP_DIR}"/{data,logs,backup}
sudo chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
ok "디렉터리 생성 완료"

# ─────────────────────────────────────────────────────────────────────────────
# 3. .env 파일 생성 (민감 정보 제외, Secrets Manager 사용)
# ─────────────────────────────────────────────────────────────────────────────
ENV_FILE="${APP_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    info ".env 파일 생성 중 (비민감 설정만 포함)..."
    cat > "$ENV_FILE" <<'ENVEOF'
# ============================================================
# pochaco EC2 설정 파일 — API 키는 Secrets Manager에 저장됨
# ============================================================

# AWS Secrets Manager (자동 주입)
AWS_SECRET_NAME=pochaco/production
AWS_REGION=ap-northeast-2

# 실행 모드 (EC2 서비스는 반드시 true)
HEADLESS=true

# LLM 공급자
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-4o
ANTHROPIC_MODEL=claude-opus-4-6

# 텔레그램 봇
TELEGRAM_ENABLED=true

# 웹 대시보드
DASHBOARD_ENABLED=true
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080

# 거래 설정
MIN_ORDER_KRW=5000
POSITION_CHECK_INTERVAL=10

# 데이터베이스
DB_PATH=/opt/pochaco/data/pochaco.db
DB_BACKUP_DIR=/opt/pochaco/backup
DB_BACKUP_KEEP_DAYS=7

# 로깅
LOG_LEVEL=INFO
LOG_FILE=/opt/pochaco/logs/pochaco.log
ENVEOF
    chmod 600 "$ENV_FILE"
    ok ".env 생성 완료: ${ENV_FILE}"
else
    ok ".env 이미 존재, 스킵: ${ENV_FILE}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. venv 및 의존성 설치 (코드가 APP_DIR에 있다고 가정)
# ─────────────────────────────────────────────────────────────────────────────
if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    info "Python 가상환경 생성 및 패키지 설치..."
    cd "${APP_DIR}"
    uv venv .venv --python python3.12
    uv pip install -r requirements.txt
    ok "패키지 설치 완료"
else
    info "코드가 아직 없음. deploy.sh로 먼저 코드를 배포하세요."
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5. systemd 서비스 등록
# ─────────────────────────────────────────────────────────────────────────────
SERVICE_SRC="${APP_DIR}/deploy/pochaco.service"
SERVICE_DEST="/etc/systemd/system/pochaco.service"

if [[ -f "$SERVICE_SRC" ]]; then
    info "systemd 서비스 등록 중..."
    sudo cp "$SERVICE_SRC" "$SERVICE_DEST"
    sudo systemctl daemon-reload
    sudo systemctl enable pochaco
    ok "서비스 등록 완료"
    echo ""
    echo "  서비스 시작: sudo systemctl start pochaco"
    echo "  로그 확인:   journalctl -u pochaco -f"
else
    warn "pochaco.service 파일 없음. 코드 배포 후 다시 실행하세요:"
    echo "  sudo cp ${APP_DIR}/deploy/pochaco.service /etc/systemd/system/"
    echo "  sudo systemctl daemon-reload && sudo systemctl enable --now pochaco"
fi

echo ""
ok "=== EC2 초기 설정 완료 ==="
