#!/usr/bin/env python3
"""
TEMP: Direct Twilio call test - bypasses ElevenLabs
Usage: python3 scripts/test_twilio_direct.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# Config
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "+19194203425")
TO_NUMBER = "+19196495528"

# Simple TwiML that says hello
TWIML_URL = "http://demo.twilio.com/docs/voice.xml"

print(f"=== Direct Twilio Call Test ===")
print(f"Account SID: {ACCOUNT_SID[:10]}...{ACCOUNT_SID[-4:]}")
print(f"From: {FROM_NUMBER}")
print(f"To: {TO_NUMBER}")
print(f"TwiML URL: {TWIML_URL}")
print()

try:
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

    call = client.calls.create(
        to=TO_NUMBER,
        from_=FROM_NUMBER,
        url=TWIML_URL
    )

    print(f"SUCCESS!")
    print(f"  Call SID: {call.sid}")
    print(f"  Status: {call.status}")
    print(f"  From: {call.from_}")
    print(f"  To: {call.to}")

except TwilioRestException as e:
    print(f"TWILIO ERROR!")
    print(f"  Code: {e.code}")
    print(f"  Status: {e.status}")
    print(f"  Message: {e.msg}")
    print(f"  More info: {e.uri}")

except Exception as e:
    print(f"GENERAL ERROR: {type(e).__name__}: {e}")
