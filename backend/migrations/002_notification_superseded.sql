-- 002_notification_superseded.sql
-- A notification row becomes SUPERSEDED when a matching re-run changes the user's match set
-- before the row was delivered. The new match set gets its own row (new idempotency key);
-- the superseded row is never sent and does not block batch completion.

ALTER TABLE notification_logs DROP CONSTRAINT notification_logs_status_check;
ALTER TABLE notification_logs ADD CONSTRAINT notification_logs_status_check
    CHECK (status IN ('PENDING', 'SENT', 'DRY_RUN', 'FAILED', 'GAVE_UP', 'SUPERSEDED'));
