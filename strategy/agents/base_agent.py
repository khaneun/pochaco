"""전문가 Agent 공통 기반

피드백 시스템:
  - 총괄 평가가(MetaEvaluator)가 3시간 주기로 각 Agent를 평가
  - 피드백은 누적 요약 형태로 축적 (최근 3회분 유지)
  - 점수 추이(상승/하락/유지)가 함께 표시되어 Agent가 자기 개선 방향을 인식
"""
import json
import logging
from abc import ABC, abstractmethod

from core.llm_provider import BaseLLMProvider, get_llm_provider

logger = logging.getLogger(__name__)

# 피드백 누적 최대 보관 수
_MAX_FEEDBACK_HISTORY = 3


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
        self._feedback_prompt: str = ""  # MetaEvaluator가 주입 (누적 요약)
        self._score: float = 50.0
        self._feedback_history: list[dict] = []  # 최근 피드백 히스토리

    @property
    def role_name(self) -> str:
        return self.ROLE_NAME

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    @property
    def score(self) -> float:
        return self._score

    @property
    def base_prompt(self) -> str:
        return self._base_prompt

    @property
    def feedback_prompt(self) -> str:
        return self._feedback_prompt

    # ---------------------------------------------------------------- #
    #  누적 피드백 관리                                                   #
    # ---------------------------------------------------------------- #
    def update_feedback(self, feedback: str, score: float) -> None:
        """총괄 평가가의 피드백을 누적 요약으로 반영

        피드백을 히스토리에 추가하고, 누적 요약 프롬프트를 재생성합니다.
        최근 3회분의 피드백을 유지하며 추이를 표시합니다.
        """
        previous_score = self._score
        self._score = max(0.0, min(100.0, score))

        # 히스토리에 추가 (최근 N개 유지)
        self._feedback_history.append({
            "score": round(score, 1),
            "previous_score": round(previous_score, 1),
            "feedback_text": feedback,
        })
        if len(self._feedback_history) > _MAX_FEEDBACK_HISTORY:
            self._feedback_history = self._feedback_history[-_MAX_FEEDBACK_HISTORY:]

        # 누적 요약 프롬프트 생성
        self._feedback_prompt = self._build_cumulative_feedback()

    def _build_cumulative_feedback(self) -> str:
        """누적 피드백을 요약 프롬프트로 변환"""
        if not self._feedback_history:
            return ""

        latest = self._feedback_history[-1]
        score = latest["score"]
        prev = latest["previous_score"]

        # 점수 추이 계산
        if len(self._feedback_history) >= 2:
            scores = [h["score"] for h in self._feedback_history]
            if scores[-1] > scores[0] + 3:
                trend = "📈 상승 추세"
            elif scores[-1] < scores[0] - 3:
                trend = "📉 하락 추세"
            else:
                trend = "➡️ 유지"
        else:
            trend = "🆕 첫 평가"

        # 점수 변동 표시
        delta = score - prev
        if delta > 0:
            delta_str = f"+{delta:.0f}점 ↑"
        elif delta < 0:
            delta_str = f"{delta:.0f}점 ↓"
        else:
            delta_str = "변동 없음"

        lines = [
            f"현재 점수: {score:.0f}/100 ({delta_str}) | 추이: {trend}",
            "",
        ]

        # 최신 피드백 (가장 중요)
        lines.append("【최신 평가 — 반드시 즉시 반영】")
        lines.append(latest["feedback_text"])

        # 이전 피드백 요약 (패턴 인식용)
        if len(self._feedback_history) >= 2:
            lines.append("")
            lines.append("【누적 패턴 — 반복되는 문제는 반드시 해결】")
            for i, h in enumerate(self._feedback_history[:-1]):
                eval_num = len(self._feedback_history) - 1 - i
                lines.append(
                    f"  [{eval_num}회 전] {h['score']:.0f}점: "
                    f"{self._extract_directive(h['feedback_text'])}"
                )

        return "\n".join(lines)

    @staticmethod
    def _extract_directive(feedback_text: str) -> str:
        """피드백 텍스트에서 지시(directive) 부분만 추출"""
        for line in feedback_text.split("\n"):
            if line.startswith("지시:"):
                return line[3:].strip()
        return feedback_text[:100]

    def update_base_prompt(self, new_prompt: str) -> None:
        """기본 역할 프롬프트를 업데이트 (대시보드 수동 수정)"""
        self._base_prompt = new_prompt

    # ---------------------------------------------------------------- #
    #  대화 모드                                                         #
    # ---------------------------------------------------------------- #
    def chat(self, message: str, history: list[dict] | None = None) -> str:
        """대화 모드 — 역할에 맞게 운영자와 자유롭게 대화"""
        system = (
            self._build_system_context()
            + "\n\n[대화 모드] 지금은 운영자와 직접 대화하는 시간입니다. "
            "당신의 전문 역할과 현재까지의 판단 기준을 바탕으로 솔직하고 유익하게 답변하세요. "
            "전략 개선 방향, 현재 설정의 문제점 등 어떤 주제든 솔직하게 이야기하세요."
        )
        messages = list(history or [])
        messages.append({"role": "user", "content": message})
        self._llm._current_agent = self.ROLE_NAME or "unknown"
        return self._llm.chat_with_system(system, messages, max_tokens=1024)

    # ---------------------------------------------------------------- #
    #  LLM 호출                                                          #
    # ---------------------------------------------------------------- #
    def _build_system_context(self) -> str:
        """기본 프롬프트 + 누적 피드백 조합"""
        parts = [self._base_prompt]
        if self._feedback_prompt:
            parts.append(
                f"\n\n{'='*60}\n"
                f"[총괄 평가 피드백 — 이 지시를 무시하면 점수가 하락합니다]\n"
                f"{'='*60}\n"
                f"{self._feedback_prompt}"
            )
        return "\n".join(parts)

    def _call_llm(self, task_prompt: str, max_tokens: int = 512) -> str:
        """시스템 컨텍스트 + 작업 프롬프트로 LLM 호출"""
        full_prompt = f"{self._build_system_context()}\n\n---\n\n{task_prompt}"
        self._llm._current_agent = self.ROLE_NAME or "unknown"
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
