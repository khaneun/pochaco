from .exchange_client import BaseExchangeClient
from .bithumb_client import BithumbClient
from .upbit_client import UpbitClient
from .websocket_client import BithumbWebSocket
from .llm_provider import BaseLLMProvider, get_llm_provider
from .derivatives_client import DerivativesClient, DerivativesData


def get_exchange_client() -> BaseExchangeClient:
    """설정에 따라 거래소 클라이언트 인스턴스 반환.

    EXCHANGE_PROVIDER=bithumb → BithumbClient
    EXCHANGE_PROVIDER=upbit   → UpbitClient
    """
    from config import settings
    if settings.EXCHANGE_PROVIDER == "upbit":
        return UpbitClient()
    return BithumbClient()


__all__ = [
    "BaseExchangeClient",
    "BithumbClient",
    "UpbitClient",
    "get_exchange_client",
    "BithumbWebSocket",
    "BaseLLMProvider",
    "get_llm_provider",
    "DerivativesClient",
    "DerivativesData",
]
