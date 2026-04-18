"""
tests/test_claude_api.py — Test Anthropic Claude API connectivity.
Sends a minimal test prompt to Claude Haiku and prints the response.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import anthropic

print("=== Step 6 — Claude API test ===")

api_key = os.getenv("ANTHROPIC_API_KEY", "")
if not api_key:
    print("FAIL — ANTHROPIC_API_KEY not set")
    sys.exit(1)

print(f"API key: {api_key[:12]}...{api_key[-6:]}")
print(f"Model:   claude-haiku-4-5-20251001")
print()

try:
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            {"role": "user", "content": "Write one sentence in Dutch about the weather."}
        ],
    )
    response_text = message.content[0].text
    print(f"Response: {response_text}")
    print()
    print(f"Input tokens:  {message.usage.input_tokens}")
    print(f"Output tokens: {message.usage.output_tokens}")
    print()
    print("RESULT: PASS")
except anthropic.AuthenticationError as e:
    print(f"RESULT: FAIL — Authentication error: {e}")
    print("  → Check ANTHROPIC_API_KEY in .env")
except Exception as e:
    print(f"RESULT: FAIL — {type(e).__name__}: {e}")
