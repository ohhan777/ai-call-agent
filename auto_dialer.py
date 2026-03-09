#!/usr/bin/env python3
"""Twilio 자동 발신 스크립트

예시:
  .venv/bin/python auto_dialer.py --to +821012345678 --message "안녕하세요. 자동 안내 전화입니다."
  .venv/bin/python auto_dialer.py --to +821011111111 +821022222222 --delay-sec 5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Iterable

from dotenv import load_dotenv
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse


def load_env() -> None:
    """우선순위: 프로젝트 .env -> ~/.openclaw/.env"""
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
    load_dotenv(os.path.expanduser("~/.openclaw/.env"), override=False)


def get_client_and_number() -> tuple[Client, str]:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")

    missing = [
        key
        for key, value in {
            "TWILIO_ACCOUNT_SID": sid,
            "TWILIO_AUTH_TOKEN": token,
            "TWILIO_FROM_NUMBER": from_number,
        }.items()
        if not value
    ]
    if missing:
        print("[오류] 필수 환경변수가 없습니다:", ", ".join(missing), file=sys.stderr)
        print("       ~/.openclaw/.env 또는 ./twilio/.env에 추가해 주세요.", file=sys.stderr)
        sys.exit(1)

    return Client(sid, token), from_number  # type: ignore[arg-type]


def build_twiml(message: str, language: str, voice: str) -> str:
    response = VoiceResponse()
    response.say(message=message, language=language, voice=voice)
    return str(response)


def place_calls(
    client: Client,
    from_number: str,
    recipients: Iterable[str],
    twiml: str,
    delay_sec: float,
    status_callback: str | None,
) -> int:
    success = 0
    recipients = list(recipients)

    for idx, to_number in enumerate(recipients, start=1):
        try:
            kwargs = {
                "to": to_number,
                "from_": from_number,
                "twiml": twiml,
            }
            if status_callback:
                kwargs["status_callback"] = status_callback
                kwargs["status_callback_method"] = "POST"
                kwargs["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]

            call = client.calls.create(**kwargs)
            success += 1
            print(f"[{idx}/{len(recipients)}] 발신 요청 성공: {to_number} | Call SID: {call.sid}")
        except Exception as exc:
            print(f"[{idx}/{len(recipients)}] 발신 요청 실패: {to_number} | {exc}", file=sys.stderr)

        if idx < len(recipients) and delay_sec > 0:
            time.sleep(delay_sec)

    return success


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twilio 자동 전화 발신기")
    parser.add_argument(
        "--to",
        nargs="+",
        required=True,
        help="수신 번호(E.164). 여러 개 가능. 예: +821012345678 +821055566677",
    )
    parser.add_argument(
        "--message",
        default="안녕하세요. 자동 안내 전화입니다.",
        help="전화 연결 시 읽어줄 문구",
    )
    parser.add_argument(
        "--language",
        default="ko-KR",
        help="TTS 언어 코드 (기본: ko-KR)",
    )
    parser.add_argument(
        "--voice",
        default="alice",
        help="Twilio voice (기본: alice)",
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=0,
        help="여러 번호 발신 시 각 요청 사이 대기 시간(초)",
    )
    parser.add_argument(
        "--status-callback",
        default=None,
        help="콜 상태 웹훅 URL(선택)",
    )
    return parser.parse_args()


def main() -> None:
    load_env()
    args = parse_args()

    client, from_number = get_client_and_number()
    twiml = build_twiml(args.message, args.language, args.voice)

    total = len(args.to)
    success = place_calls(
        client=client,
        from_number=from_number,
        recipients=args.to,
        twiml=twiml,
        delay_sec=args.delay_sec,
        status_callback=args.status_callback,
    )

    print(f"완료: {success}/{total} 건 발신 요청 성공")
    if success == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
