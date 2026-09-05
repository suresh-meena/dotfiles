"""Bounded streaming check against the local Qwen endpoint; prints metrics only."""
import json
import time
import urllib.request

prefix = "This is a synthetic latency check. The marker is BLUE.\n" * 600
for label in ("first", "repeat"):
    body = {
        "model": "qwen38",
        "messages": [{"role": "user", "content": prefix + "Reply with the marker only."}],
        "max_tokens": 32,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.monotonic()
    first = None
    usage = {}
    content = ""
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        for line in response:
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            event = json.loads(line[6:])
            usage = event.get("usage") or usage
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                text = delta.get("content") or ""
                if text and first is None:
                    first = time.monotonic() - started
                content += text
    assert "BLUE" in content, "unexpected answer"
    print(json.dumps({"case": label, "first_text_s": round(first, 3) if first else None,
                      "total_s": round(time.monotonic() - started, 3), "usage": usage}))
