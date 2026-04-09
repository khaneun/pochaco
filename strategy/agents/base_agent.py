"""전문가 Agent 공통 기반"""
import json
import logging
from abc import ABC, abstractmethod

from core.llm_provider import BaseLLMProvider, get_llm_provider

logger = logging.getLogger(__name__)


class BaseSpecialistAgent(ABC):
    """모든 전문가 Agent의 공통 기반 클래스

    LLM 호출, 피드백 관리, JSON 파싱 등 공통 기능을 제공합니다.
    서브클래스는 ROLE_NAME, DISPLAY_NAME을 정의하고 execute()를 구현합니다.
    """

    ROLE_NAME: str = ""
    DISPLAY_NAME: str = ""

    def __init__(self, llm: BaseLLMProvider | None = None):
        self._llm = llm or get_llm_provider()
        self._base_prompt: str = ""      # 서브클래스에서 설정
        self._feedback_prompt: str = ""  # MetaEvaluator가 주입
        self._score: float = 50.0

    @property
    def role_name(self) -> str:
        return self.ROLE_NAME

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @property
    def score(self) -> float:
        return self._score

    def update_feedback(self, feedback: str, score: float) -> None:
        """총괄 평가가의 피드백을 프롬프트에 반영"""
        self._feedback_prompt = feedback
        self._score = max(0.0, min(100.0, score))

    def _build_system_context(self) -> str:
        """기본 프롬프트 + 피드백 프롬프트 조합"""
        parts = [self._base_prompt]
        if self._feedback_prompt:
            parts.append(
                f"\n\n[총괄 평가 피드백 — 반드시 반영하세요]\n{self._feedback_prompt}"
            )
        return "\n".join(parts)

    def _call_llm(self, task_prompt: str, max_tokens: int = 512) -> str:
        """시스템 컨텍스트 + 작업 프롬프트로 LLM 호출"""
        full_prompt = f"{self._build_system_context()}\n\n---\n\n{task_prompt}"
        return self._llm.chat(full_prompt, max_tokens=max_tokens)

    def _parse_json(self, raw: str) -> dict:
        """LLM 응답에서 JSON 추출 (마크다운 코드블록 제거)"""
        clean = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(clean)

    @abstractmethod
    def execute(self, context: dict) -> dict:
        """역할별 핵심 로직 — 서브클래스에서 구현"""
        ...
