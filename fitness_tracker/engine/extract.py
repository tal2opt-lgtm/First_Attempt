"""
extract.py
==========
Send one or more workout screenshots to Claude's vision model and get back the
raw JSON dict described in prompt.txt.

Requires the ANTHROPIC_API_KEY environment variable. Uses only the standard
library plus `requests` so it is easy to run anywhere.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path

import requests

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("FITNESS_MODEL", "claude-sonnet-5")
PROMPT = (Path(__file__).parent / "prompt.txt").read_text(encoding="utf-8")


def _encode_image(path: str | Path) -> dict:
    path = Path(path)
    media_type = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _extract_json(text: str) -> dict:
    """Be forgiving if the model wraps the JSON in prose or code fences."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object found in model reply:\n{text}")
    return json.loads(m.group(0))


def extract_from_images(image_paths: list[str | Path],
                        api_key: str | None = None) -> dict:
    """Return the raw JSON dict for one workout from its screenshot(s)."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    content = [_encode_image(p) for p in image_paths]
    content.append({"type": "text", "text": PROMPT})

    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    reply = resp.json()["content"][0]["text"]
    return _extract_json(reply)
