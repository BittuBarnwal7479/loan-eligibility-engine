"""Import n8n credentials + workflows into the running local n8n container.

    python scripts/setup_n8n.py            # import credentials and workflows, publish them
    python scripts/setup_n8n.py --no-publish

Secrets are read from .env and streamed to the container over stdin into a
temporary file that is deleted immediately after import; nothing secret is
written to the repository or the host disk. Re-running is safe: credentials
and workflows have fixed ids, so they are updated rather than duplicated.

AWS SES credentials are only imported when AWS_SES_ACCESS_KEY_ID and
AWS_SES_SECRET_ACCESS_KEY are present in .env (cloud mode).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ["workflow-a-loan-discovery.json", "workflow-b-user-matching.json", "workflow-c-notification.json"]
WORKFLOW_IDS = ["LoanEngWorkflowA", "LoanEngWorkflowB", "LoanEngWorkflowC"]


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        sys.exit(".env not found - copy .env.example to .env first")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def compose(*args: str, stdin: str | None = None) -> str:
    cmd = ["docker", "compose", *args]
    result = subprocess.run(cmd, cwd=ROOT, input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"command failed: {' '.join(args[:3])}...\n{result.stdout}\n{result.stderr}")
    return result.stdout


def credentials(env: dict[str, str]) -> list[dict]:
    for key in ("INTERNAL_API_TOKEN", "N8N_WEBHOOK_TOKEN"):
        if not env.get(key) or env[key] == "change-me":
            sys.exit(f"{key} must be set in .env")
    creds = [
        {"id": "LoanEngInternal1", "name": "Loan Engine Internal API (X-Internal-Token)", "type": "httpHeaderAuth",
         "data": {"name": "X-Internal-Token", "value": env["INTERNAL_API_TOKEN"]}},
        {"id": "LoanEngWebhook01", "name": "Loan Engine Webhook Token (X-Webhook-Token)", "type": "httpHeaderAuth",
         "data": {"name": "X-Webhook-Token", "value": env["N8N_WEBHOOK_TOKEN"]}},
        {"id": "LoanEngMailpit01", "name": "Local Mailpit SMTP", "type": "smtp",
         "data": {"user": "", "password": "", "host": "mailpit", "port": 1025, "secure": False,
                  "disableStartTls": True}},
    ]
    if env.get("AWS_SES_ACCESS_KEY_ID") and env.get("AWS_SES_SECRET_ACCESS_KEY"):
        creds.append({"id": "LoanEngAwsSes001", "name": "AWS SES sender (least-privilege IAM user)", "type": "aws",
                      "data": {"region": env.get("AWS_SES_REGION", "ap-southeast-2"),
                               "accessKeyId": env["AWS_SES_ACCESS_KEY_ID"],
                               "secretAccessKey": env["AWS_SES_SECRET_ACCESS_KEY"]}})
    return creds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()
    env = read_env()

    creds = credentials(env)
    tmp = "/tmp/loan-engine-creds.json"
    compose("exec", "-T", "n8n", "sh", "-c", f"umask 077 && cat > {tmp}", stdin=json.dumps(creds))
    try:
        compose("exec", "-T", "n8n", "n8n", "import:credentials", f"--input={tmp}")
    finally:
        compose("exec", "-T", "n8n", "rm", "-f", tmp)
    print(f"imported {len(creds)} credential(s): {', '.join(c['name'] for c in creds)}")

    for name in WORKFLOWS:
        compose("exec", "-T", "n8n", "n8n", "import:workflow", f"--input=/workflows/{name}")
        print(f"imported workflow {name}")

    if not args.no_publish:
        for wf_id in WORKFLOW_IDS:
            compose("exec", "-T", "n8n", "n8n", "publish:workflow", f"--id={wf_id}")
        print("published workflows; restarting n8n so webhooks and schedules register")
        compose("restart", "n8n")
    print(compose("exec", "-T", "n8n", "n8n", "list:workflow").strip())


if __name__ == "__main__":
    main()
