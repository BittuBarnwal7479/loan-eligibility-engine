"""End-to-end check of the local stack: upload -> import -> n8n matching -> n8n notifications -> Mailpit.

    python scripts/e2e_local.py sample-data/users_sample.csv

Uses only the public API (same calls as the frontend) and Mailpit's read API.
Prints what actually happened; exits non-zero if the pipeline does not finish.
Requires: docker compose up, python scripts/setup_n8n.py, and NOTIFICATION_MODE=smtp.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

API = "http://localhost:8081"
MAILPIT = "http://localhost:8026"


def request(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f"Content-Type: text/csv\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "sample-data/users_sample.csv")
    content = path.read_bytes()
    mail_before = json.loads(request("GET", f"{MAILPIT}/api/v1/messages?limit=1")[1]).get("total", 0)

    status, raw = request("POST", f"{API}/api/uploads", json.dumps({"filename": path.name, "size_bytes": len(content)}).encode(),
                          {"Content-Type": "application/json"})
    created = json.loads(raw)
    assert status == 201, raw
    batch_id = created["upload_batch_id"]
    print(f"batch {batch_id} created (storage_mode={created['storage_mode']})")
    body, ctype = multipart(created["upload"]["fields"], path.name, content)
    status, raw = request("POST", created["upload"]["url"], body, {"Content-Type": ctype})
    print(f"upload -> HTTP {status}")

    deadline = time.time() + 180
    batch: dict = {}
    while time.time() < deadline:
        batch = json.loads(request("GET", f"{API}/api/uploads/{batch_id}")[1])
        done_notify = batch["notification_status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS")
        failed = batch["status"] == "FAILED" or batch["matching_status"] in ("FAILED", "TRIGGER_FAILED")
        if done_notify or failed or (batch["matching_status"] == "COMPLETED" and batch["matching"]
                                     and batch["matching"]["eligible_matches"] + batch["matching"]["potential_matches"] == 0):
            break
        time.sleep(2)

    print(json.dumps({k: batch.get(k) for k in ("status", "total_rows", "processed_rows", "failed_rows",
                                                "matching_status", "notification_status")}, indent=2))
    print("matching:", json.dumps(batch.get("matching"), indent=2))
    print("notifications:", batch.get("notifications"))
    time.sleep(2)
    mail = json.loads(request("GET", f"{MAILPIT}/api/v1/messages?limit=200")[1])
    new = mail.get("total", 0) - mail_before
    recipients = sorted({to["Address"] for m in mail.get("messages", [])[:max(new, 0)] for to in m["To"]})
    print(f"mailpit: {new} new message(s); recipients: {recipients}")
    ok = batch.get("notification_status") in ("COMPLETED", "COMPLETED_WITH_ERRORS") or batch.get("status") == "FAILED"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
