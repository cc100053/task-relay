"""Stand-in for `codex app-server` on stdio. Reply comes from $FAKE_RATE_LIMITS (JSON)."""
import json
import os
import sys

reply = os.environ.get("FAKE_RATE_LIMITS", "{}")
mode = os.environ.get("FAKE_MODE", "ok")
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        if mode == "exit":
            sys.exit(1)
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif msg.get("method") == "initialized":
        assert msg.get("params") is None
    elif msg.get("method") == "account/rateLimits/read":
        params = msg.get("params")
        if mode == "legacy" and params is not None:
            # Older CLIs take this request without params.
            print(json.dumps({"id": msg["id"], "error": {
                "code": -32600, "message": "Invalid request: invalid type: map, expected unit",
            }}), flush=True)
            continue
        if mode != "legacy":
            assert params == {"excludeResetCreditDetails": True}
        if mode == "hang":
            continue
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        if mode == "error":
            print(json.dumps({"id": msg["id"], "error": {"code": -32603, "message": "boom"}}), flush=True)
        else:
            print(json.dumps({"id": msg["id"], "result": json.loads(reply)}), flush=True)
