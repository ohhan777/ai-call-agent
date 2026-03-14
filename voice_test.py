#!/usr/bin/env python3
"""마이크/스피커로 AI 전화 비서 음성 대화를 테스트합니다.

OpenAI Realtime API에 직접 연결하여 실제 전화 없이 음성 대화 흐름을 확인할 수 있습니다.

예시:
  python voice_test.py --task "내일 3시 회의 가능 여부 확인 부탁드립니다."
  python voice_test.py --task "빅맥 세트 1개 포장 주문해 주세요." --target-name "맥도날드 직원"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import struct
import sys
import time
import uuid
from typing import Any

import pyaudio
import websockets
from dotenv import load_dotenv

load_dotenv()
load_dotenv(os.path.expanduser("~/.openclaw/.env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voice_test")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-1.5")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "coral")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# Audio config — PCM16 24kHz mono (OpenAI Realtime API native format)
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit
CHUNK_DURATION_MS = 20  # 20ms chunks
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_DURATION_MS // 1000  # 480 samples
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH  # 960 bytes


def create_state(task: str, target_name: str) -> dict[str, Any]:
    return {
        "call_id": uuid.uuid4().hex[:12],
        "created_at": int(time.time()),
        "target_name": target_name,
        "task": task,
        "history": [],
        "status": "voice_test",
    }


def append_turn(state: dict[str, Any], role: str, text: str) -> None:
    state["history"].append({"role": role, "text": text, "ts": int(time.time())})


async def run_voice_session(task: str, target_name: str) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    # Import build_realtime_system_prompt from ai_call_server
    from ai_call_server import build_realtime_system_prompt

    state = create_state(task, target_name)
    call_ended = asyncio.Event()

    print("=" * 60)
    print("AI 전화 비서 음성 테스트")
    print(f"  목표: {task}")
    print(f"  상대: {target_name}")
    print(f"  모델: {OPENAI_REALTIME_MODEL}")
    print(f"  음성: {OPENAI_REALTIME_VOICE}")
    print("=" * 60)
    print("마이크로 말하세요. Ctrl+C로 종료할 수 있습니다.\n")

    # --- PyAudio setup ---
    pa = pyaudio.PyAudio()

    mic_stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    speaker_stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        output=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    print("[시스템] 오디오 장치 초기화 완료")

    # --- Connect to OpenAI Realtime API ---
    ws_url = f"{OPENAI_REALTIME_URL}?model={OPENAI_REALTIME_MODEL}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            print("[시스템] OpenAI Realtime API 연결 완료")

            # --- Session config ---
            session_config = {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": build_realtime_system_prompt(state),
                    "voice": OPENAI_REALTIME_VOICE,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.55,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 400,
                    },
                    "tools": [
                        {
                            "type": "function",
                            "name": "end_call",
                            "description": "통화를 종료합니다. 목표가 달성/거절/보류되었을 때 호출합니다.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "reason": {
                                        "type": "string",
                                        "enum": ["completed", "rejected", "deferred"],
                                        "description": "종료 사유",
                                    }
                                },
                                "required": ["reason"],
                            },
                        }
                    ],
                },
            }
            await ws.send(json.dumps(session_config))
            print("[시스템] 세션 설정 완료\n")

            # --- 3-second silence: AI speaks first ---
            async def maybe_speak_first() -> None:
                await asyncio.sleep(1.5)
                if call_ended.is_set():
                    return
                logger.info("3초 침묵, AI가 먼저 말을 시작합니다")
                try:
                    await ws.send(json.dumps({
                        "type": "response.create",
                        "response": {"modalities": ["text", "audio"]},
                    }))
                except Exception:
                    pass

            speak_first_task = asyncio.create_task(maybe_speak_first())

            # --- Mic → API ---
            async def mic_to_api() -> None:
                loop = asyncio.get_event_loop()
                try:
                    while not call_ended.is_set():
                        # Read mic in a thread to avoid blocking the event loop
                        data = await loop.run_in_executor(
                            None, mic_stream.read, CHUNK_SAMPLES, False,
                        )
                        if call_ended.is_set():
                            break
                        encoded = base64.b64encode(data).decode("ascii")
                        await ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": encoded,
                        }))
                except Exception as exc:
                    if not call_ended.is_set():
                        logger.error("마이크 오류: %s", exc)
                    call_ended.set()

            # --- API → Speaker ---
            async def api_to_speaker() -> None:
                end_call_pending = False
                try:
                    async for raw in ws:
                        if call_ended.is_set():
                            break
                        msg = json.loads(raw)
                        etype = msg.get("type", "")

                        # Audio output → speaker
                        if etype == "response.audio.delta":
                            delta = msg.get("delta", "")
                            if delta:
                                pcm_data = base64.b64decode(delta)
                                speaker_stream.write(pcm_data)

                        # Assistant transcript
                        elif etype == "response.audio_transcript.done":
                            text = msg.get("transcript", "").strip()
                            if text:
                                append_turn(state, "assistant", text)
                                print(f"\r[비서] {text}")
                                sys.stdout.flush()

                        # User transcript
                        elif etype == "conversation.item.input_audio_transcription.completed":
                            text = msg.get("transcript", "").strip()
                            if text:
                                append_turn(state, "user", text)
                                print(f"\r[{target_name}] {text}")
                                sys.stdout.flush()

                        # Speech detected → cancel speak_first if pending
                        elif etype == "input_audio_buffer.speech_started":
                            if not speak_first_task.done():
                                speak_first_task.cancel()

                        # Function call: end_call
                        elif etype == "response.function_call_arguments.done":
                            fn_name = msg.get("name", "")
                            if fn_name == "end_call":
                                args = json.loads(msg.get("arguments", "{}"))
                                reason = args.get("reason", "completed")
                                state["call_outcome"] = reason
                                end_call_pending = True
                                logger.info("end_call 호출: reason=%s", reason)

                                # Send function result and request closing response
                                await ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": msg.get("call_id", ""),
                                        "output": json.dumps({"status": "ok", "reason": reason}),
                                    },
                                }))
                                await ws.send(json.dumps({
                                    "type": "response.create",
                                    "response": {"modalities": ["text", "audio"]},
                                }))

                        # Response done — end call if end_call was invoked
                        elif etype == "response.done":
                            resp = msg.get("response", {})
                            output = resp.get("output", [])
                            has_end_call = any(
                                item.get("type") == "function_call" and item.get("name") == "end_call"
                                for item in output
                            )
                            if has_end_call:
                                logger.info("end_call 함수 응답 완료, 마지막 인사 대기 중...")
                            elif end_call_pending:
                                logger.info("마지막 인사 완료, 통화 종료")
                                await asyncio.sleep(1) # wait for audio to finish streaming
                                call_ended.set()

                        # Session created confirmation
                        elif etype == "session.created":
                            logger.info("세션 생성됨: %s", msg.get("session", {}).get("id", ""))

                        # Error
                        elif etype == "error":
                            logger.error("OpenAI 오류: %s", msg.get("error"))

                except websockets.exceptions.ConnectionClosed:
                    logger.info("OpenAI WebSocket 연결 종료")
                    call_ended.set()

            # Run mic and speaker tasks concurrently
            await asyncio.gather(
                mic_to_api(),
                api_to_speaker(),
            )

    except Exception as exc:
        logger.error("연결 오류: %s", exc)
    finally:
        # Cleanup audio
        mic_stream.stop_stream()
        mic_stream.close()
        speaker_stream.stop_stream()
        speaker_stream.close()
        pa.terminate()

    # Print conversation log
    print("\n" + "=" * 60)
    print("대화 기록")
    print("=" * 60)
    for turn in state["history"]:
        speaker = "비서" if turn["role"] == "assistant" else target_name
        print(f"  [{speaker}] {turn['text']}")
    print(f"\n총 {len(state['history'])}턴")
    if state.get("call_outcome"):
        print(f"종료 사유: {state['call_outcome']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 전화 비서 음성 테스트")
    parser.add_argument("--task", required=True, help="달성할 목표")
    parser.add_argument("--target-name", default="상대방", help="상대 호칭 (기본: 상대방)")
    args = parser.parse_args()

    try:
        asyncio.run(run_voice_session(args.task, args.target_name))
    except KeyboardInterrupt:
        print("\n\n--- 테스트 종료 (사용자 중단) ---")


if __name__ == "__main__":
    main()
