#!/usr/bin/env python3
import os, argparse, sys, uuid
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

SID = os.getenv("TWILIO_ACCOUNT_SID")
TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FROM = os.getenv("TWILIO_FROM_NUMBER")
TWIML_BASE = os.getenv("TWIML_BASE_URL") # e.g. https://xxxx.ngrok.io

if not all([SID, TOKEN, FROM, TWIML_BASE]):
print("Missing one of: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWIML_BASE_URL")
sys.exit(1)

parser = argparse.ArgumentParser()
parser.add_argument("--to", required=True, help="E.164 number, e.g. +8210...")
args = parser.parse_args()

client = Client(SID, TOKEN)
call_id = uuid.uuid4().hex[:12]
url = f"{TWIML_BASE}/twiml/{call_id}"

call = client.calls.create(
to=args.to,
from_=FROM,
url=url,
record=True,
recording_status_callback=f"{TWIML_BASE}/recording_callback",
recording_status_callback_method="POST",
)
print("Call SID:", call.sid)
print("TwiML URL:", url)
