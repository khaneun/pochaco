#!/usr/bin/env bash
# =============================================================================
# pochaco AWS 인프라 초기 설정 스크립트 (로컬에서 1회 실행)
#
# 수행 작업:
#   1. Secrets Manager에 API 키 등록
#   2. EC2용 IAM 역할 + 인스턴스 프로파일 생성
#   3. EC2 인스턴스에 IAM 프로파일 연결 (인스턴스 ID 필요 시)
#
# 사전 조건:
#   - AWS CLI 설치 및 인증 완료 (aws configure)
#   - .env 파일에 실제 값이 입력되어 있어야 함
# =============================================================================
set -euo pipefail

# ── 설정 ─────────────────────────────────────────────────────────────────────
REGION="${AWS_REGION:-ap-northeast-2}"
SECRET_NAME="pochaco/production"
ROLE_NAME="pochaco-ec2-role"
POLICY_NAME="pochaco-secretsmanager-policy"
PROFILE_NAME="pochaco-ec2-profile"

# ── 색상 출력 ─────────────────────────────────────────────────────────────────
info()  { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[0;32m[OK]\033[0m    $*"; }
warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; exit 1; }

# ── .env 파싱 헬퍼 ───────────────────────────────────────────────────────────
env_val() {
    local key="$1"
    grep -E "^${key}=" .env 2>/dev/null \
        | head -1 \
        | cut -d'=' -f2- \
        | sed "s/^['\"]//;s/['\"]$//" \
        | tr -d '\r'
}

# ── 전제 조건 확인 ────────────────────────────────────────────────────────────
[[ -f ".env" ]] || die ".env 파일이 없습니다. 프로젝트 루트에서 실행하세요."
command -v aws &>/dev/null || die "AWS CLI가 설치되어 있지 않습니다."
aws sts get-caller-identity --region "$REGION" &>/dev/null \
    || die "AWS 인증 실패. 'aws configure' 또는 환경변수를 확인하세요."

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
info "AWS 계정: ${ACCOUNT_ID} / 리전: ${REGION}"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Secrets Manager — 시크릿 생성/업데이트
# ─────────────────────────────────────────────────────────────────────────────
info "Secrets Manager 시크릿 구성 중: ${SECRET_NAME}"

# .env에서 민감 값 읽기
BITHUMB_API_KEY=$(env_val "BITHUMB_API_KEY")
BITHUMB_SECRET_KEY=$(env_val "BITHUMB_SECRET_KEY")
ANTHROPIC_API_KEY=$(env_val "ANTHROPIC_API_KEY")
OPENAI_API_KEY=$(env_val "OPENAI_API_KEY")
GEMINI_API_KEY=$(env_val "GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN=$(env_val "TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=$(env_val "TELEGRAM_CHAT_ID")

# 빗썸 키 필수 확인
[[ -n "$BITHUMB_API_KEY" ]]    || die "BITHUMB_API_KEY가 .env에 없습니다."
[[ -n "$BITHUMB_SECRET_KEY" ]] || die "BITHUMB_SECRET_KEY가 .env에 없습니다."

# Python으로 .env 직접 파싱하여 JSON 직렬화 (특수문자 안전 처리)
SECRET_JSON=$(python3 - .env <<'PYEOF'
import json, sys

KEYS = [
    "BITHUMB_API_KEY", "BITHUMB_SECRET_KEY",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]

env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
vals = {}
with open(env_file) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'\"")
        if k in KEYS:
            vals[k] = v

print(json.dumps(vals))
PYEOF
)

if aws secretsmanager describe-secret \
        --secret-id "$SECRET_NAME" \
        --region "$REGION" &>/dev/null; then
    aws secretsmanager update-secret \
        --secret-id "$SECRET_NAME" \
        --secret-string "$SECRET_JSON" \
        --region "$REGION" > /dev/null
    ok "시크릿 업데이트: ${SECRET_NAME}"
else
    aws secretsmanager create-secret \
        --name "$SECRET_NAME" \
        --description "pochaco API keys" \
        --secret-string "$SECRET_JSON" \
        --region "$REGION" > /dev/null
    ok "시크릿 생성: ${SECRET_NAME}"
fi

SECRET_ARN="arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:${SECRET_NAME}"

# ─────────────────────────────────────────────────────────────────────────────
# 2. IAM — EC2용 역할 + 정책 + 인스턴스 프로파일
# ─────────────────────────────────────────────────────────────────────────────
info "IAM 역할 구성 중: ${ROLE_NAME}"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ec2.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}'

SECRET_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowPochacoSecrets",
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:pochaco/*"
  }]
}
EOF
)

# IAM 역할 생성 (이미 있으면 스킵)
if ! aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "pochaco EC2 instance role" > /dev/null
    ok "IAM 역할 생성: ${ROLE_NAME}"
else
    ok "IAM 역할 이미 존재: ${ROLE_NAME}"
fi

# 인라인 정책 등록 (항상 최신 상태 유지)
aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" \
    --policy-document "$SECRET_POLICY"
ok "IAM 정책 적용: ${POLICY_NAME}"

# 인스턴스 프로파일 생성
if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" &>/dev/null; then
    aws iam create-instance-profile \
        --instance-profile-name "$PROFILE_NAME" > /dev/null
    aws iam add-role-to-instance-profile \
        --instance-profile-name "$PROFILE_NAME" \
        --role-name "$ROLE_NAME"
    ok "인스턴스 프로파일 생성: ${PROFILE_NAME}"
else
    ok "인스턴스 프로파일 이미 존재: ${PROFILE_NAME}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3. EC2 인스턴스에 IAM 프로파일 연결 (선택)
# ─────────────────────────────────────────────────────────────────────────────
if [[ -n "${EC2_INSTANCE_ID:-}" ]]; then
    info "EC2 인스턴스 ${EC2_INSTANCE_ID}에 IAM 프로파일 연결 중..."
    ASSOC=$(aws ec2 describe-iam-instance-profile-associations \
        --filters "Name=instance-id,Values=${EC2_INSTANCE_ID}" \
        --query "IamInstanceProfileAssociations[0].AssociationId" \
        --output text --region "$REGION")

    if [[ "$ASSOC" != "None" && -n "$ASSOC" ]]; then
        aws ec2 replace-iam-instance-profile-association \
            --association-id "$ASSOC" \
            --iam-instance-profile "Name=${PROFILE_NAME}" \
            --region "$REGION" > /dev/null
        ok "IAM 프로파일 교체 완료"
    else
        aws ec2 associate-iam-instance-profile \
            --instance-id "$EC2_INSTANCE_ID" \
            --iam-instance-profile "Name=${PROFILE_NAME}" \
            --region "$REGION" > /dev/null
        ok "IAM 프로파일 연결 완료"
    fi
else
    warn "EC2_INSTANCE_ID 미지정 — 나중에 아래 명령으로 연결하세요:"
    echo "  EC2_INSTANCE_ID=i-xxxx bash deploy/create_secrets.sh"
    echo ""
    echo "  또는 EC2 콘솔 → 인스턴스 → 작업 → 보안 → IAM 역할 수정 → ${PROFILE_NAME}"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
ok "=== 완료 ==="
echo "  시크릿 ARN : ${SECRET_ARN}"
echo "  IAM 역할   : ${ROLE_NAME}"
echo "  다음 단계  : EC2에서 deploy/setup_ec2.sh 실행"
