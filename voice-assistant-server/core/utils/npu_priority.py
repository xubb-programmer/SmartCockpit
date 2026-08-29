"""Cross-process NPU priority hints shared with the RKLLM services."""

import json
import os
import re
import tempfile
import time


DEFAULT_PRIORITY_DIR = "/tmp/voice_assistant_npu_priority"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def signal_text_priority(owner: object, ttl_seconds: float = 4.0) -> str:
    """Keep background vision pre-encoding off the NPU for a short voice turn."""
    priority_dir = os.environ.get("VOICE_ASSISTANT_NPU_PRIORITY_DIR", DEFAULT_PRIORITY_DIR)
    os.makedirs(priority_dir, exist_ok=True)
    safe_owner = _SAFE_NAME_RE.sub("-", str(owner or "voice"))[:80] or "voice"
    target = os.path.join(priority_dir, f"speech-{safe_owner}.priority")
    payload = {
        "owner": safe_owner,
        "expires_at": time.time() + max(0.5, float(ttl_seconds)),
    }

    fd, tmp_path = tempfile.mkstemp(prefix=".priority-", dir=priority_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=True)
        os.replace(tmp_path, target)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return target
