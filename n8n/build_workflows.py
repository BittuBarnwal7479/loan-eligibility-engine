"""Generate the three importable n8n workflow JSON files.

The JSON files in n8n/workflows/ are the deliverables; this script exists so
node wiring stays consistent and reviewable. Re-run after editing:

    python n8n/build_workflows.py

Node parameter shapes target n8n 2.43 (node versions pinned below).
No secrets are written: nodes reference credentials by id/name only.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent / "workflows"
def api(path_js: str) -> str:
    """n8n expression: backend base URL (env, defaulting to the compose service) + a JS path expression."""
    return "={{ ($env.LOAN_API_BASE_URL || 'http://api:8000') + " + path_js + " }}"


def self_url(path_js: str) -> str:
    return "={{ ($env.N8N_SELF_BASE_URL || 'http://localhost:5678') + " + path_js + " }}"

CRED_INTERNAL = {"httpHeaderAuth": {"id": "LoanEngInternal1", "name": "Loan Engine Internal API (X-Internal-Token)"}}
CRED_WEBHOOK = {"httpHeaderAuth": {"id": "LoanEngWebhook01", "name": "Loan Engine Webhook Token (X-Webhook-Token)"}}
CRED_SMTP = {"smtp": {"id": "LoanEngMailpit01", "name": "Local Mailpit SMTP"}}
CRED_AWS = {"aws": {"id": "LoanEngAwsSes001", "name": "AWS SES sender (least-privilege IAM user)"}}

RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000}


def _id(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"loan-engine/{seed}"))


class Flow:
    def __init__(self, name: str, wf_id: str) -> None:
        self.name, self.wf_id = name, wf_id
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}

    def node(self, name: str, type_: str, version: float, pos: tuple[int, int], params: dict, **extra) -> str:
        node = {"parameters": params, "id": _id(f"{self.wf_id}/{name}"), "name": name, "type": type_,
                "typeVersion": version, "position": list(pos)}
        node.update(extra)
        self.nodes.append(node)
        return name

    def link(self, src: str, dst: str, out: int = 0) -> None:
        outputs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outputs) <= out:
            outputs.append([])
        outputs[out].append({"node": dst, "type": "main", "index": 0})

    def sticky(self, name: str, pos: tuple[int, int], text: str, width: int = 420, height: int = 260) -> None:
        self.node(name, "n8n-nodes-base.stickyNote", 1, pos, {"content": text, "width": width, "height": height})

    def dump(self, filename: str, tags: list[str]) -> None:
        doc = {
            "id": self.wf_id,
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True, "saveDataErrorExecution": "all",
                         "saveDataSuccessExecution": "all", "timezone": "Asia/Kolkata"},
            "pinData": {},
            "active": False,
            "meta": {"templateCredsSetupCompleted": False},
            "tags": [{"name": t} for t in tags],
        }
        (OUT / filename).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")


def http(method: str, url: str, body: str | None = None, creds: dict | None = CRED_INTERNAL, timeout: int = 120000,
         full_response: bool = False, text: bool = False, never_error: bool = False, headers: dict | None = None) -> dict:
    params: dict = {"method": method, "url": url, "options": {"timeout": timeout}}
    if creds is not None:
        params.update({"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"})
    if headers:
        params["sendHeaders"] = True
        params["headerParameters"] = {"parameters": [{"name": k, "value": v} for k, v in headers.items()]}
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
    response = {}
    if full_response:
        response["fullResponse"] = True
    if never_error:
        response["neverError"] = True
    if text:
        response["responseFormat"] = "text"
    if response:
        params["options"]["response"] = {"response": response}
    return params


def if_params(left: str, op_type: str, operation: str, right: str = "") -> dict:
    condition = {"id": _id(left + operation), "leftValue": left, "rightValue": right,
                 "operator": {"type": op_type, "operation": operation}}
    if op_type == "boolean":
        condition["operator"]["singleValue"] = True
    return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                           "conditions": [condition], "combinator": "and"}, "options": {}}


VALIDATE_BATCH_JS = """// Accept only a UUID batch id from the webhook body; reject anything else.
const body = $json.body || {};
const id = String(body.upload_batch_id || '');
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
  throw new Error('upload_batch_id missing or not a UUID');
}
return [{ json: { upload_batch_id: id, received_at: new Date().toISOString() } }];"""


# --------------------------------------------------------------------------- Workflow A


def workflow_a() -> None:
    f = Flow("A - Loan Product Discovery", "LoanEngWorkflowA")
    f.sticky("About", (-60, -320), (
        "## Workflow A - loan product discovery\n"
        "Runs weekly (Mon 06:00) or on demand via **Run manually**.\n\n"
        "For each configured public lender page: check robots.txt, fetch the page politely "
        "(fixed User-Agent, timeout, delay between sources), then the backend extracts facts "
        "deterministically, validates them and upserts by (lender, product).\n\n"
        "A failed fetch or a changed layout never deletes data: it is counted and products "
        "unverified for 7 days are flagged stale (30 days -> inactive).\n\n"
        "No LLM is used here. Sources live in backend/config/loan_sources.json."), 520, 300)
    manual = f.node("Run manually", "n8n-nodes-base.manualTrigger", 1, (0, 0), {})
    schedule = f.node("Weekly schedule", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 180), {
        "rule": {"interval": [{"field": "weeks", "weeksInterval": 1, "triggerAtDay": [1], "triggerAtHour": 6}]}})
    sources = f.node("Get configured sources", "n8n-nodes-base.httpRequest", 4.2, (240, 80),
                     http("GET", api("'/internal/products/sources'")), credentials=CRED_INTERNAL, **RETRY)
    explode = f.node("One item per source", "n8n-nodes-base.code", 2, (460, 80), {"jsCode": (
        "const cfg = $input.first().json;\n"
        "return (cfg.sources || []).map(s => ({ json: { ...s, user_agent: cfg.user_agent,"
        " delay_seconds: cfg.min_delay_seconds || 5 } }));")})
    loop = f.node("Loop over sources", "n8n-nodes-base.splitInBatches", 3, (680, 80), {"batchSize": 1, "options": {}})
    robots = f.node("Fetch robots.txt", "n8n-nodes-base.httpRequest", 4.2, (900, 180), http(
        "GET", "={{ $json.robots_url }}", creds=None, timeout=15000, full_response=True, text=True, never_error=True,
        headers={"User-Agent": "={{ $json.user_agent }}"}), onError="continueRegularOutput")
    robots_check = f.node("Evaluate robots.txt", "n8n-nodes-base.httpRequest", 4.2, (1120, 180), http(
        "POST", api("'/internal/products/robots-check'"),
        body="={{ JSON.stringify({ source_key: $('Loop over sources').item.json.source_key, "
             "status_code: $json.statusCode ?? null, "
             "robots_txt: (typeof ($json.data ?? $json.body) === 'string' ? ($json.data ?? $json.body) : null) }) }}"),
        credentials=CRED_INTERNAL, **RETRY)
    allowed = f.node("Allowed by robots.txt?", "n8n-nodes-base.if", 2.2, (1340, 180),
                     if_params("={{ $json.allowed }}", "boolean", "true"))
    page = f.node("Fetch product page", "n8n-nodes-base.httpRequest", 4.2, (1560, 80), http(
        "GET", "={{ $('Loop over sources').item.json.url }}", creds=None, timeout=20000, full_response=True,
        text=True, never_error=True, headers={"User-Agent": "={{ $('Loop over sources').item.json.user_agent }}",
                                              "Accept": "text/html"}), onError="continueRegularOutput")
    ingest = f.node("Extract, validate and upsert", "n8n-nodes-base.httpRequest", 4.2, (1780, 80), http(
        "POST", api("'/internal/products/ingest'"),
        body="={{ JSON.stringify({ source_key: $('Loop over sources').item.json.source_key, "
             "status_code: $json.statusCode ?? null, "
             "html: (typeof ($json.data ?? $json.body) === 'string' ? ($json.data ?? $json.body) : null), "
             "error: $json.error ? String($json.error.message || 'request failed').slice(0, 400) : null }) }}"),
        credentials=CRED_INTERNAL, **RETRY)
    skipped = f.node("Record robots skip", "n8n-nodes-base.code", 2, (1560, 280), {"jsCode": (
        "return [{ json: { source_key: $('Loop over sources').item.json.source_key, stored: false,"
        " reason: 'skipped: ' + ($json.reason || 'disallowed by robots.txt') } }];")})
    wait = f.node("Polite delay", "n8n-nodes-base.wait", 1.1, (2000, 180), {
        "amount": "={{ $('Loop over sources').item.json.delay_seconds }}", "unit": "seconds"},
        webhookId=_id("A/wait"))
    stale = f.node("Flag stale products", "n8n-nodes-base.httpRequest", 4.2, (900, -100),
                   http("POST", api("'/internal/products/refresh-staleness'")), credentials=CRED_INTERNAL,
                   executeOnce=True, **RETRY)
    summary = f.node("Run summary", "n8n-nodes-base.code", 2, (1120, -100), {"jsCode": (
        "// The loop's 'done' output (index 0) carries every per-source result.\n"
        "const results = $('Loop over sources').all(0).map(i => i.json);\n"
        "return [{ json: { finished_at: new Date().toISOString(), staleness: $json,\n"
        "  stored: results.filter(r => r.stored).map(r => r.source_key),\n"
        "  not_stored: results.filter(r => !r.stored).map(r => ({ source_key: r.source_key, reason: r.reason })) } }];")})

    f.link(manual, sources)
    f.link(schedule, sources)
    f.link(sources, explode)
    f.link(explode, loop)
    f.link(loop, stale, 0)       # done
    f.link(loop, robots, 1)      # loop
    f.link(robots, robots_check)
    f.link(robots_check, allowed)
    f.link(allowed, page, 0)
    f.link(allowed, skipped, 1)
    f.link(page, ingest)
    f.link(ingest, wait)
    f.link(skipped, wait)
    f.link(wait, loop)
    f.link(stale, summary)
    f.dump("workflow-a-loan-discovery.json", ["loan-engine", "discovery"])


# --------------------------------------------------------------------------- Workflow B


def workflow_b() -> None:
    f = Flow("B - User Matching (multi-stage)", "LoanEngWorkflowB")
    f.sticky("About", (-60, -340), (
        "## Workflow B - optimized matching\n"
        "Triggered by the backend after a CSV batch is committed (header **X-Webhook-Token**).\n\n"
        "1. Validate the batch id and status.\n"
        "2. *(optional)* LLM normalizes free-text **product** criteria once per product, "
        "evidence-checked. Skipped unless LLM_ENABLED=true. Never sees user data.\n"
        "3. Backend runs, per 1,000-user chunk: **SQL pre-filter** (parameterized join drops pairs "
        "that fail any known threshold) -> **deterministic evaluation** with per-criterion reasons "
        "-> idempotent upsert. Unknown criteria never count as a pass.\n"
        "4. Counters (pairs, SQL survivors, eliminations, matches, LLM calls) are stored in matching_runs.\n"
        "5. Triggers Workflow C."), 560, 320)
    hook = f.node("Upload completed webhook", "n8n-nodes-base.webhook", 2, (0, 0), {
        "httpMethod": "POST", "path": "loan-matching", "authentication": "headerAuth",
        "responseMode": "onReceived", "options": {}}, credentials=CRED_WEBHOOK, webhookId=_id("B/webhook"))
    validate = f.node("Validate payload", "n8n-nodes-base.code", 2, (220, 0), {"jsCode": VALIDATE_BATCH_JS})
    batch = f.node("Get upload batch", "n8n-nodes-base.httpRequest", 4.2, (440, 0), http(
        "GET", api("'/internal/batches/' + $json.upload_batch_id")), credentials=CRED_INTERNAL, **RETRY)
    ready = f.node("Batch ready?", "n8n-nodes-base.if", 2.2, (660, 0),
                   if_params("={{ $json.ready_for_matching }}", "boolean", "true"))
    not_ready = f.node("Stop: batch not ready", "n8n-nodes-base.stopAndError", 1, (880, 160), {
        "errorMessage": "={{ 'Upload batch ' + $json.upload_batch_id + ' is ' + $json.status + '; matching skipped' }}"})
    normalize = f.node("Stage 0 - normalize ambiguous product criteria (optional LLM)", "n8n-nodes-base.httpRequest",
                       4.2, (880, -80), http(
        "POST", api("'/internal/matching/' + $('Validate payload').item.json.upload_batch_id + '/normalize-criteria'")), credentials=CRED_INTERNAL, **RETRY)
    run = f.node("Stages 1-3 - SQL pre-filter, deterministic evaluation, persist", "n8n-nodes-base.httpRequest", 4.2,
                 (1120, -80), http(
        "POST", api("'/internal/matching/' + $('Validate payload').item.json.upload_batch_id + '/run'"),
        body="={{ JSON.stringify({ llm_invocations: $json.llm_invocations || 0 }) }}", timeout=900000),
                 credentials=CRED_INTERNAL, **RETRY)
    summary = f.node("Optimization summary", "n8n-nodes-base.code", 2, (1360, -80), {"jsCode": (
        "const s = $json;\n"
        "const pct = (a, b) => b ? Math.round(1000 * a / b) / 10 : 0;\n"
        "return [{ json: { upload_batch_id: s.upload_batch_id, matching_run_id: s.matching_run_id,\n"
        "  users: s.users_considered, products: s.active_products, possible_pairs: s.total_possible_pairs,\n"
        "  sql_candidates: s.sql_candidate_pairs, sql_eliminated_pct: pct(s.sql_eliminated_pairs, s.total_possible_pairs),\n"
        "  deterministic_rejections: s.deterministic_rejections, eligible: s.eligible_matches,\n"
        "  potential: s.potential_matches, llm_invocations: s.llm_invocations } }];")})
    notify = f.node("Trigger notification workflow", "n8n-nodes-base.httpRequest", 4.2, (1600, -80), http(
        "POST", self_url("'/webhook/loan-notify'"),
        body="={{ JSON.stringify({ event: 'matching.completed', upload_batch_id: $json.upload_batch_id }) }}",
        creds=CRED_WEBHOOK, timeout=30000), credentials=CRED_WEBHOOK, **RETRY)

    f.link(hook, validate)
    f.link(validate, batch)
    f.link(batch, ready)
    f.link(ready, normalize, 0)
    f.link(ready, not_ready, 1)
    f.link(normalize, run)
    f.link(run, summary)
    f.link(summary, notify)
    f.dump("workflow-b-user-matching.json", ["loan-engine", "matching"])


# --------------------------------------------------------------------------- Workflow C


def workflow_c() -> None:
    f = Flow("C - Match Notifications", "LoanEngWorkflowC")
    f.sticky("About", (-60, -360), (
        "## Workflow C - notifications\n"
        "Triggered by Workflow B only after matching COMPLETED.\n\n"
        "The backend groups matches into **one email per user**, creates notification_logs rows keyed by "
        "an idempotency key and returns only rows still to send, so retries never resend.\n\n"
        "Every message goes to **NOTIFICATION_TEST_RECIPIENT** - never to CSV addresses.\n\n"
        "Delivery mode comes from the backend (NOTIFICATION_MODE):\n"
        "- dry_run: nothing sent, logged as DRY_RUN\n- smtp: local Mailpit catcher\n- ses: AWS SES\n\n"
        "Each outcome (SENT / FAILED + error) is reported back. Failed rows are not retried automatically: "
        "they are re-sent (max 3 attempts) the next time this workflow runs for the batch, e.g. after "
        "POST /internal/batches/{id}/trigger-matching."),
                560, 360)
    hook = f.node("Matching completed webhook", "n8n-nodes-base.webhook", 2, (0, 0), {
        "httpMethod": "POST", "path": "loan-notify", "authentication": "headerAuth",
        "responseMode": "onReceived", "options": {}}, credentials=CRED_WEBHOOK, webhookId=_id("C/webhook"))
    validate = f.node("Validate payload", "n8n-nodes-base.code", 2, (220, 0), {"jsCode": VALIDATE_BATCH_JS})
    prepare = f.node("Prepare grouped outbox", "n8n-nodes-base.httpRequest", 4.2, (440, 0), http(
        "POST", api("'/internal/notifications/' + $json.upload_batch_id + '/prepare'")),
        credentials=CRED_INTERNAL, **RETRY)
    split = f.node("One item per message", "n8n-nodes-base.code", 2, (660, 0), {"jsCode": (
        "const out = $json.messages || [];\n"
        "return out.map(m => ({ json: m }));")}, alwaysOutputData=False)
    loop = f.node("Send one at a time", "n8n-nodes-base.splitInBatches", 3, (880, 0), {"batchSize": 1, "options": {}})
    is_dry = f.node("Dry run?", "n8n-nodes-base.if", 2.2, (1100, 100),
                    if_params("={{ $json.delivery_mode }}", "string", "equals", "dry_run"))
    is_ses = f.node("SES mode?", "n8n-nodes-base.if", 2.2, (1320, 200),
                    if_params("={{ $json.delivery_mode }}", "string", "equals", "ses"))
    record_dry = f.node("Record DRY_RUN", "n8n-nodes-base.httpRequest", 4.2, (1540, 0), http(
        "POST", api("'/internal/notifications/results/' + $json.notification_id"),
        body="={{ JSON.stringify({ status: 'DRY_RUN' }) }}"), credentials=CRED_INTERNAL, **RETRY)
    ses = f.node("Send via AWS SES", "n8n-nodes-base.awsSes", 1, (1540, 160), {
        "resource": "email", "operation": "send", "isBodyHtml": True,
        "subject": "={{ $json.subject }}", "body": "={{ $json.html }}", "fromEmail": "={{ $json.from }}",
        "toAddresses": ["={{ $json.to }}"], "additionalFields": {}},
        credentials=CRED_AWS, onError="continueErrorOutput")
    smtp = f.node("Send via SMTP (Mailpit)", "n8n-nodes-base.emailSend", 2.1, (1540, 360), {
        "fromEmail": "={{ $json.from }}", "toEmail": "={{ $json.to }}", "subject": "={{ $json.subject }}",
        "emailFormat": "both", "text": "={{ $json.text }}", "html": "={{ $json.html }}",
        "options": {"appendAttribution": False}}, credentials=CRED_SMTP, onError="continueErrorOutput")
    record_sent_body = ("={{ JSON.stringify({ status: 'SENT', provider_message_id: "
                        "String($json.messageId || $json.MessageId || '').slice(0, 250) || null }) }}")
    record_fail_body = ("={{ JSON.stringify({ status: 'FAILED', error: "
                        "String(($json.error && ($json.error.message || $json.error)) || 'send failed').slice(0, 500) }) }}")
    msg_id = "$('Send one at a time').item.json.notification_id"
    ses_ok = f.node("Record SES SENT", "n8n-nodes-base.httpRequest", 4.2, (1780, 100), http(
        "POST", api("'/internal/notifications/results/' + " + msg_id), body=record_sent_body),
        credentials=CRED_INTERNAL, **RETRY)
    ses_fail = f.node("Record SES FAILED", "n8n-nodes-base.httpRequest", 4.2, (1780, 220), http(
        "POST", api("'/internal/notifications/results/' + " + msg_id), body=record_fail_body),
        credentials=CRED_INTERNAL, **RETRY)
    smtp_ok = f.node("Record SMTP SENT", "n8n-nodes-base.httpRequest", 4.2, (1780, 340), http(
        "POST", api("'/internal/notifications/results/' + " + msg_id), body=record_sent_body),
        credentials=CRED_INTERNAL, **RETRY)
    smtp_fail = f.node("Record SMTP FAILED", "n8n-nodes-base.httpRequest", 4.2, (1780, 460), http(
        "POST", api("'/internal/notifications/results/' + " + msg_id), body=record_fail_body),
        credentials=CRED_INTERNAL, **RETRY)
    throttle = f.node("Throttle (SES sandbox: 1 msg/s)", "n8n-nodes-base.wait", 1.1, (2020, 200),
                      {"amount": 1, "unit": "seconds"}, webhookId=_id("C/wait"))
    finalize = f.node("Finalize batch notification status", "n8n-nodes-base.httpRequest", 4.2, (1100, -160), http(
        "POST", api("'/internal/notifications/' + $('Validate payload').first().json.upload_batch_id + '/finalize'")), credentials=CRED_INTERNAL, executeOnce=True, **RETRY)

    f.link(hook, validate)
    f.link(validate, prepare)
    f.link(prepare, split)
    f.link(split, loop)
    f.link(loop, finalize, 0)   # done
    f.link(loop, is_dry, 1)     # next message
    f.link(is_dry, record_dry, 0)
    f.link(is_dry, is_ses, 1)
    f.link(is_ses, ses, 0)
    f.link(is_ses, smtp, 1)
    f.link(ses, ses_ok, 0)
    f.link(ses, ses_fail, 1)
    f.link(smtp, smtp_ok, 0)
    f.link(smtp, smtp_fail, 1)
    for n in (record_dry, ses_ok, ses_fail, smtp_ok, smtp_fail):
        f.link(n, throttle)
    f.link(throttle, loop)
    f.dump("workflow-c-notification.json", ["loan-engine", "notifications"])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    workflow_a()
    workflow_b()
    workflow_c()
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.json"))))
