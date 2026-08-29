#!/usr/bin/env python3
"""
Validate the RKLLM v1.3 native multimodal endpoint for Qwen3.5.

Run this on the RK3588 board after llm_server.py is started:
  python3 tools/test_vision_qwen35.py --image /tmp/demo.jpg

The image path is a board-local path and must be allowed by llm_server.py.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


EXPECTED_HEALTH = {
    "max_new_tokens": 48,
    "img_start": "<|vision_start|>",
    "img_end": "<|vision_end|>",
    "img_content": "<|image_pad|>",
    "model_width": 448,
    "model_height": 448,
}


def request_json(url, payload=None, timeout=180):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc


def check_health(base_url):
    status, health = request_json(f"{base_url}/health", timeout=30)
    if status != 200:
        raise RuntimeError(f"unexpected health status: {status}")

    print("[health]")
    print(json.dumps(health, ensure_ascii=False, indent=2))

    errors = []
    for key, expected in EXPECTED_HEALTH.items():
        actual = health.get("vision", {}).get(key)
        if actual != expected:
            errors.append(f"{key}: expected {expected!r}, got {actual!r}")

    for key in ("n_image_tokens", "embed_size", "n_output"):
        value = health.get("vision", {}).get(key)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"{key}: expected positive integer, got {value!r}")

    if health.get("vision", {}).get("enabled") is not True:
        errors.append("vision.enabled: expected true")

    if errors:
        raise RuntimeError("health check mismatch:\n  " + "\n  ".join(errors))


def analyze_image(base_url, image_path, question):
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"image not found on this board: {image_path}")

    payload = {
        "image_path": image_path,
        "question": question,
    }
    started_at = time.monotonic()
    status, data = request_json(f"{base_url}/vision/analyze", payload=payload)
    elapsed = time.monotonic() - started_at
    if status != 200:
        raise RuntimeError(f"unexpected analyze status: {status}")
    if "result" not in data or not str(data["result"]).strip():
        raise RuntimeError(f"empty analyze result: {data!r}")

    print("[vision/analyze]")
    print(f"耗时: {elapsed:.3f}s")
    print(data["result"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--image", default="/tmp/demo.jpg")
    parser.add_argument("--question", default="这张图片中有什么？")
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    try:
        check_health(base_url)
        if not args.health_only:
            analyze_image(base_url, args.image, args.question)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print("[OK] Qwen3.5 multimodal vision service validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
