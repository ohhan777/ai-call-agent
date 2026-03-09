#!/usr/bin/env python3
import os
import json
import logging
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Start
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("realtime_server")

TWILIO_STREAM_WS = os.getenv("TWILIO_STREAM_WS") # e.g. wss://xxxx.ngrok.io/ws/media
active_streams = {}

@app.get("/twiml/{call_id}")
async def twiml(call_id: str, request: Request):
resp = VoiceResponse()
resp.say("안내 말씀 드립니다. 이 통화는 녹음 및 전사될 수 있습니다.", language="ko-KR")

stream_url = TWILIO_STREAM_WS
if not stream_url:
return PlainTextResponse("Set TWILIO_STREAM_WS in ~/.openclaw/.env", status_code=500)

start = Start()
start.stream(url=stream_url)
resp.append(start)

return PlainTextResponse(str(resp), media_type="application/xml")

@app.websocket("/ws/media")
async def media_ws(websocket: WebSocket):
await websocket.accept()
call_sid = None
try:
while True:
msg = await websocket.receive_text()
data = json.loads(msg)
event = data.get("event")

if event == "start":
call_sid = data.get("start", {}).get("callSid")
if call_sid:
active_streams[call_sid] = websocket
logger.info(f"stream start call_sid={call_sid}")

elif event == "media":
# TODO: 실시간 ASR -> LLM -> TTS -> Twilio 송신
pass

elif event == "stop":
logger.info(f"stream stop call_sid={call_sid}")
break

except WebSocketDisconnect:
logger.info("WS disconnected")

finally:
if call_sid and call_sid in active_streams:
del active_streams[call_sid]

@app.post("/recording_callback")
async def recording_callback(request: Request):
form = await request.form()
logger.info(f"recording callback: {dict(form)}")
return {"ok": True}
