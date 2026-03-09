#!/usr/bin/env python3
"""텍스트 모드로 AI 전화 비서 대화를 테스트합니다.

실제 전화 없이 터미널에서 대화 흐름을 확인할 수 있습니다.

예시:
  .venv/bin/python text_test.py --task "내일 3시 회의 가능 여부 확인 부탁드립니다."
  .venv/bin/python text_test.py --task "빅맥 세트 1개 포장 주문해 주세요." --target-name "맥도날드 직원"
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(os.path.expanduser("~/.openclaw/.env"), override=False)

OPENAI_MODEL = os.getenv("OPENAI_CALL_MODEL", "gpt-5-mini")


def build_system_prompt(state: dict[str, Any]) -> str:
    from ai_call_server import build_system_prompt as _build
    return _build(state)


def create_state(task: str, target_name: str) -> dict[str, Any]:
    return {
        "call_id": uuid.uuid4().hex[:12],
        "created_at": int(time.time()),
        "target_name": target_name,
        "task": task,
        "history": [],
        "status": "text_test",
    }


def _clean(s: str) -> str:
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _build_messages(state: dict[str, Any], user_text: str) -> list[dict[str, str]]:
    system_prompt = build_system_prompt(state)
    messages: list[dict[str, str]] = [{"role": "system", "content": _clean(system_prompt)}]
    for turn in state.get("history", [])[-20:]:
        role = "assistant" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "content": _clean(turn["text"])})
    messages.append({"role": "user", "content": _clean(user_text)})
    return messages


def generate_reply_stream(client: OpenAI, state: dict[str, Any], user_text: str) -> str:
    """스트리밍으로 LLM 응답을 실시간 출력하고 전체 텍스트를 반환합니다."""
    normalized = re.sub(r"\s+", "", user_text)
    if not normalized:
        return "말씀이 잘 안 들렸습니다. 한 번만 다시 말씀해 주시겠어요?"

    messages = _build_messages(state, user_text)
    t0 = time.time()
    sys.stdout.write("\n[비서] ")
    sys.stdout.flush()

    full_text = ""
    stream = client.chat.completions.create(model=OPENAI_MODEL, messages=messages, stream=True)
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices[0].delta.content else ""
        clean_delta = delta.replace("[END_CALL]", "")
        sys.stdout.write(clean_delta)
        sys.stdout.flush()
        full_text += delta

    elapsed = time.time() - t0
    sys.stdout.write(f"  ({elapsed:.1f}s)\n\n")
    sys.stdout.flush()
    return full_text.strip() or "네, 말씀 감사합니다."


def append_turn(state: dict[str, Any], role: str, text: str) -> None:
    state["history"].append({"role": role, "text": text, "ts": int(time.time())})


def clean_for_display(text: str) -> tuple[str, bool]:
    should_end = "[END_CALL]" in text
    text = text.replace("[END_CALL]", "").strip()
    return text, should_end


def run_text_session(task: str, target_name: str) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    state = create_state(task, target_name)

    print("=" * 60)
    print("AI 전화 비서 텍스트 테스트")
    print(f"  용건: {task}")
    print(f"  상대: {target_name}")
    print(f"  모델: {OPENAI_MODEL}")
    print("=" * 60)

    # 비서의 첫 인사 생성 (스트리밍)
    opening = generate_reply_stream(client, state, "여보세요?")
    opening_clean, _ = clean_for_display(opening)
    append_turn(state, "assistant", opening_clean)

    while True:
        try:
            user_input = input(f"[{target_name}] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n--- 테스트 종료 (사용자 중단) ---")
            break

        if not user_input:
            continue

        if user_input.lower() in ("/quit", "/q", "/exit"):
            print("\n--- 테스트 종료 ---")
            break

        append_turn(state, "user", user_input)

        reply = generate_reply_stream(client, state, user_input)
        display_text, should_end = clean_for_display(reply)
        append_turn(state, "assistant", display_text)

        if should_end:
            print("--- 비서가 통화를 종료했습니다 ---")
            break

    # 대화 요약 출력
    print("\n" + "=" * 60)
    print("대화 기록")
    print("=" * 60)
    for turn in state["history"]:
        speaker = "비서" if turn["role"] == "assistant" else target_name
        print(f"  [{speaker}] {turn['text']}")
    print(f"\n총 {len(state['history'])}턴")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 전화 비서 텍스트 테스트")
    parser.add_argument("--task", required=True, help="전달할 용건")
    parser.add_argument("--target-name", default="상대방", help="상대 호칭 (기본: 상대방)")
    args = parser.parse_args()

    run_text_session(args.task, args.target_name)


if __name__ == "__main__":
    main()
