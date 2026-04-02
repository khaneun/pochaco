"""텔레그램 봇 — 상태 조회 / 매매 제어 / 알림

명령어:
  /help     — 명령어 목록
  /status   — 전체 상태 요약 (잔고·포지션·수익률)
  /balance  — KRW 잔고
  /position — 현재 포지션 상세
  /stop     — 신규 매수 일시 중지 (기존 포지션 감시는 유지)
  /resume   — 매수 재개
  /report   — 오늘 성과 + 최근 7일 요약
  /dashboard — 웹 대시보드 접속 주소

알림 (엔진이 자동 발송):
  매수 완료 / 익절·손절 매도 / 오류 / 일별 리포트
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

import requests

from config import settings
from database import TradeRepository
from database.models import Position

if TYPE_CHECKING:
    from core import BithumbClient
    from strategy.trading_engine import TradingEngine

logger = logging.getLogger(__name__)

_HELP_TEXT = """📋 <b>pochaco 명령어 목록</b>

/status   — 전체 상태 요약
/balance  — KRW 잔고 조회
/position — 현재 포지션 상세
/stop     — 신규 매수 일시 중지
/resume   — 매수 재개
/report   — 오늘 + 최근 7일 성과
/dashboard — 웹 대시보드 접속 주소
/help     — 이 메시지"""


class TelegramBot:
    """텔레그램 봇 (long-polling, 동기)"""

    _API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(
        self,
        token: str,
        chat_id: str,
        client: "BithumbClient",
        repo: TradeRepository,
        engine: "TradingEngine",
    ):
        self._token = token
        self._chat_id = str(chat_id).strip()
        self._client = client
        self._repo = repo
        self._engine = engine
        self._running = False
        self._offset = 0

    # ------------------------------------------------------------------ #
    #  텔레그램 API 래퍼                                                   #
    # ------------------------------------------------------------------ #
    def _call(self, method: str, timeout: int = 10, **kwargs) -> dict:
        url = self._API.format(token=self._token, method=method)
        try:
            resp = requests.post(url, json=kwargs, timeout=timeout)
            return resp.json()
        except Exception as e:
            logger.warning(f"Telegram API 오류 ({method}): {e}")
            return {}

    def send(self, text: str, parse_mode: str = "HTML") -> None:
        """허가된 chat_id로 메시지 발송"""
        if not self._token or not self._chat_id:
            return
        self._call(
            "sendMessage",
            chat_id=self._chat_id,
            text=text,
            parse_mode=parse_mode,
        )

    # ------------------------------------------------------------------ #
    #  알림 메서드 (TradingEngine에서 호출)                                 #
    # ------------------------------------------------------------------ #
    def notify_start(self) -> None:
        self.send(
            "🚀 <b>pochaco 시작</b>\n"
            f"LLM: {settings.LLM_PROVIDER}\n"
            f"감시주기: {settings.POSITION_CHECK_INTERVAL}초\n"
            f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def notify_buy(
        self,
        symbol: str,
        price: float,
        units: float,
        krw_amount: float,
        reason: str,
        take_profit_pct: float,
        stop_loss_pct: float,
        llm_provider: str = "",
    ) -> None:
        self.send(
            f"🟢 <b>매수 완료</b>\n"
            f"코인: <b>{symbol}</b>\n"
            f"수량: {units:.6f}개\n"
            f"매수가: {price:,.0f} 원\n"
            f"투입: {krw_amount:,.0f} 원\n"
            f"목표: 익절 +{take_profit_pct}% / 손절 {stop_loss_pct}%\n"
            f"AI 이유: {reason[:80]}\n"
            f"LLM: {llm_provider}"
        )

    def notify_sell(
        self,
        symbol: str,
        price: float,
        pnl_pct: float,
        pnl_krw: float,
        reason: str,
        held_minutes: float = 0,
    ) -> None:
        icon = "✅" if pnl_pct >= 0 else "🔴"
        sign = "+" if pnl_pct >= 0 else ""
        held_str = (
            f"{held_minutes / 60:.1f}시간"
            if held_minutes >= 60
            else f"{held_minutes:.0f}분"
        )
        self.send(
            f"{icon} <b>{'익절' if pnl_pct >= 0 else '손절'} 매도</b>\n"
            f"코인: <b>{symbol}</b>\n"
            f"매도가: {price:,.0f} 원\n"
            f"손익: {sign}{pnl_pct:.2f}% ({sign}{pnl_krw:,.0f} 원)\n"
            f"보유시간: {held_str}\n"
            f"사유: {reason}"
        )

    def notify_error(self, msg: str) -> None:
        self.send(f"⚠️ <b>오류 발생</b>\n{msg[:300]}")

    def notify_daily_report(
        self,
        date: str,
        starting_krw: float,
        ending_krw: float,
        pnl_krw: float,
        pnl_pct: float,
        trade_count: int,
        win_count: int,
    ) -> None:
        sign = "+" if pnl_krw >= 0 else ""
        icon = "📈" if pnl_krw >= 0 else "📉"
        loss_count = trade_count // 2 - win_count if trade_count > 0 else 0
        self.send(
            f"{icon} <b>일별 성과 리포트 ({date})</b>\n"
            f"시작: {starting_krw:,.0f} 원\n"
            f"종료: {ending_krw:,.0f} 원\n"
            f"손익: {sign}{pnl_krw:,.0f} 원 ({sign}{pnl_pct:.2f}%)\n"
            f"거래: {trade_count}건 (익절 {win_count} / 손절 {loss_count})"
        )

    def notify_paused(self) -> None:
        self.send("⏸ <b>매매 일시 중지</b>\n신규 매수를 중단합니다.\n보유 포지션 감시는 계속됩니다.")

    def notify_resumed(self) -> None:
        self.send("▶️ <b>매매 재개</b>\n신규 매수를 다시 시작합니다.")

    # ------------------------------------------------------------------ #
    #  명령어 핸들러                                                        #
    # ------------------------------------------------------------------ #
    def _cmd_help(self, _args: str) -> None:
        self.send(_HELP_TEXT)

    def _cmd_status(self, _args: str) -> None:
        try:
            krw = self._client.get_krw_balance()
            total = krw
            pos_line = "없음"

            pos: Position | None = self._repo.get_open_position()
            if pos:
                cur = self._client.get_current_price(pos.symbol)
                pnl_pct = (cur - pos.buy_price) / pos.buy_price * 100
                pos_value = pos.units * cur
                total = krw + pos_value
                sign = "+" if pnl_pct >= 0 else ""
                pos_line = (
                    f"{pos.symbol} {pos.units:.4f}개  "
                    f"({sign}{pnl_pct:.2f}%)"
                )

            stats = self._repo.get_total_stats()
            initial = stats["initial_krw"]
            total_pnl_pct = (total - initial) / initial * 100 if initial > 0 else 0

            state = "⏸ 일시중지" if self._engine.is_paused else "✅ 실행 중"

            sign = "+" if total_pnl_pct >= 0 else ""
            self.send(
                f"📊 <b>pochaco 상태</b>\n"
                f"\n"
                f"💰 KRW 잔고: {krw:,.0f} 원\n"
                f"🏦 총 자산: {total:,.0f} 원\n"
                f"📦 현재 포지션: {pos_line}\n"
                f"\n"
                f"📈 누적 수익률: {sign}{total_pnl_pct:.2f}%\n"
                f"🎯 승률: {stats['win_rate']:.0%} "
                f"({stats['win_count']}승 {stats['loss_count']}패)\n"
                f"\n"
                f"⚡ 매매 상태: {state}\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
            )
        except Exception as e:
            self.send(f"❌ 상태 조회 실패: {e}")

    def _cmd_balance(self, _args: str) -> None:
        try:
            krw = self._client.get_krw_balance()
            self.send(f"💰 <b>KRW 잔고</b>\n{krw:,.0f} 원")
        except Exception as e:
            self.send(f"❌ 잔고 조회 실패: {e}")

    def _cmd_position(self, _args: str) -> None:
        pos: Position | None = self._repo.get_open_position()
        if pos is None:
            self.send("📭 현재 보유 포지션 없음\nAI 코인 선정 대기 중...")
            return
        try:
            cur = self._client.get_current_price(pos.symbol)
            pnl_pct = (cur - pos.buy_price) / pos.buy_price * 100
            pnl_krw = (cur - pos.buy_price) * pos.units
            held_min = (datetime.utcnow() - pos.opened_at).total_seconds() / 60
            held_str = f"{held_min / 60:.1f}시간" if held_min >= 60 else f"{held_min:.0f}분"
            sign = "+" if pnl_pct >= 0 else ""
            icon = "📈" if pnl_pct >= 0 else "📉"
            self.send(
                f"{icon} <b>현재 포지션: {pos.symbol}</b>\n"
                f"\n"
                f"수량: {pos.units:.6f}개\n"
                f"매수가: {pos.buy_price:,.0f} 원\n"
                f"현재가: {cur:,.0f} 원\n"
                f"손익: {sign}{pnl_pct:.2f}% ({sign}{pnl_krw:,.0f} 원)\n"
                f"\n"
                f"익절 기준: +{pos.take_profit_pct}%\n"
                f"손절 기준: {pos.stop_loss_pct}%\n"
                f"보유시간: {held_str}\n"
                f"\n"
                f"AI: {(pos.agent_reason or '')[:80]}"
            )
        except Exception as e:
            self.send(f"❌ 포지션 조회 실패: {e}")

    def _cmd_stop(self, _args: str) -> None:
        if self._engine.is_paused:
            self.send("⏸ 이미 일시 중지 상태입니다.")
            return
        self._engine.pause()
        self.notify_paused()

    def _cmd_resume(self, _args: str) -> None:
        if not self._engine.is_paused:
            self.send("▶️ 이미 실행 중입니다.")
            return
        self._engine.resume()
        self.notify_resumed()

    def _cmd_report(self, _args: str) -> None:
        try:
            reports = self._repo.get_recent_reports(7)
            if not reports:
                self.send("📭 성과 데이터 없음\n(매일 23:55에 자동 기록됩니다)")
                return

            lines = ["📋 <b>성과 리포트 (최근 7일)</b>\n"]
            for r in reversed(reports):
                sign = "+" if r.pnl_krw >= 0 else ""
                icon = "🟢" if r.pnl_krw >= 0 else "🔴"
                win = r.win_count
                total = r.trade_count // 2 if r.trade_count > 0 else 0
                lines.append(
                    f"{icon} {r.date}  "
                    f"{sign}{r.pnl_pct:.2f}%  "
                    f"({sign}{r.pnl_krw:,.0f}원)  "
                    f"{win}/{total}승"
                )

            stats = self._repo.get_total_stats()
            sign = "+" if stats["total_pnl_krw"] >= 0 else ""
            lines.append(
                f"\n📊 누적 손익: {sign}{stats['total_pnl_krw']:,.0f} 원\n"
                f"🎯 전체 승률: {stats['win_rate']:.0%} "
                f"({stats['win_count']}승 {stats['loss_count']}패)"
            )
            self.send("\n".join(lines))
        except Exception as e:
            self.send(f"❌ 리포트 조회 실패: {e}")

    @staticmethod
    def _get_public_ip() -> str:
        """서버 공인 IP 조회. EC2 IMDSv2 → IMDSv1 → ipify 순으로 시도."""
        # EC2 IMDSv2 (토큰 방식, 보안 권장)
        try:
            token = requests.put(
                "http://169.254.169.254/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
                timeout=1,
            ).text
            return requests.get(
                "http://169.254.169.254/latest/meta-data/public-ipv4",
                headers={"X-aws-ec2-metadata-token": token},
                timeout=1,
            ).text.strip()
        except Exception:
            pass
        # EC2 IMDSv1 폴백
        try:
            return requests.get(
                "http://169.254.169.254/latest/meta-data/public-ipv4",
                timeout=1,
            ).text.strip()
        except Exception:
            pass
        # 로컬 개발 환경 폴백
        try:
            return requests.get("https://api.ipify.org", timeout=5).text.strip()
        except Exception:
            return "IP 조회 실패"

    def _cmd_dashboard(self, _args: str) -> None:
        pub_ip = self._get_public_ip()
        port = settings.DASHBOARD_PORT
        enabled = settings.DASHBOARD_ENABLED

        web_line = (
            f"🌐 웹 대시보드: http://{pub_ip}:{port}"
            if enabled
            else "🌐 웹 대시보드: 비활성화 (DASHBOARD_ENABLED=true 로 설정)"
        )

        self.send(
            f"🖥 <b>대시보드 접속 정보</b>\n"
            f"\n"
            f"📡 서버 공인 IP: <code>{pub_ip}</code>\n"
            f"{web_line}\n"
            f"\n"
            f"💻 SSH 접속:\n"
            f"<code>ssh ubuntu@{pub_ip}</code>\n"
            f"\n"
            f"📋 서비스 로그 확인:\n"
            f"<code>journalctl -u pochaco -f</code>"
        )

    # ------------------------------------------------------------------ #
    #  폴링 루프                                                            #
    # ------------------------------------------------------------------ #
    _COMMANDS: dict[str, str] = {
        "help":      "_cmd_help",
        "status":    "_cmd_status",
        "balance":   "_cmd_balance",
        "position":  "_cmd_position",
        "stop":      "_cmd_stop",
        "resume":    "_cmd_resume",
        "report":    "_cmd_report",
        "dashboard": "_cmd_dashboard",
    }

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message", {})
        if not msg:
            return

        # 허가된 chat_id 검증
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning(f"Telegram: 허가되지 않은 chat_id={chat_id}, 무시")
            return

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        # "/command@botname args" → "command", "args"
        parts = text.lstrip("/").split()
        cmd = parts[0].split("@")[0].lower()
        args = " ".join(parts[1:])

        method_name = self._COMMANDS.get(cmd)
        if method_name:
            logger.info(f"Telegram 명령어: /{cmd}")
            try:
                getattr(self, method_name)(args)
            except Exception as e:
                logger.error(f"Telegram 명령어 처리 오류 (/{cmd}): {e}")
                self.send(f"❌ 오류: {e}")
        else:
            self.send(f"❓ 알 수 없는 명령어: /{cmd}\n/help 로 목록 확인")

    def _poll_loop(self) -> None:
        logger.info("Telegram 봇 폴링 시작")
        while self._running:
            try:
                result = self._call(
                    "getUpdates",
                    timeout=25,
                    offset=self._offset,
                    limit=10,
                    allowed_updates=["message"],
                )
                for update in result.get("result", []):
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except Exception as e:
                logger.warning(f"Telegram 폴링 오류: {e}")
                time.sleep(5)

    def start(self) -> None:
        self._running = True
        thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="telegram-bot",
        )
        thread.start()
        logger.info(f"Telegram 봇 시작 (chat_id={self._chat_id})")

    def stop(self) -> None:
        self._running = False
