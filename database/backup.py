"""SQLite 자동 백업 유틸리티 (EC2 전용)

- 매일 23:50 스케줄러가 호출
- DB 파일을 backup/ 디렉토리에 타임스탬프 복사
- DB_BACKUP_KEEP_DAYS 이상 된 파일 자동 삭제
- PostgreSQL 사용 시 이 모듈은 스킵 (외부 DB는 RDS 자동 백업 사용)
"""
import glob
import logging
import os
import shutil
from datetime import datetime, timedelta

from config import settings

logger = logging.getLogger(__name__)


def backup_sqlite() -> str | None:
    """SQLite DB 파일을 백업 디렉토리에 복사. 백업 경로 반환, 스킵 시 None"""

    if settings.DATABASE_URL:
        logger.debug("외부 DB 사용 중, SQLite 백업 스킵")
        return None

    src = settings.DB_PATH
    if not os.path.exists(src):
        logger.warning(f"백업 대상 DB 파일 없음: {src}")
        return None

    os.makedirs(settings.DB_BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_name = os.path.basename(src)
    dst = os.path.join(settings.DB_BACKUP_DIR, f"{db_name}.{timestamp}.bak")

    try:
        shutil.copy2(src, dst)
        logger.info(f"DB 백업 완료: {dst}")
        _cleanup_old_backups(db_name)
        return dst
    except OSError as e:
        logger.error(f"DB 백업 실패: {e}")
        return None


def _cleanup_old_backups(db_name: str) -> None:
    """보관 기간 초과 백업 파일 삭제"""
    pattern = os.path.join(settings.DB_BACKUP_DIR, f"{db_name}.*.bak")
    files = sorted(glob.glob(pattern))  # 이름순 = 날짜순

    keep = settings.DB_BACKUP_KEEP_DAYS
    if len(files) > keep:
        for old in files[:-keep]:
            try:
                os.remove(old)
                logger.info(f"오래된 백업 삭제: {old}")
            except OSError as e:
                logger.warning(f"백업 삭제 실패: {old} - {e}")
