# AI Call Agent

Twilio + OpenAI 기반 AI 전화 대리 통화 에이전트입니다. 사용자가 지정한 용건을 AI가 대신 전화로 전달하고, 상대방과 자연스럽게 대화한 뒤 결과를 보고합니다.

## 주요 기능

- **AI 대리 통화**: 용건을 입력하면 AI가 전화를 걸어 자연스럽게 대화
- **OpenAI Realtime API**: 실시간 음성 대화 (Twilio Media Streams 연동)
- **Gather 모드**: STT → LLM → TTS 순차 처리 방식 (ElevenLabs TTS 지원)
- **자동 녹음 및 전사**: 통화 녹음, Whisper 전사, GPT 요약 리포트 생성
- **음성사서함 감지(AMD)**: 자동 응답기 감지 시 자동 처리
- **텍스트 테스트**: 실제 전화 없이 터미널에서 대화 흐름 확인
- **음성 테스트**: 마이크/스피커로 OpenAI Realtime API 직접 연결하여 음성 대화 테스트

## 구조

```
ai_call_server.py   # FastAPI 서버 (TwiML, WebSocket, 콜백 처리)
run_ai_call.py      # CLI 클라이언트 (전화 발신 및 결과 대기)
auto_dialer.py      # 단순 자동 발신 스크립트 (TTS 메시지 전달)
text_test.py        # 텍스트 모드 대화 테스트
voice_test.py       # 음성 모드 대화 테스트 (마이크/스피커)
realtime_server.py  # Realtime API 프로토타입 서버
call_and_stream.py  # 발신 + 미디어 스트림 연결 스크립트
scripts/            # ngrok 시작, 서버 스택 시작 등 유틸리티
```

## 사전 준비

- Python 3.12+
- [Twilio 계정](https://www.twilio.com/) (전화번호 필요)
- [OpenAI API 키](https://platform.openai.com/)
- [ngrok](https://ngrok.com/) (로컬 서버 공개용)
- (선택) [ElevenLabs API 키](https://elevenlabs.io/) (고품질 TTS)

## 설치

```bash
git clone https://github.com/ohhan777/ai-call-agent.git
cd ai-call-agent
python -m venv .venv
source .venv/bin/activate
uv sync  # 또는 pip install -e .
```

## 환경 변수 설정

프로젝트 루트에 `.env` 파일을 만들거나, 환경 변수를 직접 설정합니다.

```env
# 필수
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_token
TWILIO_FROM_NUMBER=+1234567890
TWILIO_PUBLIC_BASE_URL=https://your-ngrok-url.ngrok.io
OPENAI_API_KEY=your_openai_key

# 발신자 정보 (소개 문구 조합에 사용)
MASTER_NAME=오한
MASTER_TITLE=박사
CALLER_NAME=인공지능
CALLER_TITLE=비서
# → "안녕하세요. 오한 박사님의 인공지능 비서입니다"

# 선택
ELEVENLABS_API_KEY=your_elevenlabs_key
ELEVENLABS_VOICE_ID=your_voice_id
OPENAI_CALL_MODEL=gpt-5-mini
OPENAI_REALTIME_MODEL=gpt-realtime-mini
NGROK_AUTH_TOKEN=your_ngrok_token
```

## 사용법

### 1. 서버 시작

```bash
# 방법 A: 스크립트로 서버 + ngrok 한번에 시작
./scripts/start_ai_stack.sh

# 방법 B: 수동 시작
source .venv/bin/activate
uvicorn ai_call_server:app --host 0.0.0.0 --port 5055
# 별도 터미널에서 ngrok 시작
ngrok http 5055
```

### 2. 전화 걸기

```bash
# Realtime API 모드 (기본)
python run_ai_call.py --to +821012345678 --task "내일 3시 회의 가능 여부 확인"

# Gather 모드 (TTS 순차 처리)
python run_ai_call.py --to +821012345678 --task "빅맥 세트 1개 포장 주문" --gather

# 실패 시 재시도
python run_ai_call.py --to +821012345678 --task "배달 가능 여부 확인" --retry 2
```

### 3. 텍스트 테스트 (전화 없이)

```bash
python text_test.py --task "내일 오후 미팅 가능한지 확인해 주세요"
python text_test.py --task "빅맥 세트 1개 포장" --target-name "맥도날드 직원"
```

### 4. 음성 테스트 (마이크/스피커)

```bash
python voice_test.py --task "내일 3시 회의 가능 여부 확인" --target-name "김과장"
python voice_test.py --task "순살 치킨 반반 1개 배달 주문" --target-name "치킨집 직원"
```

### 5. 단순 자동 발신

```bash
python auto_dialer.py --to +821012345678 --message "안녕하세요. 자동 안내 전화입니다."
```

## API 엔드포인트

| 엔드포인트 | 설명 |
|---|---|
| `GET /health` | 서버 상태 확인 |
| `POST /internal/create/{call_id}` | 통화 세션 생성 |
| `GET /internal/report/{call_id}` | 통화 결과 조회 |
| `GET /twiml/start/{call_id}` | Gather 모드 TwiML |
| `GET /twiml/start-realtime/{call_id}` | Realtime 모드 TwiML |

## License

MIT
