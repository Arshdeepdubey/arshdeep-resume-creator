#!/usr/bin/env python3
"""Check the configured OpenAI-compatible LLM endpoint and model."""
import os
import sys

from openai import APIConnectionError, APIStatusError, OpenAI

from tailor import load_dotenv


def main() -> int:
    load_dotenv()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()

    missing = [
        name for name, value in (
            ("LLM_BASE_URL", base_url),
            ("LLM_API_KEY", api_key),
            ("LLM_MODEL", model),
        ) if not value
    ]
    if missing:
        print(f"Missing configuration in environment or .env: {', '.join(missing)}", file=sys.stderr)
        return 1

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=30)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "local ollama connection test successful. Reply with exactly: OK"}],
        temperature=0,
        max_tokens=20,
    )

    content = response.choices[0].message.content if response.choices else None
    if not content or not content.strip():
        print("The endpoint responded, but returned empty message content.", file=sys.stderr)
        print(f"Finish reason: {response.choices[0].finish_reason if response.choices else 'no choices'}", file=sys.stderr)
        return 1

    print("LLM call succeeded.")
    print(f"Response: {content.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
