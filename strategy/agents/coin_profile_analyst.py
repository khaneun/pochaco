"""가상화폐 특성 분석가

매매가 완료될 때마다 해당 코인의 특성·패턴을 학습하여 data/coin_profiles/ 에 저장합니다.
저장된 프로파일은 매수 전문가 등이 진입 전에 참고합니다.
투자가 반복될수록 프로파일이 누적되어 코인별 최적 전략이 자동으로 개선됩니다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .base_agent import BaseSpecialistAgent

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


class CoinProfileAnalyst(BaseSpecialistAgent):
    """코인별 특성을 누적 학습·관리하는 전문가 Agent.

    - get_profile(symbol)  : 저장된 프로파일 텍스트 반환 (없으면 None)
    - execute(context)     : 매매 완료 후 프로파일 업데이트 (LLM 호출)
    """

    ROLE_NAME    = "coin_profile_analyst"
    DISPLAY_NAME = "특성 분석가"

    def __init__(self, profile_dir: Path, **kwargs):
        super().__init__(**kwargs)
        self._profile_dir = Path(profile_dir)
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._base_prompt = (
            "당신은 코인별 매매 특성을 누적 학습하는 전문가입니다.\n\n"
            "【핵심 임무】\n"
            "매매가 완료될 때마다 해당 코인의 프로파일을 기술적 분석 데이터 기반으로 업데이트합니다.\n"
            "이 프로파일은 매수 전문가가 포트폴리오 구성 시 직접 참고하므로,\n"
            "막연한 서술 대신 구체적 기술 지표 수치와 패턴을 기록해야 합니다.\n\n"
            "【프로파일 작성 원칙 — 기술 지표 중심】\n"
            "★ 매매 이력에 진입 시점의 RSI·MACD·OBV 상태를 반드시 기록\n"
            "★ 손절 시: RSI가 몇이었는지, MACD 방향, OBV 추세, BB 위치 기록\n"
            "★ 익절 시: 어떤 기술 지표 조건에서 진입해서 성공했는지 기록\n"
            "★ '이 코인의 최적 진입 RSI는?', '어떤 MACD 상태에서 성공률이 높은가?' 에 답할 수 있어야 합니다\n"
            "★ 반복 패턴: RSI 과매수 진입 후 손절 반복이면 반드시 경고 기록\n"
            "★ 매매 이력은 최신 10건만 유지 (초과 시 오래된 것 제거)"
        )

    # ── 프로파일 조회 ────────────────────────────────────────────── #

    def get_profile(self, symbol: str) -> str | None:
        """저장된 코인 프로파일 읽기.

        Args:
            symbol: 코인 심볼 (대소문자 무관)

        Returns:
            프로파일 마크다운 텍스트, 없으면 None
        """
        path = self._profile_dir / f"{symbol.upper()}.md"
        try:
            return path.read_text("utf-8") if path.exists() else None
        except Exception as e:
            logger.warning(f"[특성 분석가] {symbol} 프로파일 읽기 실패: {e}")
            return None

    def list_profiles(self) -> list[str]:
        """프로파일이 존재하는 코인 심볼 목록 반환"""
        return sorted(p.stem for p in self._profile_dir.glob("*.md"))

    def consult(self, candidate_symbols: list[str]) -> str:
        """포트폴리오 구성 전 후보 코인들에 대한 적극적 조언 제공.

        프로파일이 있는 후보 코인들을 분석하여 매수 선호·주의·회피 여부를
        판단하고, BuyStrategist가 즉시 반영할 수 있는 텍스트를 반환합니다.

        Args:
            candidate_symbols: 후보 코인 심볼 목록

        Returns:
            조언 텍스트 (프로파일 없는 코인만 있으면 빈 문자열)
        """
        profiled = {
            sym: self.get_profile(sym)
            for sym in candidate_symbols
            if self.get_profile(sym)
        }
        if not profiled:
            return ""

        profiles_block = "\n\n".join(
            f"### {sym}\n{profile}" for sym, profile in profiled.items()
        )
        task_prompt = (
            f"다음은 과거 매매 이력이 있는 코인들의 특성 프로파일입니다.\n\n"
            f"{profiles_block}\n\n"
            f"포트폴리오 구성 전 이 코인들에 대한 조언을 제공하세요.\n\n"
            f"JSON으로만 응답 (마크다운 코드블록 없이):\n"
            f'{{"recommend": [{{"symbol": "COIN", "reason": "선호 이유 (30자 이내)"}}], '
            f'"caution": [{{"symbol": "COIN", "reason": "주의 이유 (30자 이내)"}}], '
            f'"avoid": [{{"symbol": "COIN", "reason": "회피 이유 (30자 이내)"}}]}}'
        )
        try:
            raw = self._call_llm(task_prompt, max_tokens=600)
            data = self._parse_json(raw)

            lines = ["[특성 분석가 조언]"]
            for item in data.get("recommend", []):
                lines.append(f"  ✅ {item['symbol']}: {item.get('reason', '')}")
            for item in data.get("caution", []):
                lines.append(f"  ⚠️ {item['symbol']}: {item.get('reason', '')}")
            for item in data.get("avoid", []):
                lines.append(f"  ❌ {item['symbol']}: {item.get('reason', '')}")

            result = "\n".join(lines)
            logger.info(f"[특성 분석가] 조언 완료: {len(profiled)}개 코인 분석")
            return result
        except Exception as e:
            logger.warning(f"[특성 분석가] 조언 생성 실패: {e}")
            return ""

    # ── 프로파일 업데이트 (LLM 호출) ────────────────────────────── #

    def execute(self, context: dict) -> dict:
        """매매 완료 후 코인 프로파일 생성·업데이트.

        매 거래가 끝날 때 AgentCoordinator.evaluate_trade() 에서 호출합니다.

        Args:
            context: {
                "symbol": str,
                "buy_price": float,
                "sell_price": float,
                "pnl_pct": float,
                "held_minutes": float,
                "exit_type": str,           # "take_profit" | "stop_loss" | "timeout"
                "agent_reason": str,        # 매수 이유
                "original_tp": float,
                "original_sl": float,
                "original_sl_1st": float | None,
                "partial_sl_executed": bool,
                "evaluation": str,          # PortfolioEvaluator 평가 텍스트
                "lesson": str,
                "trade_time": str,          # "YYYY-MM-DD HH:MM" (KST)
            }

        Returns:
            {"updated": bool, "symbol": str}
        """
        symbol = context.get("symbol", "").upper()
        if not symbol:
            return {"updated": False, "symbol": ""}

        try:
            existing  = self.get_profile(symbol) or ""
            new_trade = self._format_trade(context)
            updated   = self._update_with_llm(symbol, existing, new_trade)
            self._save_profile(symbol, updated)
            logger.info(
                f"[특성 분석가] {symbol} 프로파일 업데이트 완료 ({len(updated)}자)"
            )
            return {"updated": True, "symbol": symbol}
        except Exception as e:
            logger.error(f"[특성 분석가] {symbol} 프로파일 업데이트 실패: {e}")
            return {"updated": False, "symbol": symbol}

    # ── 내부 헬퍼 ────────────────────────────────────────────────── #

    @staticmethod
    def _format_trade(ctx: dict) -> str:
        """새 거래 데이터를 프롬프트용 텍스트로 포맷 (기술 지표 포함)"""
        exit_kr = {
            "take_profit": "익절",
            "stop_loss":   "손절",
        }.get(ctx.get("exit_type", ""), "시간초과")
        sl1       = ctx.get("original_sl_1st")
        sl1_str   = f"{sl1}%" if sl1 else "없음"
        partial   = "실행" if ctx.get("partial_sl_executed") else "미실행"

        # 기술 지표 (진입 시점)
        tech = ctx.get("technical_summary", "")
        tech_line = f"기술지표  : {tech}\n" if tech else ""

        return (
            f"거래일시  : {ctx.get('trade_time', '')}\n"
            f"결과      : {exit_kr} {ctx.get('pnl_pct', 0):+.2f}%\n"
            f"가격      : {ctx.get('buy_price', 0):,.0f}원 → {ctx.get('sell_price', 0):,.0f}원\n"
            f"보유 시간 : {ctx.get('held_minutes', 0):.0f}분\n"
            f"설정      : TP +{ctx.get('original_tp', 0)}% / "
            f"SL1 {sl1_str} / SL2 {ctx.get('original_sl', 0)}% / 1차손절 {partial}\n"
            f"{tech_line}"
            f"매수 이유 : {ctx.get('agent_reason', '')}\n"
            f"AI 평가   : {ctx.get('evaluation', '')}\n"
            f"교훈      : {ctx.get('lesson', '')}"
        )

    def _update_with_llm(self, symbol: str, existing: str, new_trade: str) -> str:
        """LLM을 사용해 프로파일을 갱신하고 마크다운 텍스트를 반환"""
        existing_section = (
            f"**기존 프로파일:**\n{existing}\n\n"
            if existing else
            "**기존 프로파일:** 없음 (첫 매매)\n\n"
        )
        today = datetime.now(tz=_KST).strftime("%Y-%m-%d")

        task_prompt = (
            f"{existing_section}"
            f"**새로 완료된 거래 데이터:**\n{new_trade}\n\n"
            f"위 정보를 바탕으로 {symbol} 특성 프로파일을 작성·업데이트하세요.\n\n"
            f"**작성 규칙:**\n"
            f"- 전체 분량: 600자 이내 (간결하게)\n"
            f"- 매매 이력: 최신 10건만 유지 (초과 시 가장 오래된 항목 제거)\n"
            f"- ★ 기술 지표(RSI, MACD, OBV) 수치를 반드시 포함할 것\n"
            f"- 막연한 서술('변동성 크다') 금지, 구체적 수치('RSI 72 진입→손절') 필수\n"
            f"- 아래 마크다운 형식을 정확히 따를 것 (코드블록 없이 순수 마크다운)\n\n"
            f"---\n"
            f"# {symbol} 특성 프로파일\n"
            f"*마지막 업데이트: {today}*\n\n"
            f"## 기술 지표 패턴\n"
            f"- 최적 진입 RSI: (경험 기반, 예: 35~50)\n"
            f"- 위험 진입 RSI: (과매수 진입 후 손절 패턴이 있으면 기록)\n"
            f"- MACD 성공 패턴: (골든크로스 진입 성공률 등)\n"
            f"- OBV 특이사항: (가짜 상승 경험 등)\n\n"
            f"## 매매 이력 (최신순, 최대 10건)\n"
            f"| 날짜 | 결과 | 수익률 | 보유 | RSI | MACD | 핵심교훈 |\n"
            f"|------|------|--------|------|-----|------|---------|\n"
            f"| YYYY-MM-DD | 익절/손절 | +X.X% | X분 | 65 | 상승 | ... |\n\n"
            f"## 전략 권고\n"
            f"- 최적 TP/SL: (경험 기반 범위)\n"
            f"- 유리한 진입 조건: (RSI·MACD·OBV 조합)\n"
            f"- 회피 조건: (어떤 기술 상태에서 손절 반복되는지)\n\n"
            f"## 주의사항\n"
            f"- (이 코인 매매 시 특히 주의할 기술적 신호)\n"
            f"---"
        )

        return self._call_llm(task_prompt, max_tokens=900)

    def _save_profile(self, symbol: str, content: str) -> None:
        """프로파일 텍스트를 파일로 저장"""
        path = self._profile_dir / f"{symbol.upper()}.md"
        path.write_text(content.strip(), encoding="utf-8")
