"""LLM 공급자 추상화 레이어

.env의 LLM_PROVIDER 값에 따라 Anthropic / OpenAI / Gemini 중 하나를 사용합니다.
ai_agent.py 등 상위 모듈은 BaseLLMProvider.chat() 만 호출하면 됩니다.

토큰 사용량은 _UsageTracker 싱글톤에 자동 기록됩니다.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone, timedelta

from config import settings

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

# ------------------------------------------------------------------ #
#  토큰 사용량 추적기                                                   #
# ------------------------------------------------------------------ #
# 모델별 가격 (USD per 1M tokens): (input, output)
_PRICE_MAP: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o":                    (2.50,  10.00),
    "gpt-4o-2024-11-20":         (2.50,  10.00),
    "gpt-4o-2024-08-06":         (2.50,  10.00),
    "gpt-4o-mini":               (0.15,   0.60),
    "gpt-4o-mini-2024-07-18":    (0.15,   0.60),
    "gpt-4-turbo":               (10.00, 30.00),
    "gpt-4":                     (30.00, 60.00),
    "o1":                        (15.00, 60.00),
    "o1-mini":                   (3.00,  12.00),
    # Anthropic
    "claude-opus-4-6":           (15.00, 75.00),
    "claude-sonnet-4-6":         (3.00,  15.00),
    "claude-haiku-4-5-20251001": (0.80,   4.00),
    "claude-3-5-sonnet-20241022":(3.00,  15.00),
    "claude-3-5-haiku-20241022": (0.80,   4.00),
    "claude-3-opus-20240229":    (15.00, 75.00),
    "claude-3-haiku-20240307":   (0.25,   1.25),
    # Gemini
    "gemini-2.0-flash":          (0.10,   0.40),
    "gemini-1.5-pro":            (1.25,   5.00),
    "gemini-1.5-flash":          (0.075,  0.30),
    "gemini-1.0-pro":            (0.50,   1.50),
}
_USD_TO_KRW = 1_380.0   # 환율 근사값


def _calc_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """토큰 수로 비용(USD) 계산. 알 수 없는 모델은 0 반환."""
    key = model.lower()
    # 접두사 매칭 (버전 suffix 허용)
    price = None
    for k, v in _PRICE_MAP.items():
        if key == k or key.startswith(k):
            price = v
            break
    if price is None:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


class _UsageRecord:
    __slots__ = ("ts", "agent", "model", "input_tokens", "output_tokens", "cost_usd")

    def __init__(
        self,
        agent: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ):
        self.ts = datetime.now(tz=_KST)
        self.agent = agent
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = _calc_cost_usd(model, input_tokens, output_tokens)


class _UsageTracker:
    """스레드 안전 LLM 토큰 사용량 추적 싱글톤"""

    _MAX_RECORDS = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._records: deque[_UsageRecord] = deque(maxlen=self._MAX_RECORDS)
        self._session_start = datetime.now(tz=_KST)

    def record(
        self,
        agent: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with self._lock:
            self._records.append(_UsageRecord(agent, model, input_tokens, output_tokens))

    def get_stats(self) -> dict:
        """전체/에이전트별/모델별 집계 반환"""
        with self._lock:
            records = list(self._records)

        total_in = sum(r.input_tokens for r in records)
        total_out = sum(r.output_tokens for r in records)
        total_cost = sum(r.cost_usd for r in records)
        total_calls = len(records)

        # 에이전트별 집계
        by_agent: dict[str, dict] = {}
        for r in records:
            a = by_agent.setdefault(r.agent, {"calls": 0, "input": 0, "output": 0, "cost_usd": 0.0})
            a["calls"] += 1
            a["input"] += r.input_tokens
            a["output"] += r.output_tokens
            a["cost_usd"] += r.cost_usd

        # 최근 100건 로그
        recent = []
        for r in reversed(list(records)[-100:]):
            recent.append({
                "ts": r.ts.strftime("%m-%d %H:%M:%S"),
                "agent": r.agent,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(r.cost_usd, 6),
                "cost_krw": round(r.cost_usd * _USD_TO_KRW, 2),
            })

        return {
            "session_start": self._session_start.strftime("%m-%d %H:%M"),
            "total_calls": total_calls,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "total_cost_usd": round(total_cost, 4),
            "total_cost_krw": round(total_cost * _USD_TO_KRW, 0),
            "by_agent": dict(
                sorted(by_agent.items(), key=lambda x: x[1]["cost_usd"], reverse=True)
            ),
            "recent": recent,
        }


# 전역 싱글톤
usage_tracker = _UsageTracker()


# ------------------------------------------------------------------ #
#  추상 기반 클래스                                                     #
# ------------------------------------------------------------------ #
class BaseLLMProvider(ABC):
    # 하위 클래스가 현재 호출 주체(에이전트명)를 설정할 수 있도록
    _current_agent: str = "unknown"

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
        usage_tracker.record(
            agent=self._current_agent,
            model=self._model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
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
        usage_tracker.record(
            agent=self._current_agent,
            model=self._model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
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
        if response.usage:
            usage_tracker.record(
                agent=self._current_agent,
                model=self._model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
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
        if response.usage:
            usage_tracker.record(
                agent=self._current_agent,
                model=self._model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
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
        try:
            um = response.usage_metadata
            usage_tracker.record(
                agent=self._current_agent,
                model=self._model_name,
                input_tokens=um.prompt_token_count or 0,
                output_tokens=um.candidates_token_count or 0,
            )
        except Exception:
            pass
        return response.text.strip()

    def chat_with_system(
        self, system: str, messages: list[dict], max_tokens: int = 1024
    ) -> str:
        parts = [f"[시스템 지침]\n{system}\n\n[대화 내역]"]
        for m in messages:
            label = "사용자" if m["role"] == "user" else "AI"
            parts.append(f"{label}: {m['content']}")
        full_prompt = "\n\n".join(parts)
        response = self._model.generate_content(
            full_prompt,
            generation_config={"max_output_tokens": max_tokens},
        )
        try:
            um = response.usage_metadata
            usage_tracker.record(
                agent=self._current_agent,
                model=self._model_name,
                input_tokens=um.prompt_token_count or 0,
                output_tokens=um.candidates_token_count or 0,
            )
        except Exception:
            pass
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
