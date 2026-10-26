"""Stand-in for `codex app-server` on stdio. Replies come from $FAKE_RATE_LIMITS / $FAKE_THREADS (JSON)."""
import json
import os
import sys

reply = os.environ.get("FAKE_RATE_LIMITS", "{}")
threads = json.loads(os.environ.get("FAKE_THREADS", "{}"))  # {"list": [...], "turns": {id: [...]}}
mode = os.environ.get("FAKE_MODE", "ok")
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        if mode == "exit":
            sys.exit(1)
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        assert msg["params"] == {"excludeResetCreditDetails": True}
        if mode == "hang":
            continue
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        if mode == "error":
            print(json.dumps({"id": msg["id"], "error": {"code": -32603, "message": "boom"}}), flush=True)
        else:
            print(json.dumps({"id": msg["id"], "result": json.loads(reply)}), flush=True)
    elif msg.get("method") == "thread/list":
        print(json.dumps({"id": msg["id"], "result": {"data": threads.get("list", []), "nextCursor": None}}),
              flush=True)
    elif msg.get("method") == "thread/turns/list":
        turns = threads.get("turns", {}).get(msg["params"]["threadId"], [])
        print(json.dumps({"id": msg["id"], "result": {"data": turns[:1], "nextCursor": None}}), flush=True)
