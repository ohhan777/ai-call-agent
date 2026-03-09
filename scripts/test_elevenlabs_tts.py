#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.openclaw/.env"), override=False)
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

base_dir = Path(__file__).resolve().parents[1]
out_path = base_dir / "output" / "tts" / "test_elevenlabs.mp3"
out_path.parent.mkdir(parents=True, exist_ok=True)

api_key = os.getenv("ELEVENLABS_API_KEY", "")
voice_id = os.getenv("ELEVENLABS_VOICE_ID", "")
model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

if not api_key or not voice_id:
    raise SystemExit("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID 설정이 필요합니다.")

master_name = os.getenv("MASTER_NAME", "")
master_title = os.getenv("MASTER_TITLE", "")
caller_name = os.getenv("CALLER_NAME", "")
caller_title = os.getenv("CALLER_TITLE", "비서")

if master_name and master_title:
    master_label = f"{master_name} {master_title}님"
elif master_name:
    master_label = f"{master_name}님"
else:
    master_label = "담당자님"
caller_part = f"{caller_name} " if caller_name else ""
text = f"안녕하세요. {master_label}의 {caller_part}{caller_title}입니다. 음성 합성 연결 테스트 중입니다."
url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
headers = {
    "xi-api-key": api_key,
    "Content-Type": "application/json",
    "Accept": "audio/mpeg",
}
payload = {
    "text": text,
    "model_id": model_id,
    "voice_settings": {
        "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.42")),
        "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY", "0.78")),
        "style": float(os.getenv("ELEVENLABS_STYLE", "0.18")),
        "use_speaker_boost": True,
    },
}

resp = requests.post(url, headers=headers, json=payload, timeout=45)
resp.raise_for_status()
out_path.write_bytes(resp.content)
print(f"OK: {out_path}")
