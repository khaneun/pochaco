import json
import os
from typing import Literal

from pydantic_settings import BaseSettings


def _load_aws_secrets() -> None:
    """AWS Secrets Manager에서 민감 정보를 환경변수로 주입.

    AWS_SECRET_NAME 환경변수가 설정된 경우에만 동작합니다.
    EC2 IAM 역할이 있으면 별도 자격증명 없이 자동 인증됩니다.
    이미 환경변수에 값이 있으면 덮어쓰지 않습니다(.env 우선 방지용 역할 분리).
    """
    secret_name = os.environ.get("AWS_SECRET_NAME")
    if not secret_name:
        return
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    try:
        import boto3
        sm = boto3.client("secretsmanager", region_name=region)
        resp = sm.get_secret_value(SecretId=secret_name)
        secrets: dict = json.loads(resp["SecretString"])
        injected = []
        for key, val in secrets.items():
            # 이미 환경변수에 있으면 유지 (로컬 오버라이드 허용)
            if key not in os.environ:
                os.environ[key] = str(val)
                injected.append(key)
        print(f"[config] Secrets Manager '{secret_name}' 로드 완료: {', '.join(injected)}")
    except ImportError:
        print("[config] boto3 미설치 — Secrets Manager 스킵, .env 사용")
    except Exception as e:
        print(f"[config] Secrets Manager 로드 실패 — .env 폴백: {e}")


_load_aws_secrets()


class Settings(BaseSettings):
    # ---------------------------------------------------------------- #
    #  빗썸 API                                                          #
    # ---------------------------------------------------------------- #
    BITHUMB_API_KEY: str = ""
    BITHUMB_SECRET_KEY: str = ""
    BITHUMB_BASE_URL: str = "https://api.bithumb.com"
    BITHUMB_WS_URL: str = "wss://pubwss.bithumb.com/pub/ws"

    # ---------------------------------------------------------------- #
    #  LLM 공급자 선택                                                   #
    #  LLM_PROVIDER: "anthropic" | "openai" | "gemini"                 #
    # ---------------------------------------------------------------- #
    LLM_PROVIDER: Literal["anthropic", "openai", "gemini"] = "anthropic"

    # Anthropic (Claude)
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-4-6"

    # OpenAI (ChatGPT)
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-1.5-pro"

    # ---------------------------------------------------------------- #
    #  거래 설정                                                          #
    # ---------------------------------------------------------------- #
    QUOTE_CURRENCY: str = "KRW"
    MIN_ORDER_KRW: int = 5_000           # 빗썸 최소 주문금액
    POSITION_CHECK_INTERVAL: int = 10    # 포지션 감시 주기(초)
    PORTFOLIO_SIZE: int = 8              # 포트폴리오당 코인 수

    # ---------------------------------------------------------------- #
    #  데이터베이스                                                       #
    #  DATABASE_URL을 설정하면 PostgreSQL 등 외부 DB 사용                 #
    #  미설정 시 SQLite(DB_PATH) 자동 사용                               #
    # ---------------------------------------------------------------- #
    DATABASE_URL: str = ""               # 예: postgresql+psycopg2://user:pw@host/db
    DB_PATH: str = "/opt/pochaco/data/pochaco.db"   # SQLite 절대경로 (EC2 권장)
    DB_POOL_SIZE: int = 5                # PostgreSQL 커넥션 풀 크기
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False                # True 시 SQL 쿼리 로깅

    # 자동 백업 (SQLite 전용)
    DB_BACKUP_DIR: str = "/opt/pochaco/backup"
    DB_BACKUP_KEEP_DAYS: int = 7         # 보관할 백업 파일 수(일)

    # ---------------------------------------------------------------- #
    #  실행 모드                                                         #
    # ---------------------------------------------------------------- #
    HEADLESS: bool = False              # True = 터미널 UI 없이 서비스로 실행

    # ---------------------------------------------------------------- #
    #  AWS                                                              #
    # ---------------------------------------------------------------- #
    AWS_SECRET_NAME: str = ""           # 예: pochaco/production
    AWS_REGION: str = "ap-northeast-2"

    # ---------------------------------------------------------------- #
    #  텔레그램 봇                                                       #
    # ---------------------------------------------------------------- #
    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""          # 허가된 chat_id (보안용)

    # ---------------------------------------------------------------- #
    #  웹 대시보드                                                       #
    # ---------------------------------------------------------------- #
    DASHBOARD_ENABLED: bool = True
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8080

    # ---------------------------------------------------------------- #
    #  로깅                                                              #
    # ---------------------------------------------------------------- #
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "/opt/pochaco/logs/pochaco.log"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
