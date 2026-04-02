"""AI Agent - 코인 선정 및 익절/손절 기준 결정

LLM 공급자에 의존하지 않습니다. core.llm_provider.get_llm_provider()가
.env의 LLM_PROVIDER 값에 따라 Anthropic / OpenAI / Gemini 중 하나를 주입합니다.
"""
import json
import logging
from dataclasses import dataclass

from core.llm_provider import BaseLLMProvider, get_llm_provider
from .market_analyzer import CoinSnapshot

logger = logging.getLogger(__name__)


@dataclass
class AgentDecision:
    symbol: str
    take_profit_pct: float
    stop_loss_pct: float
    confidence: float
    reason: str
    llm_provider: str = ""      # 어떤 LLM이 결정했는지 기록


class TradingAgent:
    """LLM 공급자에 위임하는 매매 의사결정 에이전트"""

    def __init__(self, llm: BaseLLMProvider | None = None):
        self._llm = llm or get_llm_provider()

    @property
    def provider_name(self) -> str:
        return self._llm.provider_name

    # ---------------------------------------------------------------- #
    #  공통 프롬프트 빌더                                                 #
    # ---------------------------------------------------------------- #
    @staticmethod
    def _snapshots_to_text(snapshots: list[CoinSnapshot]) -> str:
        lines = []
        for s in snapshots:
            lines.append(
                f"- {s.symbol}: "
                f"현재가={s.current_price:,.0f}원, "
                f"24h변동={s.change_pct_24h:+.2f}%, "
                f"24h거래대금={s.volume_krw_24h / 1e8:.1f}억원, "
                f"고가={s.high_price:,.0f}, 저가={s.low_price:,.0f}"
            )
        return "\n".join(lines)

    # ---------------------------------------------------------------- #
    #  코인 선정                                                          #
    # ---------------------------------------------------------------- #
    def select_coin(self, snapshots: list[CoinSnapshot]) -> AgentDecision:
        """시장 데이터를 분석해 매수할 코인 1개와 익절/손절 기준을 결정"""

        market_text = self._snapshots_to_text(snapshots)

        prompt = f"""당신은 단기 변동성 매매 전문 트레이더입니다.
전략: 코인 1개를 전액 매수 → 익절 또는 손절 시 전량 매도 → 즉시 반복.
수익은 오직 가격 변동성에서만 나옵니다.

아래는 빗썸 거래소 상위 코인의 실시간 시장 데이터입니다.

{market_text}

**코인 선정 기준 (우선순위 순)**
1. 강한 단기 상승 모멘텀 — 현재가가 당일 고가 대비 10% 이내에 있고, 24h 변동률이 양수이며 상승 추세인 코인 최우선
2. 거래대금 충분 — 최소 50억원/24h 이상, 슬리피지 최소화 및 빠른 체결 보장
3. 고저 차이가 클 것 — 24h 고저 차이가 5% 이상인 코인 우선 (변동성 확보)
4. 하락 중인 코인은 절대 선정 금지 — 24h 변동률이 음수이거나 현재가가 당일 저가 근처인 코인 제외

**익절·손절 기준 설정 원칙 (반드시 준수)**
- 리워드:리스크 비율을 최소 3:1 이상으로 설정 — 손절 1%당 익절은 3% 이상
- 익절(take_profit_pct): 24h 고저 차이의 50% 이상을 목표로 설정, 최소 3% 이상
- 손절(stop_loss_pct): 최대 -2%로 제한 — 그 이상의 손실은 절대 허용하지 않음
- 수수료(매수+매도 약 0.4%) 감안 후에도 순이익이 발생해야 함 — 익절 기준은 반드시 1% 이상의 순수익 보장
- 손절 기준이 -2%를 초과하는 선정은 무효 — 반드시 -2% 이하(예: -1.5%, -1.0%)로 설정

반드시 아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "symbol": "코인심볼(예:BTC)",
  "take_profit_pct": 익절퍼센트(숫자, 예:4.5),
  "stop_loss_pct": 손절퍼센트(음수숫자, -2.0 이하, 예:-1.5),
  "confidence": 확신도(0.0~1.0),
  "reason": "선정 이유 (한국어, 100자 이내) — 상승 모멘텀 근거 포함"
}}"""

        logger.info(f"AI Agent ({self.provider_name}): 코인 선정 분석 중...")
        raw = self._llm.chat(prompt, max_tokens=512)
        logger.info(f"AI Agent 응답: {raw}")

        try:
            # JSON 코드블록 감싸진 경우 제거
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean)
            return AgentDecision(
                symbol=data["symbol"].upper(),
                take_profit_pct=float(data["take_profit_pct"]),
                stop_loss_pct=float(data["stop_loss_pct"]),
                confidence=float(data.get("confidence", 0.5)),
                reason=data.get("reason", ""),
                llm_provider=self.provider_name,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"AI Agent 응답 파싱 실패: {e}\n원문: {raw}")
            raise RuntimeError(f"AI Agent 응답 파싱 실패: {e}")

    # ---------------------------------------------------------------- #
    #  전략 조정 질의                                                     #
    # ---------------------------------------------------------------- #
    def should_adjust_strategy(
        self,
        symbol: str,
        buy_price: float,
        current_price: float,
        current_pnl_pct: float,
        holding_minutes: int,
        original_tp: float,
        original_sl: float,
    ) -> dict:
        """포지션 보유 중 익절/손절 기준 조정 여부를 LLM에게 질의"""

        prompt = f"""현재 보유 포지션:
- 코인: {symbol}
- 매수가: {buy_price:,.0f}원
- 현재가: {current_price:,.0f}원
- 현재 수익률: {current_pnl_pct:+.2f}%
- 보유 시간: {holding_minutes}분
- 원래 익절 기준: +{original_tp}%
- 원래 손절 기준: {original_sl}%

현재 상황에서 익절/손절 기준을 조정해야 하나요?
아래 JSON 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON):
{{
  "adjust": true 또는 false,
  "new_take_profit_pct": 숫자(조정 불필요 시 원래값),
  "new_stop_loss_pct": 숫자(조정 불필요 시 원래값),
  "reason": "이유 (50자 이내)"
}}"""

        raw = self._llm.chat(prompt, max_tokens=256)
        try:
            clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            return {
                "adjust": False,
                "new_take_profit_pct": original_tp,
                "new_stop_loss_pct": original_sl,
                "reason": "파싱 실패, 기존 전략 유지",
            }
