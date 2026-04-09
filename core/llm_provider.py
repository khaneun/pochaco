"""LLM 공급자 추상화 레이어

.env의 LLM_PROVIDER 값에 따라 Anthropic / OpenAI / Gemini 중 하나를 사용합니다.
ai_agent.py 등 상위 모듈은 BaseLLMProvider.chat() 만 호출하면 됩니다.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from config import settings

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  추상 기반 클래스                                                     #
# ------------------------------------------------------------------ #
class BaseLLMProvider(ABC):
    @abstractmethod
    def chat(self, prompt: str, max_tokens: int = 1024) -> str:
        """단일 사용자 메시지를 보내고 텍스트 응답을 반환"""
        ...

    @abstractmethod
    def chat_with_system(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
    ) -> str:
        """시스템 프롬프트 + 멀티턴 메시지로 LLM 호출

        messages: [{"role": "user"|"assistant", "content": "..."}]
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...


# ------------------------------------------------------------------ #
#  Anthropic (Claude)                                                  #
# ------------------------------------------------------------------ #
class AnthropicProvider(BaseLLMProvider):
    def __init__(self):
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic 패키지가 필요합니다: pip install anthropic")
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError(".env에 ANTHROPIC_API_KEY가 없습니다")
        self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._model = settings.ANTHROPIC_MODEL

    @property
    def provider_name(self) -> str:
        return f"anthropic/{self._model}"

    def chat(self, prompt: str, max_tokens: int = 1024) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    def chat_with_system(
        self, system: str, messages: list[dict], max_tokens: int = 1024
    ) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return msg.content[0].text.strip()


# ------------------------------------------------------------------ #
#  OpenAI (ChatGPT)                                                    #
# ------------------------------------------------------------------ #
class OpenAIProvider(BaseLLMProvider):
    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai 패키지가 필요합니다: pip install openai")
        if not settings.OPENAI_API_KEY:
            raise ValueError(".env에 OPENAI_API_KEY가 없습니다")
        self._client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = settings.OPENAI_MODEL

    @property
    def provider_name(self) -> str:
        return f"openai/{self._model}"

    def chat(self, prompt: str, max_tokens: int = 1024) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    def chat_with_system(
        self, system: str, messages: list[dict], max_tokens: int = 1024
    ) -> str:
        all_msgs = [{"role": "system", "content": system}] + messages
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=all_msgs,
        )
        return response.choices[0].message.content.strip()


# ------------------------------------------------------------------ #
#  Google Gemini                                                        #
# ------------------------------------------------------------------ #
class GeminiProvider(BaseLLMProvider):
    def __init__(self):
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError(
                "google-generativeai 패키지가 필요합니다: pip install google-generativeai"
            )
        if not settings.GEMINI_API_KEY:
            raise ValueError(".env에 GEMINI_API_KEY가 없습니다")
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._model = genai.GenerativeModel(settings.GEMINI_MODEL)
        self._model_name = settings.GEMINI_MODEL

    @property
    def provider_name(self) -> str:
        return f"gemini/{self._model_name}"

    def chat(self, prompt: str, max_tokens: int = 1024) -> str:
        response = self._model.generate_content(
            prompt,
            generation_config={"max_output_tokens": max_tokens},
        )
        return response.text.strip()

    def chat_with_system(
        self, system: str, messages: list[dict], max_tokens: int = 1024
    ) -> str:
        # Gemini: 시스템 지침 + 대화 이력을 단일 텍스트로 구성
        parts = [f"[시스템 지침]\n{system}\n\n[대화 내역]"]
        for m in messages:
            label = "사용자" if m["role"] == "user" else "AI"
            parts.append(f"{label}: {m['content']}")
        full_prompt = "\n\n".join(parts)
        response = self._model.generate_content(
            full_prompt,
            generation_config={"max_output_tokens": max_tokens},
        )
        return response.text.strip()


# ------------------------------------------------------------------ #
#  팩토리 함수                                                          #
# ------------------------------------------------------------------ #
_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def get_llm_provider() -> BaseLLMProvider:
    """LLM_PROVIDER 설정값에 맞는 공급자 인스턴스를 반환"""
    key = settings.LLM_PROVIDER.lower()
    if key not in _PROVIDERS:
        raise ValueError(
            f"지원하지 않는 LLM_PROVIDER: '{key}'. "
            f"가능한 값: {list(_PROVIDERS.keys())}"
        )
    provider = _PROVIDERS[key]()
    logger.info(f"LLM 공급자: {provider.provider_name}")
    return provider
