#!/usr/bin/env python3
"""AI 전화 대리 통화를 시작합니다.

예시:
  .venv/bin/python run_ai_call.py --to +821012345678 --task "내일 3시 회의 가능 여부 확인"
  .venv/bin/python run_ai_call.py --to +821012345678 --task "빅맥 세트 1개 포장 주문" --retry 2
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import requests
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv(os.path.expanduser("~/.openclaw/.env"), override=False)


def resolve_from_number(sid: str, token: str, from_number: str) -> str:
    if from_number:
        return from_number
    tmp_client = Client(sid, token)
    nums = tmp_client.incoming_phone_numbers.list(limit=1)
    if not nums:
        raise SystemExit("TWILIO_FROM_NUMBER 미설정 + 계정 번호 없음")
    return nums[0].phone_number


def place_call(
    sid: str,
    token: str,
    to: str,
    from_number: str,
    base_url: str,
    task: str,
    target_name: str,
    use_amd: bool,
    use_realtime: bool = False,
) -> tuple[str, str]:
    """통화를 생성하고 (call_id, call_sid)를 반환합니다."""
    call_id = uuid.uuid4().hex[:12]

    r = requests.post(
        f"{base_url}/internal/create/{call_id}",
        data={"target_name": target_name, "task": task},
        timeout=60,
    )
    r.raise_for_status()

    client = Client(sid, token)
    twiml_path = "twiml/start-realtime" if use_realtime else "twiml/start"
    call_kwargs: dict = {
        "to": to,
        "from_": from_number,
        "url": f"{base_url}/{twiml_path}/{call_id}",
        "method": "GET",
        "record": True,
        "recording_status_callback": f"{base_url}/callbacks/recording/{call_id}",
        "recording_status_callback_method": "POST",
        "status_callback": f"{base_url}/callbacks/status/{call_id}",
        "status_callback_method": "POST",
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
    }

    if use_amd:
        call_kwargs["machine_detection"] = "DetectMessageEnd"
        call_kwargs["async_amd"] = True
        call_kwargs["async_amd_status_callback"] = f"{base_url}/callbacks/amd/{call_id}"
        call_kwargs["async_amd_status_callback_method"] = "POST"

    call = client.calls.create(**call_kwargs)
    return call_id, call.sid


def wait_for_result(base_url: str, call_id: str, timeout: int = 120) -> dict:
    """통화 결과를 폴링합니다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/internal/report/{call_id}", timeout=10)
            state = r.json()
            status = state.get("status", "")
            if status in ("reported", "completed", "no-answer", "busy", "failed", "canceled"):
                return state
        except Exception:
            pass
        time.sleep(3)
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 전화 대리 통화 시작")
    parser.add_argument("--to", required=True, help="수신번호 (E.164)")
    parser.add_argument("--task", required=True, help="전달할 용건")
    parser.add_argument("--target-name", default="상대방", help="상대 호칭")
    parser.add_argument("--base-url", default=os.getenv("TWILIO_PUBLIC_BASE_URL", ""), help="공개 서버 URL")
    parser.add_argument("--from-number", default=os.getenv("TWILIO_FROM_NUMBER", ""), help="Twilio 발신번호")
    parser.add_argument("--retry", type=int, default=0, help="실패 시 재시도 횟수 (기본: 0)")
    parser.add_argument("--retry-delay", type=int, default=30, help="재시도 간격 초 (기본: 30)")
    parser.add_argument("--no-amd", action="store_true", help="음성사서함 감지(AMD) 비활성화")
    parser.add_argument("--realtime", action="store_true", default=True, help="Realtime API 모드 (기본값)")
    parser.add_argument("--gather", action="store_true", help="Gather 모드 (기존 TTS 방식)")
    parser.add_argument("--wait", action="store_true", default=True, help="통화 완료까지 대기 후 결과 출력 (기본값)")
    parser.add_argument("--no-wait", action="store_true", help="전화 걸고 바로 종료")
    args = parser.parse_args()

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise SystemExit("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN 필요")

    base_url = args.base_url.rstrip("/")
    if not base_url:
        raise SystemExit("--base-url 또는 TWILIO_PUBLIC_BASE_URL 필요")

    from_number = resolve_from_number(sid, token, args.from_number)
    use_amd = not args.no_amd
    use_realtime = args.realtime and not args.gather
    use_wait = args.wait and not args.no_wait

    max_attempts = 1 + args.retry
    for attempt in range(1, max_attempts + 1):
        call_id, call_sid = place_call(
            sid=sid,
            token=token,
            to=args.to,
            from_number=from_number,
            base_url=base_url,
            task=args.task,
            target_name=args.target_name,
            use_amd=use_amd,
            use_realtime=use_realtime,
        )

        mode = "realtime" if use_realtime else "gather"
        print(f"[{attempt}/{max_attempts}] 발신")
        print(f"  call_id:    {call_id}")
        print(f"  call_sid:   {call_sid}")
        print(f"  from:       {from_number}")
        print(f"  mode:       {mode}")
        print(f"  AMD:        {'on' if use_amd else 'off'}")
        print(f"  report_api: {base_url}/internal/report/{call_id}")

        if not use_wait and attempt == max_attempts:
            break

        if use_wait or attempt < max_attempts:
            print("  결과 대기 중...")
            result = wait_for_result(base_url, call_id)
            status = result.get("status", "unknown")
            outcome = result.get("call_outcome", status)
            print(f"  결과: {outcome}")

            if status in ("reported", "completed"):
                if result.get("report"):
                    print(f"\n{result['report']}")
                break

            if attempt < max_attempts:
                retryable = status in ("no-answer", "busy", "failed") or outcome == "voicemail"
                if retryable:
                    print(f"  {args.retry_delay}초 후 재시도...")
                    time.sleep(args.retry_delay)
                else:
                    print(f"  재시도 불가 상태: {status}")
                    break


if __name__ == "__main__":
    main()
