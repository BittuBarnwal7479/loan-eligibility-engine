"""Static checks on the n8n workflow exports (importability is verified separately with the n8n CLI)."""

import json
import os
import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(os.environ.get("N8N_WORKFLOW_DIR") or Path(__file__).resolve().parents[2] / "n8n" / "workflows")
FILES = ["workflow-a-loan-discovery.json", "workflow-b-user-matching.json", "workflow-c-notification.json"]

if not WORKFLOW_DIR.exists():  # e.g. when only backend/ is mounted into a container
    pytest.skip("n8n/workflows not available in this environment", allow_module_level=True)


def load(name):
    return json.loads((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FILES)
def test_structure_is_consistent(name):
    wf = load(name)
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "node names must be unique"
    assert len({n["id"] for n in wf["nodes"]}) == len(names)
    for src, outputs in wf["connections"].items():
        assert src in names
        for branch in outputs["main"]:
            for edge in branch:
                assert edge["node"] in names, f"{src} -> {edge['node']} points to a missing node"
    connected = set(wf["connections"]) | {e["node"] for o in wf["connections"].values() for b in o["main"] for e in b}
    orphans = [
        n["name"] for n in wf["nodes"] if n["name"] not in connected and n["type"] != "n8n-nodes-base.stickyNote"
    ]
    assert not orphans, orphans


@pytest.mark.parametrize("name", FILES)
def test_no_secrets_or_hardcoded_hosts(name):
    raw = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
    assert not re.search(r"(X-Internal-Token|X-Webhook-Token)\"\s*,\s*\"value\"", raw)
    assert not re.search(r"(AKIA[0-9A-Z]{16}|sk-ant-|password\"\s*:\s*\"[^\"]+)", raw)
    wf = load(name)
    for node in wf["nodes"]:
        url = node["parameters"].get("url", "")
        if "/internal/" in url:
            assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
            assert node["credentials"]["httpHeaderAuth"]["id"] == "LoanEngInternal1"


@pytest.mark.parametrize("name", ["workflow-b-user-matching.json", "workflow-c-notification.json"])
def test_webhooks_require_header_auth(name):
    hooks = [n for n in load(name)["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
    assert hooks and all(h["parameters"]["authentication"] == "headerAuth" for h in hooks)
    assert all(h["parameters"]["httpMethod"] == "POST" and h.get("webhookId") for h in hooks)


def test_discovery_has_schedule_manual_robots_and_no_llm():
    wf = load("workflow-a-loan-discovery.json")
    types = {n["type"] for n in wf["nodes"]}
    assert {"n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.manualTrigger"} <= types
    urls = " ".join(n["parameters"].get("url", "") for n in wf["nodes"])
    assert "robots-check" in urls and "refresh-staleness" in urls
    assert "anthropic" not in json.dumps(wf).lower() and "openai" not in json.dumps(wf).lower()


def test_matching_stages_in_order():
    wf = load("workflow-b-user-matching.json")
    order, node = [], "Upload completed webhook"
    while node in wf["connections"]:
        node = wf["connections"][node]["main"][0][0]["node"]
        order.append(node)
    joined = " | ".join(order)
    assert (
        joined.index("Get upload batch")
        < joined.index("Stage 0")
        < joined.index("Stages 1-3")
        < joined.index("Trigger notification workflow")
    )


def test_notifications_record_every_outcome():
    wf = load("workflow-c-notification.json")
    by_name = {n["name"]: n for n in wf["nodes"]}
    for sender in ("Send via AWS SES", "Send via SMTP (Mailpit)"):
        assert by_name[sender]["onError"] == "continueErrorOutput"
        success, failure = wf["connections"][sender]["main"]
        assert "SENT" in success[0]["node"] and "FAILED" in failure[0]["node"]
    assert by_name["Send via AWS SES"]["parameters"]["toAddresses"] == ["={{ $json.to }}"]
