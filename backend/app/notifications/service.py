"""Notification outbox for n8n Workflow C.

Flow
----
1. ``prepare_outbox`` groups the batch's matches per user (one email per user,
   not per product), creates one ``notification_logs`` row per user keyed by a
   deterministic idempotency key, and *claims* the rows that still need sending.
2. n8n delivers each claimed message (dry run, SMTP to a local mail catcher, or
   AWS SES) and calls ``record_result``.
3. Rows already SENT / DRY_RUN are never returned again, so workflow retries do
   not produce duplicate emails.

Safety: messages are always addressed to NOTIFICATION_TEST_RECIPIENT. Email
addresses from the uploaded CSV are never used as recipients; they only appear
masked in the log.
"""

from __future__ import annotations

import hashlib
import html
import logging
import uuid
from collections import defaultdict
from typing import Any

import psycopg

from app.config import Settings
from app.security import mask_email

log = logging.getLogger(__name__)

DISCLAIMER = (
    "These are preliminary matches based only on publicly published criteria and the details in your "
    "uploaded profile. They are not loan offers, approvals or financial advice. Lenders make the final "
    "decision using their own checks, and published rates and criteria can change."
)
DRY_RUN_RECIPIENT = "dry-run@localhost.invalid"


class NotificationConfigError(RuntimeError):
    pass


def resolve_recipient(settings: Settings) -> str:
    if settings.notification_test_recipient:
        return settings.notification_test_recipient
    if settings.notification_mode == "dry_run":
        return DRY_RUN_RECIPIENT
    raise NotificationConfigError("NOTIFICATION_TEST_RECIPIENT must be set for smtp/ses delivery")


def idempotency_key(batch_id: uuid.UUID, user_pk: int, product_ids: list[int]) -> str:
    material = f"{batch_id}|{user_pk}|{','.join(str(p) for p in sorted(product_ids))}"
    return hashlib.sha256(material.encode()).hexdigest()


def _rate(product: dict[str, Any]) -> str:
    low, high = product["interest_rate_min"], product["interest_rate_max"]
    if low is not None and high is not None:
        return f"{low.normalize():f}% - {high.normalize():f}% p.a."
    if low is not None:
        return f"from {low.normalize():f}% p.a. (as published)"
    return "not published / not extracted"


def render_email(user: dict[str, Any], items: list[dict[str, Any]]) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for one user's grouped matches."""
    count = len(items)
    subject = f"{count} potential loan match{'es' if count != 1 else ''} for your profile"
    any_fixture = any(i["data_origin"] == "dev_fixture" for i in items)
    rows_html, rows_text = [], []
    for i in items:
        badge = (
            "Meets all published criteria we could check"
            if i["match_status"] == "ELIGIBLE"
            else ("Meets the criteria we know; some criteria could not be checked")
        )
        unknown = [u["detail"] for u in i["unknown_criteria"]]
        unknown_html = (
            "<ul style='margin:4px 0 0 18px;color:#6b4e00'>"
            + "".join(f"<li>{html.escape(u)}</li>" for u in unknown)
            + "</ul>"
            if unknown
            else ""
        )
        fixture_tag = (
            " <strong style='color:#b00020'>[TEST FIXTURE - not a real product]</strong>"
            if i["data_origin"] == "dev_fixture"
            else ""
        )
        rows_html.append(
            "<tr><td style='padding:12px;border-bottom:1px solid #e5e7eb'>"
            f"<div style='font-weight:600'>{html.escape(i['product_name'])}{fixture_tag}</div>"
            f"<div style='color:#4b5563'>{html.escape(i['lender_name'])}</div>"
            f"<div>Published interest rate: {html.escape(_rate(i))}</div>"
            f"<div style='color:#065f46'>{html.escape(badge)}</div>{unknown_html}"
            f"<div><a href='{html.escape(i['source_url'], quote=True)}'>Lender source page</a></div>"
            "</td></tr>"
        )
        rows_text.append(
            f"- {i['product_name']} ({i['lender_name']}){' [TEST FIXTURE]' if i['data_origin'] == 'dev_fixture' else ''}\n"
            f"  Rate: {_rate(i)}\n  Status: {badge}\n"
            + "".join(f"  Not checked: {u}\n" for u in unknown)
            + f"  Source: {i['source_url']}\n"
        )
    fixture_note = (
        "<p style='color:#b00020'><strong>Test data:</strong> some products below are synthetic development fixtures.</p>"
        if any_fixture
        else ""
    )
    routing = (
        f"Test delivery: intended for user {html.escape(user['user_id'])} ({html.escape(mask_email(user['email']))})."
    )
    body_html = (
        "<!doctype html><html><body style='font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:640px;margin:auto'>"
        f"<p style='font-size:12px;color:#6b7280'>{routing}</p>"
        "<h2 style='margin-bottom:4px'>Your preliminary loan matches</h2>"
        f"<p>Based on the profile you uploaded, we found {count} product{'s' if count != 1 else ''} whose published "
        "eligibility criteria you appear to meet.</p>"
        f"{fixture_note}<table style='width:100%;border-collapse:collapse'>{''.join(rows_html)}</table>"
        f"<p style='font-size:13px;color:#374151;background:#f3f4f6;padding:12px;border-radius:6px'>{html.escape(DISCLAIMER)}</p>"
        "</body></html>"
    )
    body_text = (
        f"{routing}\n\nYour preliminary loan matches ({count}):\n\n" + "\n".join(rows_text) + f"\n{DISCLAIMER}\n"
    )
    return subject, body_html, body_text


MATCHES_FOR_BATCH_SQL = """
SELECT m.user_id AS user_pk, u.user_id, u.email, m.loan_product_id, m.match_status, m.unknown_criteria,
       p.product_name, p.lender_name, p.source_url, p.interest_rate_min, p.interest_rate_max, p.data_origin
  FROM matches m
  JOIN users u ON u.id = m.user_id
  JOIN loan_products p ON p.id = m.loan_product_id
 WHERE m.upload_batch_id = %s
 ORDER BY m.user_id, (m.match_status = 'ELIGIBLE') DESC, p.interest_rate_min NULLS LAST, p.id
"""

CLAIM_SQL = """
UPDATE notification_logs n
   SET claimed_at = now(), attempt_count = attempt_count + 1, updated_at = now()
  FROM (SELECT id FROM notification_logs
         WHERE upload_batch_id = %(batch_id)s
           AND status IN ('PENDING', 'FAILED')
           AND attempt_count < %(max_attempts)s
           AND (claimed_at IS NULL OR claimed_at < now() - interval '10 minutes')
         ORDER BY id
         LIMIT %(limit)s
         FOR UPDATE SKIP LOCKED) c
 WHERE n.id = c.id
RETURNING n.id, n.user_id, n.idempotency_key, n.attempt_count
"""


def prepare_outbox(
    settings: Settings, conn: psycopg.Connection, batch_id: uuid.UUID, limit: int = 200
) -> dict[str, Any]:
    batch = conn.execute("SELECT matching_status FROM upload_batches WHERE id = %s", (batch_id,)).fetchone()
    if batch is None:
        raise LookupError("upload batch not found")
    if batch["matching_status"] != "COMPLETED":
        raise NotificationConfigError(
            f"matching_status is {batch['matching_status']}; notifications wait for COMPLETED"
        )
    recipient = resolve_recipient(settings)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    users: dict[int, dict[str, Any]] = {}
    for row in conn.execute(MATCHES_FOR_BATCH_SQL, (batch_id,)).fetchall():
        grouped[row["user_pk"]].append(row)
        users[row["user_pk"]] = {"user_id": row["user_id"], "email": row["email"]}

    rendered: dict[int, tuple[str, str, str]] = {}
    current_keys: list[str] = []
    created = 0
    for user_pk, items in grouped.items():
        subject, body_html, body_text = render_email(users[user_pk], items)
        rendered[user_pk] = (subject, body_html, body_text)
        key = idempotency_key(batch_id, user_pk, [i["loan_product_id"] for i in items])
        current_keys.append(key)
        created += conn.execute(
            "INSERT INTO notification_logs (user_id, upload_batch_id, recipient_email, original_recipient_masked,"
            " match_count, subject, delivery_mode, idempotency_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING",
            (
                user_pk,
                batch_id,
                recipient,
                mask_email(users[user_pk]["email"]),
                len(items),
                subject,
                settings.notification_mode,
                key,
            ),
        ).rowcount

    # Undelivered rows for a match set that no longer exists (matching was re-run with different
    # results) must neither be sent alongside the new row nor block completion.
    conn.execute(
        "UPDATE notification_logs SET status = 'SUPERSEDED', claimed_at = NULL, updated_at = now(),"
        " last_error = 'match set changed by a later matching run'"
        " WHERE upload_batch_id = %s AND status IN ('PENDING', 'FAILED') AND NOT (idempotency_key = ANY(%s))",
        (batch_id, current_keys),
    )

    claimed = conn.execute(
        CLAIM_SQL, {"batch_id": batch_id, "max_attempts": settings.notification_max_attempts, "limit": limit}
    ).fetchall()
    if claimed:
        conn.execute(
            "UPDATE upload_batches SET notification_status = 'IN_PROGRESS', updated_at = now() WHERE id = %s",
            (batch_id,),
        )
    else:
        # Nothing to send (no matches, or a retry after everything was delivered): the workflow
        # has no items to loop over, so settle the batch status here.
        finalize(conn, batch_id)
    messages = []
    for c in claimed:
        if c["user_id"] not in rendered:
            continue  # match set changed since the log row was created; leave it for finalize to report
        subject, body_html, body_text = rendered[c["user_id"]]
        messages.append(
            {
                "notification_id": c["id"],
                "idempotency_key": c["idempotency_key"],
                "attempt": c["attempt_count"],
                "delivery_mode": settings.notification_mode,
                "to": recipient,
                "from": settings.notification_from_email or "loan-engine@localhost.invalid",
                "subject": subject,
                "html": body_html,
                "text": body_text,
            }
        )
    log.info(
        "notification outbox prepared",
        extra={
            "batch_id": str(batch_id),
            "users_with_matches": len(grouped),
            "new_logs": created,
            "claimed": len(messages),
        },
    )
    return {
        "upload_batch_id": str(batch_id),
        "delivery_mode": settings.notification_mode,
        "users_with_matches": len(grouped),
        "new_notifications": created,
        "messages": messages,
    }


def record_result(
    settings: Settings,
    conn: psycopg.Connection,
    notification_id: int,
    status: str,
    provider_message_id: str | None,
    error: str | None,
) -> dict[str, Any]:
    if status not in {"SENT", "DRY_RUN", "FAILED"}:
        raise ValueError("status must be SENT, DRY_RUN or FAILED")
    row = conn.execute("SELECT * FROM notification_logs WHERE id = %s FOR UPDATE", (notification_id,)).fetchone()
    if row is None:
        raise LookupError("notification not found")
    if row["status"] in ("SENT", "DRY_RUN"):
        return {"notification_id": notification_id, "status": row["status"], "duplicate": True}
    if row["status"] == "SUPERSEDED" and status != "SENT":
        # Superseded while in flight; only a real delivery is worth recording over it.
        return {"notification_id": notification_id, "status": row["status"], "duplicate": True}
    if status == "DRY_RUN" and row["delivery_mode"] != "dry_run":
        raise ValueError("DRY_RUN result is only valid for dry_run delivery mode")
    if status == "SENT" and row["delivery_mode"] == "dry_run":
        raise ValueError("SENT is not valid in dry_run mode")
    new_status = status
    if status == "FAILED" and row["attempt_count"] >= settings.notification_max_attempts:
        new_status = "GAVE_UP"
    conn.execute(
        "UPDATE notification_logs SET status = %s, provider_message_id = %s, last_error = %s, claimed_at = NULL,"
        " sent_at = CASE WHEN %s IN ('SENT', 'DRY_RUN') THEN now() END, updated_at = now() WHERE id = %s",
        (new_status, provider_message_id, (error or "")[:500] or None, new_status, notification_id),
    )
    return {"notification_id": notification_id, "status": new_status, "duplicate": False}


def finalize(conn: psycopg.Connection, batch_id: uuid.UUID) -> dict[str, Any]:
    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, count(*) AS n FROM notification_logs WHERE upload_batch_id = %s GROUP BY status",
            (batch_id,),
        ).fetchall()
    }
    outstanding = counts.get("PENDING", 0) + counts.get("FAILED", 0)
    if outstanding:
        status = "IN_PROGRESS"
    elif counts.get("GAVE_UP"):
        status = "COMPLETED_WITH_ERRORS"
    else:
        status = "COMPLETED"
    conn.execute(
        "UPDATE upload_batches SET notification_status = %s, updated_at = now() WHERE id = %s", (status, batch_id)
    )
    return {"upload_batch_id": str(batch_id), "notification_status": status, "counts": counts}
