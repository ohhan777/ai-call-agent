#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import asyncio

import requests
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from twilio.twiml.voice_response import Connect, Gather, VoiceResponse

load_dotenv(os.path.expanduser("~/.openclaw/.env"), override=False)
load_dotenv(Path(__file__).with_name(".env"), override=False)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai_call")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).parent
OUT_DIR = APP_DIR / "output" / "calls"
OUT_DIR.mkdir(parents=True, exist_ok=True)
TTS_DIR = APP_DIR / "output" / "tts"
TTS_DIR.mkdir(parents=True, exist_ok=True)

OPENAI_MODEL = os.getenv("OPENAI_CALL_MODEL", "gpt-5-mini")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
PUBLIC_BASE_URL = os.getenv("TWILIO_PUBLIC_BASE_URL", "").rstrip("/")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
MASTER_NAME = os.getenv("MASTER_NAME", "")
MASTER_TITLE = os.getenv("MASTER_TITLE", "")
CALLER_NAME = os.getenv("CALLER_NAME", "")
CALLER_TITLE = os.getenv("CALLER_TITLE", "비서")


def _master_label() -> str:
    """'오한 박사님' 같은 호칭 생성."""
    if MASTER_NAME and MASTER_TITLE:
        return f"{MASTER_NAME} {MASTER_TITLE}님"
    if MASTER_NAME:
        return f"{MASTER_NAME}님"
    return "담당자님"


def _caller_intro() -> str:
    """'오한 박사님의 인공지능 비서입니다' 같은 소개 문구 생성."""
    master = _master_label()
    caller_part = f"{CALLER_NAME} " if CALLER_NAME else ""
    return f"{master}의 {caller_part}{CALLER_TITLE}입니다"

def _is_person_target(target: str) -> bool:
    """상대방이 사람(지인)인지 가게/업체인지 판별."""
    person_suffixes = (
        "과장", "부장", "대리", "사원", "팀장", "실장", "본부장",
        "박사", "책임", "선임", "연구원", "교수", "선생",
        "사장", "이사", "전무", "상무", "회장",
        "님",
    )
    store_keywords = ("직원", "가게", "매장", "식당", "집", "점", "마트", "약국", "병원", "센터")
    lower = target.strip()
    if any(lower.endswith(k) for k in store_keywords):
        return False
    if any(lower.endswith(s) for s in person_suffixes):
        return True
    # 2~4글자 한글 이름 패턴 (예: 김철수)
    import re as _re
    if _re.fullmatch(r"[가-힣]{2,4}", lower):
        return True
    return False


def _build_opening_text(target: str, task: str) -> str:
    """TTS용 오프닝 문장 생성. 사람이면 자기소개 포함, 가게면 바로 본론."""
    task_text = (task or "전달드릴 말씀이 있습니다.").strip()
    intro = _caller_intro()
    # 이미 자기소개가 포함된 경우 제거
    task_text = re.sub(rf"^안녕하세요\.?\s*{re.escape(intro)}\.?\s*", "", task_text)
    if not task_text:
        task_text = "전달드릴 말씀이 있습니다."

    if _is_person_target(target):
        return f"{target}님 안녕하세요. {intro}. {task_text}"
    else:
        return f"안녕하세요. {task_text}"


MAX_TURNS = int(os.getenv("MAX_CALL_TURNS", "12"))

# Realtime API config
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-1.5")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "coral")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI(title="AI Call Agent")

# Serve generated TTS files at /static/tts/
app.mount("/static/tts", StaticFiles(directory=str(TTS_DIR)), name="tts_static")


def now_ts() -> int:
    return int(time.time())


def call_file(call_id: str) -> Path:
    return OUT_DIR / f"{call_id}.json"


def load_state(call_id: str) -> dict[str, Any]:
    p = call_file(call_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "call_id": call_id,
        "created_at": now_ts(),
        "target_name": "상대방",
        "task": "",
        "history": [],
        "status": "new",
    }


def save_state(call_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = now_ts()
    call_file(call_id).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_turn(state: dict[str, Any], role: str, text: str) -> None:
    state.setdefault("history", []).append({"role": role, "text": text, "ts": now_ts()})


def _common_prompt_body(target: str, end_method: str = "[END_CALL]") -> str:
    """build_system_prompt / build_realtime_system_prompt 공통 본문."""
    if end_method == "end_call":
        end_rule = "- 상대방 말이 끝나면 적절히 대응하고, 시간에 맞는 인사를 한 뒤 end_call을 호출한다.\n"
    else:
        end_rule = (
            "- 상대방 말이 끝나면 적절히 대응하고, 시간에 맞는 인사를 한 뒤 종료한다.\n"
            "- 종료할 때만 문장 끝에 [END_CALL]을 붙인다.\n"
        )

    master = _master_label()
    caller_intro = _caller_intro()

    return (
        "## 역할\n"
        f"- 나는 {master}의 비서이다. {master}을 대신해 {target}에게 전화를 걸었다.\n"
        f"- 내가 수집한 정보는 {master}에게 전달하는 것이다. 상대방에게 전달하는 것이 아니다.\n"
        f"- 상대방이 '{master}에게 전해달라'고 하면 '네, {master}께 전달드리겠습니다'로 답한다.\n"
        "- 절대 상대방 역할(점원·직원·안내자)을 대신하지 않는다.\n"
        "- '준비해드리겠습니다', '확인해드리겠습니다', '더 필요하신 게 있으신가요' 등 서비스 제공자 표현 금지.\n"
        "- 주문·예약 전화에서는 고객으로서 요청하고, 상대의 안내를 따른다.\n\n"
        "## 말투\n"
        "- 실제 사람 비서처럼 자연스럽게 말한다. 로봇이나 고객센터 상담원처럼 말하지 않는다.\n"
        "- 한 번에 1~2문장. 짧게, 핵심만.\n"
        "- 상대방의 분위기와 톤에 맞춘다. 상대가 편하게 말하면 나도 편하게 대응한다.\n"
        "- 상대방의 말에 자연스럽게 반응한다 (예: '아 네', '그렇군요', '알겠습니다').\n"
        "- '도움이 필요하시면 언제든지 말씀해 주세요' 같은 고객센터식 멘트 절대 금지.\n"
        "- 상대가 시간이 필요하거나 '잠깐', '멈춰봐' 등을 말하면 조용히 기다린다. 말을 끊거나 추가 설명하지 않는다.\n"
        "- 상대와 말이 겹치면 당황하지 말고 하던 말을 간결하게 마무리한 뒤 상대의 말을 듣는다.\n"
        "- 짧은 추임새(네, 아, 응, 여보세요)에는 말을 멈추지 않고 이어간다.\n"
        "- 번호 목록·긴 나열 금지. AI 티 나는 표현 금지.\n"
        "- 시간은 고유어: 한시, 두시, 세시, 네시, 다섯시 … 열두시. (예: '세시' O, '3시'·'사시' X)\n"
        "- 날짜는 자연스럽게: '삼월 십일' O, '3/10' X\n\n"
        "## 자기소개\n"
        "- 상대가 사람(이름·직함: 김과장, 이부장, 박대리, 최박사, 정책임, 한선임, 윤연구원 등)\n"
        f"  → 반드시 자기소개: '{target}{"" if target.endswith("님") else "님"} 안녕하세요. {caller_intro}.'\n"
        "- 상대가 가게·업체·직원(맥도날드, 치킨집, 피자헛 등)\n"
        "  → 자기소개 없이 바로 본론: '안녕하세요. 순살 치킨 반반 하나 배달 부탁드려요.'\n\n"
        "## 대화 흐름\n"
        "- 첫 인사에서는 자기소개만 하고, 용건은 상대가 응답한 뒤 하나씩 꺼낸다.\n"
        "- 목표에 여러 용건이 있으면 하나를 먼저 물어보고, 답을 받은 뒤 다음 용건으로 넘어간다.\n"
        "- 상대가 먼저 인사하거나 말을 걸면 그에 맞춰 자연스럽게 응답한 뒤 용건으로 넘어간다.\n"
        "- 상대가 침묵하면 내가 먼저 말을 시작한다.\n"
        "- 상대의 질문에는 목표 범위 내에서 바로 답한다.\n"
        "- 모르는 내용은 '확인 후 다시 연락드리겠습니다'.\n"
        "- 상대가 선택지를 주면 합리적으로 선택해서 답한다.\n"
        "- 상대가 지시하면('전달해줘', '알려줘') 바로 수락하고 간결하게 확인한다. 내용을 길게 되풀이하지 않는다.\n\n"
        "## 종료\n"
        "- 절대 먼저 끊지 않는다. 'thank you', '고마워' 등 가벼운 인사에 바로 종료하지 않는다.\n"
        "- 아직 달성하지 못한 목표가 남아 있으면 끊지 않고 계속 대화한다.\n"
        "- 상대가 추가로 말하면 끊지 말고 대응한다.\n"
        "- 상대가 명확하게 끝내려는 신호('끊을게', '수고해', 'Good luck', '그래 알겠어' + 침묵)를 보낸 뒤에만 종료한다.\n"
        + end_rule +
        "  낮: '감사합니다. 좋은 하루 되세요.'\n"
        "  저녁: '감사합니다. 좋은 저녁 되세요.'\n"
        "  밤: '감사합니다. 좋은 밤 되세요.'\n\n"
    )


def build_system_prompt(state: dict[str, Any]) -> str:
    target = state.get("target_name", "상대방")
    return (
        f"당신은 {_master_label()}을 대신해 전화를 건 사람이다. 실제 사람처럼 자연스럽게 통화한다.\n\n"
        + _common_prompt_body(target, end_method="[END_CALL]")
        + f"[상대] {target}\n"
        f"[목표]\n{state.get('task','') or '목표 미지정'}\n"
    )


def generate_reply(state: dict[str, Any], user_text: str) -> str:
    normalized = re.sub(r"\s+", "", user_text)
    if normalized in ["(음성인식실패/무응답)", "", "(무응답)"]:
        return "말씀이 잘 안 들렸습니다. 한 번만 다시 말씀해 주시겠어요?"

    system_prompt = build_system_prompt(state)
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for turn in state.get("history", [])[-20:]:
        role = "assistant" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_text})

    t0 = time.time()
    try:
        resp = openai_client.with_options(timeout=12.0).chat.completions.create(
            model=OPENAI_MODEL, messages=messages,
        )
        text = (resp.choices[0].message.content or "").strip()
        elapsed = time.time() - t0
        logger.info("[TIMING] LLM %.2fs | call=%s turns=%d | %s",
                    elapsed, state.get("call_id", "?"), len(state.get("history", [])), text[:80])
        return text or "네, 말씀 감사합니다."
    except Exception as exc:
        elapsed = time.time() - t0
        logger.error("[TIMING] LLM FAIL %.2fs | call=%s | %s", elapsed, state.get("call_id", "?"), exc)
        return f"네, 말씀 감사합니다. 방금 말씀해주신 내용은 {_master_label()}께 전달드리겠습니다. [END_CALL]"


def clean_for_tts(text: str) -> tuple[str, bool]:
    should_end = "[END_CALL]" in text
    text = text.replace("[END_CALL]", "").strip()
    if not text:
        text = "감사합니다. 좋은 하루 보내세요."
    return text, should_end


def closing_by_local_time() -> str:
    """시간대에 맞는 인사말 반환."""
    import datetime
    hour = datetime.datetime.now().hour
    if hour < 18:
        return "좋은 하루 되세요."
    elif hour < 21:
        return "좋은 저녁 되세요."
    else:
        return "좋은 밤 되세요."


def build_transcript_from_history(state: dict[str, Any]) -> str:
    """대화 history에서 화자 분리된 전사를 생성합니다."""
    target = state.get("target_name", "상대방")
    lines = []
    for turn in state.get("history", []):
        speaker = "비서" if turn["role"] == "assistant" else target
        lines.append(f"[{speaker}] {turn['text']}")
    return "\n".join(lines)


def transcribe_recording(audio_path: Path) -> str:
    with audio_path.open("rb") as f:
        tr = openai_client.audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=f,
            response_format="text",
        )
    return tr if isinstance(tr, str) else str(tr)


def determine_call_outcome(state: dict[str, Any]) -> str:
    """통화 결과를 판단합니다."""
    if state.get("amd_result") == "machine":
        return "voicemail"
    status = state.get("status", "")
    if status in ("no-answer", "busy", "failed"):
        return status
    history = state.get("history", [])
    if not history:
        return "no_conversation"
    return "completed"


def summarize_call(state: dict[str, Any], transcript: str) -> str:
    history_transcript = build_transcript_from_history(state)
    outcome = determine_call_outcome(state)
    prompt = (
        f"다음은 {_master_label()}의 {CALLER_TITLE}가 대리한 전화 통화 내용이다.\n"
        f"{_master_label()}에게 보고하는 간결한 형식으로 한국어 요약해줘.\n\n"
        "형식:\n"
        f"## 통화 결과: {outcome}\n\n"
        "### 목표 달성 현황\n- (각 목표별 달성 여부)\n\n"
        "### 상대방 핵심 답변\n- (상대방이 말한 내용만, 비서 발화 제외)\n\n"
        "### 후속 조치\n- (필요한 다음 행동)\n\n"
        "불확실한 내용은 추정하지 말 것.\n\n"
        f"[목표]\n{state.get('task','')}\n\n"
        f"[화자 분리 대화 기록]\n{history_transcript}\n\n"
        f"[녹음 전사 (참고용)]\n{transcript[:8000]}"
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


def try_download_recording(recording_url: str, call_id: str) -> Path | None:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return None

    mp3_url = recording_url + ".mp3" if not recording_url.endswith(".mp3") else recording_url
    out = OUT_DIR / f"{call_id}.mp3"
    r = requests.get(mp3_url, auth=(sid, token), timeout=60)
    if r.status_code >= 400:
        return None
    out.write_bytes(r.content)
    return out


def elevenlabs_tts_save(text: str, file_id: str) -> tuple[str | None, str | None]:
    """Generate TTS via ElevenLabs and return (public_url, error_message)."""
    api_key = os.getenv("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY)
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", ELEVENLABS_VOICE_ID)
    if not api_key or not voice_id:
        return None, "missing_api_key_or_voice_id"

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
        "voice_settings": {
            "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.42")),
            "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY", "0.78")),
            "style": float(os.getenv("ELEVENLABS_STYLE", "0.18")),
            "use_speaker_boost": True,
        },
    }

    t0 = time.time()
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        out_path = TTS_DIR / f"{file_id}.mp3"
        out_path.write_bytes(r.content)
        elapsed = time.time() - t0
        logger.info("[TIMING] TTS %.2fs | file=%s | %d bytes", elapsed, file_id, len(r.content))
        return f"{PUBLIC_BASE_URL}/static/tts/{file_id}.mp3", None
    except Exception as exc:
        elapsed = time.time() - t0
        logger.error("[TIMING] TTS FAIL %.2fs | file=%s | %s", elapsed, file_id, exc)
        return None, str(exc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/twiml/start/{call_id}")
def twiml_start(call_id: str) -> PlainTextResponse:
    start_t0 = time.time()
    logger.info("[START] call=%s", call_id)
    state = load_state(call_id)
    state["status"] = "in_progress"
    save_state(call_id, state)

    target = state.get("target_name", "상대방")
    opening_text = _build_opening_text(target, state.get("task", ""))

    vr = VoiceResponse()
    cache = state.get("tts_cache", {}) if isinstance(state.get("tts_cache"), dict) else {}
    opening_tts_url = cache.get("opening")
    if not opening_tts_url:
        opening_tts_url, opening_tts_err = elevenlabs_tts_save(opening_text, f"{call_id}_opening")
        if opening_tts_err:
            state["opening_tts_error"] = opening_tts_err
    state["opening_tts_provider"] = "elevenlabs" if opening_tts_url else "twilio_say"
    save_state(call_id, state)

    if opening_tts_url:
        vr.play(opening_tts_url)
    else:
        vr.say(opening_text, language="ko-KR", voice="alice")

    gather = Gather(
        input="speech",
        action=f"{PUBLIC_BASE_URL}/twiml/turn/{call_id}",
        method="POST",
        speech_timeout="2",
        timeout=4,
        language="ko-KR",
        hints="네, 아니요, 가능합니다, 안됩니다, 잠시만요, 여보세요",
    )
    vr.append(gather)
    vr.redirect(f"{PUBLIC_BASE_URL}/twiml/turn/{call_id}", method="POST")
    logger.info("[START END] call=%s | %.2fs", call_id, time.time() - start_t0)
    return PlainTextResponse(str(vr), media_type="application/xml")


@app.get("/twiml/turn/{call_id}")
async def twiml_turn_get(call_id: str) -> PlainTextResponse:
    # Twilio can hit this path via GET after Gather timeout depending on flow.
    return await twiml_turn(call_id=call_id, SpeechResult="")


@app.post("/twiml/turn/{call_id}")
async def twiml_turn(call_id: str, SpeechResult: str = Form(default="")) -> PlainTextResponse:
    turn_t0 = time.time()
    state = load_state(call_id)

    user_said = (SpeechResult or "").strip() or "(음성 인식 실패/무응답)"
    logger.info("[TURN START] call=%s | user=%s", call_id, user_said[:60])
    append_turn(state, "user", user_said)

    ai_text = generate_reply(state, user_said)
    speak_text, should_end = clean_for_tts(ai_text)
    if should_end and not any(k in speak_text for k in ["좋은 하루", "좋은 저녁", "좋은 밤"]):
        speak_text = f"{speak_text} {closing_by_local_time()}".strip()
    append_turn(state, "assistant", speak_text)

    turns = len(state.get("history", []))
    if turns >= MAX_TURNS:
        should_end = True
        if not any(k in speak_text for k in ["전달하겠", "전달드리겠", "마치겠"]):
            speak_text = speak_text + " 확인 감사합니다. 이만 통화를 마치겠습니다."
        logger.info("call %s hit max turns (%d), ending", call_id, MAX_TURNS)

    save_state(call_id, state)

    vr = VoiceResponse()

    # If ElevenLabs configured, prefer pre-generated cache to reduce turn latency.
    cache = state.get("tts_cache", {}) if isinstance(state.get("tts_cache"), dict) else {}
    tts_url = None
    tts_err = None
    if "말씀이 잘 안 들렸습니다" in speak_text:
        tts_url = cache.get("fast_reprompt")
    elif "오늘 좋은 하루 보내세요" in speak_text and "전달" in speak_text:
        tts_url = cache.get("fast_end") or cache.get("fast_end_alt")

    if not tts_url:
        tts_url, tts_err = elevenlabs_tts_save(speak_text, f"{call_id}_{turns}")

    state["last_tts_provider"] = "elevenlabs" if tts_url else "twilio_say"
    if tts_err:
        state["last_tts_error"] = tts_err
    save_state(call_id, state)

    if tts_url:
        vr.play(tts_url)
    else:
        vr.say(speak_text, language="ko-KR", voice="alice")

    if should_end:
        # Avoid duplicated closing sentence if assistant already closed in the main response.
        need_extra_closing = not any(k in speak_text for k in ["전달하겠", "전달드리겠", "좋은 하루", "좋은 저녁", "좋은 밤"])

        if need_extra_closing:
            daypart = closing_by_local_time()
            closing_text = f"감사합니다. {_master_label()}께 바로 전달하겠습니다. {daypart}"
            cache = state.get("tts_cache", {}) if isinstance(state.get("tts_cache"), dict) else {}
            key = "fast_end"
            closing_url = cache.get(key)
            if not closing_url and tts_url:
                closing_url, _ = elevenlabs_tts_save(closing_text, f"{call_id}_{key}")

            if closing_url:
                vr.play(closing_url)
            else:
                vr.say(closing_text, language="ko-KR", voice="alice")

        vr.hangup()
        state["status"] = "ended_by_agent"
        save_state(call_id, state)
    else:
        gather = Gather(
            input="speech",
            action=f"{PUBLIC_BASE_URL}/twiml/turn/{call_id}",
            method="POST",
            speech_timeout="2",
            timeout=4,
            language="ko-KR",
            hints="네, 아니요, 가능합니다, 안됩니다, 잠시만요, 여보세요",
        )
        vr.append(gather)
        vr.redirect(f"{PUBLIC_BASE_URL}/twiml/turn/{call_id}", method="POST")

    turn_elapsed = time.time() - turn_t0
    logger.info("[TURN END] call=%s | total=%.2fs | turns=%d | end=%s",
                call_id, turn_elapsed, turns, should_end)
    return PlainTextResponse(str(vr), media_type="application/xml")


@app.post("/callbacks/amd/{call_id}")
async def amd_callback(call_id: str, request: Request) -> dict[str, Any]:
    """Twilio Answering Machine Detection 콜백."""
    form = await request.form()
    state = load_state(call_id)
    answered_by = str(form.get("AnsweredBy", "unknown"))
    state["amd_result"] = answered_by
    state["amd_raw"] = dict(form)
    save_state(call_id, state)
    logger.info("call %s AMD result: %s", call_id, answered_by)
    return {"ok": True, "answered_by": answered_by}


@app.post("/callbacks/status/{call_id}")
async def status_callback(call_id: str, request: Request) -> dict[str, Any]:
    form = await request.form()
    state = load_state(call_id)
    state["twilio_status"] = dict(form)
    call_status = form.get("CallStatus")
    if call_status:
        state["status"] = str(call_status)
    save_state(call_id, state)
    logger.info("call %s status: %s", call_id, call_status)
    return {"ok": True}


@app.post("/callbacks/recording/{call_id}")
async def recording_callback(call_id: str, request: Request) -> dict[str, Any]:
    form = await request.form()
    state = load_state(call_id)
    rec = dict(form)
    state["recording"] = rec
    save_state(call_id, state)

    recording_url = rec.get("RecordingUrl")
    if not recording_url:
        return {"ok": False, "reason": "no RecordingUrl"}

    audio_path = try_download_recording(str(recording_url), call_id)
    if not audio_path:
        return {"ok": False, "reason": "download failed"}

    state["recording_file"] = str(audio_path)
    save_state(call_id, state)

    try:
        transcript = transcribe_recording(audio_path)
        state["transcript"] = transcript

        history_transcript = build_transcript_from_history(state)
        state["history_transcript"] = history_transcript

        summary = summarize_call(state, transcript)
        state["report"] = summary
        state["call_outcome"] = determine_call_outcome(state)
        state["status"] = "reported"
        save_state(call_id, state)

        report_path = OUT_DIR / f"{call_id}.report.txt"
        report_path.write_text(summary, encoding="utf-8")

        transcript_path = OUT_DIR / f"{call_id}.transcript.txt"
        transcript_path.write_text(
            f"=== 화자 분리 대화 기록 ===\n{history_transcript}\n\n"
            f"=== 녹음 전사 (원본) ===\n{transcript}",
            encoding="utf-8",
        )
        logger.info("call %s reported: outcome=%s", call_id, state.get("call_outcome"))
    except Exception as exc:
        logger.error("call %s report error: %s", call_id, exc)
        state["report_error"] = str(exc)
        save_state(call_id, state)

    return {"ok": True}


def create_call_id() -> str:
    return uuid.uuid4().hex[:12]


@app.post("/internal/create/{call_id}")
async def internal_create(call_id: str, target_name: str = Form(default="상대방"), task: str = Form(default="")) -> dict[str, Any]:
    st = load_state(call_id)
    st["target_name"] = target_name
    st["task"] = task

    opening_text = _build_opening_text(target_name, task)
    daypart = closing_by_local_time()

    cache: dict[str, str] = {}
    for key, text in [
        ("opening", opening_text),
        ("prompt_open", "말씀해 주시면 이어서 안내드리겠습니다."),
        ("prompt_next", "네, 계속 말씀해 주세요."),
        ("fast_reprompt", "말씀이 잘 안 들렸습니다. 한 번만 다시 말씀해 주시겠어요?"),
        ("fast_end", f"감사합니다. {_master_label()}께 바로 전달하겠습니다. {daypart}"),
        ("fast_end_alt", f"말씀 감사합니다. 전달받은 내용은 {_master_label()}께 바로 공유드리겠습니다. {daypart}"),
    ]:
        u, _ = elevenlabs_tts_save(text, f"{call_id}_{key}")
        if u:
            cache[key] = u

    st["tts_cache"] = cache
    save_state(call_id, st)
    return {"ok": True, "call_id": call_id}


@app.get("/internal/report/{call_id}")
def internal_report(call_id: str) -> dict[str, Any]:
    st = load_state(call_id)
    return st


# ---------------------------------------------------------------------------
# Realtime API (Phase 2) — Twilio Media Streams ↔ OpenAI Realtime
# ---------------------------------------------------------------------------

def build_realtime_system_prompt(state: dict[str, Any]) -> str:
    """Realtime API용 system prompt. [END_CALL] 대신 end_call 함수를 사용한다."""
    target = state.get("target_name", "상대방")
    return (
        f"당신은 {_master_label()}을 대신해 전화를 건 사람이다. 한국어로 실제 사람처럼 자연스럽게 통화한다.\n\n"
        + _common_prompt_body(target, end_method="end_call")
        + f"[상대] {target}\n"
        f"[목표]\n{state.get('task', '') or '목표 미지정'}\n"
    )


@app.get("/twiml/start-realtime/{call_id}")
def twiml_start_realtime(call_id: str) -> PlainTextResponse:
    """Realtime 모드용 TwiML — <Connect><Stream>으로 양방향 오디오 스트리밍."""
    logger.info("[REALTIME START] call=%s", call_id)
    state = load_state(call_id)
    state["status"] = "in_progress"
    state["mode"] = "realtime"
    save_state(call_id, state)

    ws_url = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")

    vr = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"{ws_url}/ws/media/{call_id}")
    vr.append(connect)
    return PlainTextResponse(str(vr), media_type="application/xml")


@app.websocket("/ws/media/{call_id}")
async def media_websocket(websocket: WebSocket, call_id: str) -> None:
    """Twilio Media Stream ↔ OpenAI Realtime API 양방향 브릿지."""
    await websocket.accept()
    logger.info("[REALTIME WS] call=%s connected", call_id)

    state = load_state(call_id)
    stream_sid: str | None = None
    call_ended = asyncio.Event()
    other_party_spoke = asyncio.Event()
    last_response_id: str | None = None  # 현재 진행 중인 응답 ID
    mark_counter = 0  # 오디오 마크 카운터
    is_ai_speaking = False  # AI가 현재 오디오 출력 중인지
    ai_audio_chunks_sent = 0  # 현재 응답에서 보낸 오디오 청크 수

    async def _maybe_speak_first(oai_ws, ended_ev: asyncio.Event) -> None:
        """상대방이 1.5초 내 말하지 않으면 AI가 먼저 시작한다."""
        if ended_ev.is_set() or other_party_spoke.is_set():
            return
        logger.info("[REALTIME] silence 1.5s, AI speaks first call=%s", call_id)
        try:
            await oai_ws.send(json.dumps({
                "type": "response.create",
                "response": {"modalities": ["text", "audio"]},
            }))
        except Exception:
            pass

    openai_ws_url = f"{OPENAI_REALTIME_URL}?model={OPENAI_REALTIME_MODEL}"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(openai_ws_url, additional_headers=headers) as openai_ws:
            # --- Configure session ---
            session_config = {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": build_realtime_system_prompt(state),
                    "voice": OPENAI_REALTIME_VOICE,
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
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
            await openai_ws.send(json.dumps(session_config))
            logger.info("[REALTIME] session configured for call=%s", call_id)

            # --- Twilio → OpenAI ---
            async def twilio_to_openai() -> None:
                nonlocal stream_sid
                try:
                    while not call_ended.is_set():
                        raw = await websocket.receive_text()
                        msg = json.loads(raw)
                        event = msg.get("event")

                        if event == "start":
                            stream_sid = msg["start"]["streamSid"]
                            logger.info("[REALTIME] twilio stream started sid=%s", stream_sid)
                            asyncio.get_event_loop().call_later(
                                1.5,
                                lambda: asyncio.ensure_future(_maybe_speak_first(openai_ws, call_ended)),
                            )

                        elif event == "media":
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": msg["media"]["payload"],
                            }))

                        elif event == "mark":
                            logger.debug("[REALTIME] mark received: %s", msg.get("mark", {}).get("name"))

                        elif event == "stop":
                            logger.info("[REALTIME] twilio stream stopped")
                            call_ended.set()
                            break
                except WebSocketDisconnect:
                    logger.info("[REALTIME] twilio WS disconnected")
                    call_ended.set()

            # --- OpenAI → Twilio ---
            async def openai_to_twilio() -> None:
                nonlocal last_response_id, mark_counter, is_ai_speaking, ai_audio_chunks_sent
                try:
                    async for raw in openai_ws:
                        if call_ended.is_set():
                            break
                        msg = json.loads(raw)
                        etype = msg.get("type", "")

                        # Audio delta → Twilio
                        if etype == "response.audio.delta":
                            delta = msg.get("delta", "")
                            if delta and stream_sid:
                                is_ai_speaking = True
                                ai_audio_chunks_sent += 1
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": delta},
                                })

                        # Audio done → send mark to Twilio for sync
                        elif etype == "response.audio.done":
                            is_ai_speaking = False
                            ai_audio_chunks_sent = 0
                            if stream_sid:
                                mark_counter += 1
                                await websocket.send_json({
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": f"audio_done_{mark_counter}"},
                                })

                        # Track response ID
                        elif etype == "response.created":
                            last_response_id = msg.get("response", {}).get("id")

                        # Assistant transcript done
                        elif etype == "response.audio_transcript.done":
                            text = msg.get("transcript", "").strip()
                            if text:
                                append_turn(state, "assistant", text)
                                save_state(call_id, state)
                                logger.info("[REALTIME] assistant: %s", text[:80])

                        # Speech detected → conditionally handle interruption
                        elif etype == "input_audio_buffer.speech_started":
                            other_party_spoke.set()
                            if is_ai_speaking and ai_audio_chunks_sent >= 40:
                                # AI가 이미 충분히 말했으면 (~2초+) 끊지 않고 마무리
                                logger.info(
                                    "[REALTIME] speech overlap — AI nearly done (%d chunks), continuing",
                                    ai_audio_chunks_sent,
                                )
                            else:
                                # AI가 아직 별로 안 말했거나 말하고 있지 않으면 양보
                                logger.info(
                                    "[REALTIME] speech detected — AI yielding (speaking=%s, chunks=%d)",
                                    is_ai_speaking, ai_audio_chunks_sent,
                                )
                                if stream_sid:
                                    await websocket.send_json({
                                        "event": "clear",
                                        "streamSid": stream_sid,
                                    })
                                if last_response_id:
                                    await openai_ws.send(json.dumps({
                                        "type": "response.cancel",
                                    }))

                        # User transcript done
                        elif etype == "conversation.item.input_audio_transcription.completed":
                            text = msg.get("transcript", "").strip()
                            if text:
                                append_turn(state, "user", text)
                                save_state(call_id, state)
                                logger.info("[REALTIME] user: %s", text[:80])

                        # Function call: end_call
                        elif etype == "response.function_call_arguments.done":
                            fn_name = msg.get("name", "")
                            if fn_name == "end_call":
                                args = json.loads(msg.get("arguments", "{}"))
                                reason = args.get("reason", "completed")
                                state["call_outcome"] = reason
                                save_state(call_id, state)
                                logger.info("[REALTIME] end_call reason=%s call=%s", reason, call_id)

                                await openai_ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": msg.get("call_id", ""),
                                        "output": json.dumps({"status": "ok", "reason": reason}),
                                    },
                                }))
                                await openai_ws.send(json.dumps({
                                    "type": "response.create",
                                    "response": {"modalities": ["text", "audio"]},
                                }))

                        # Response done — check if call should end
                        elif etype == "response.done":
                            resp = msg.get("response", {})
                            output = resp.get("output", [])
                            for item in output:
                                if item.get("type") == "function_call" and item.get("name") == "end_call":
                                    await asyncio.sleep(3)
                                    call_ended.set()
                                    break

                        elif etype == "error":
                            logger.error("[REALTIME] openai error: %s", msg.get("error"))

                except websockets.exceptions.ConnectionClosed:
                    logger.info("[REALTIME] openai WS closed")
                    call_ended.set()

            # Run both directions
            await asyncio.gather(
                twilio_to_openai(),
                openai_to_twilio(),
            )

    except Exception as exc:
        logger.error("[REALTIME] error call=%s: %s", call_id, exc)

    # Finalize state
    state = load_state(call_id)
    state["status"] = "ended_by_agent"
    if "call_outcome" not in state:
        state["call_outcome"] = "completed"
    save_state(call_id, state)
    logger.info("[REALTIME END] call=%s turns=%d", call_id, len(state.get("history", [])))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("ai_call_server:app", host="0.0.0.0", port=5055, reload=False)
