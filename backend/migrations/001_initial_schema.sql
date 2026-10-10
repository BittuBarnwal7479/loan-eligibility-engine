-- 001_initial_schema.sql
-- Core schema for the Loan Eligibility Engine.
-- Applied by `python -m app.migrate` (tracked in schema_migrations).

CREATE TABLE upload_batches (
    id                       uuid PRIMARY KEY,
    original_filename        text        NOT NULL,
    storage_bucket           text,
    storage_key              text        NOT NULL UNIQUE,
    status                   text        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')),
    content_sha256           text,
    total_rows               integer     NOT NULL DEFAULT 0,
    processed_rows           integer     NOT NULL DEFAULT 0,
    inserted_rows            integer     NOT NULL DEFAULT 0,
    updated_rows             integer     NOT NULL DEFAULT 0,
    failed_rows              integer     NOT NULL DEFAULT 0,
    -- Capped list of {row, field, code, message}; never contains raw cell values.
    row_errors               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    error_message            text,
    attempt_count            integer     NOT NULL DEFAULT 0,
    processing_started_at    timestamptz,
    processing_completed_at  timestamptz,
    matching_status          text        NOT NULL DEFAULT 'NOT_STARTED'
        CHECK (matching_status IN ('NOT_STARTED', 'TRIGGERED', 'TRIGGER_FAILED', 'RUNNING', 'COMPLETED', 'FAILED')),
    matching_triggered_at    timestamptz,
    matching_error           text,
    notification_status      text        NOT NULL DEFAULT 'NOT_STARTED'
        CHECK (notification_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'COMPLETED_WITH_ERRORS')),
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX upload_batches_status_idx ON upload_batches (status, created_at);

CREATE TABLE users (
    id                 bigserial PRIMARY KEY,
    user_id            text          NOT NULL UNIQUE,
    email              text          NOT NULL,
    monthly_income     numeric(14,2) NOT NULL CHECK (monthly_income >= 0),
    credit_score       integer       NOT NULL CHECK (credit_score BETWEEN 300 AND 900),
    employment_status  text          NOT NULL
        CHECK (employment_status IN ('salaried', 'self_employed', 'unemployed', 'student', 'retired')),
    age                integer       NOT NULL CHECK (age BETWEEN 18 AND 100),
    -- The batch that created or most recently updated this record.
    upload_batch_id    uuid          NOT NULL REFERENCES upload_batches (id),
    created_at         timestamptz   NOT NULL DEFAULT now(),
    updated_at         timestamptz   NOT NULL DEFAULT now()
);

-- Keyset pagination over one batch + range filters used by the SQL pre-filter.
CREATE INDEX users_batch_id_idx ON users (upload_batch_id, id);
CREATE INDEX users_batch_score_income_idx ON users (upload_batch_id, credit_score, monthly_income);

CREATE TABLE loan_products (
    id                         bigserial PRIMARY KEY,
    lender_name                text          NOT NULL,
    product_name               text          NOT NULL,
    source_key                 text,
    source_url                 text          NOT NULL,
    source_attribution         text          NOT NULL,
    data_origin                text          NOT NULL CHECK (data_origin IN ('crawled', 'dev_fixture', 'manual')),
    interest_rate_min          numeric(5,2),
    interest_rate_max          numeric(5,2),
    interest_rate_text         text,
    -- NULL in any criterion column means "unknown", never "no requirement".
    min_monthly_income         numeric(14,2),
    min_credit_score           integer,
    max_credit_score           integer,
    min_age                    integer,
    max_age                    integer,
    employment_types           text[],
    employment_types_source    text CHECK (employment_types_source IN ('extracted', 'llm_normalized', 'manual', 'fixture')),
    employment_criteria_text   text,
    other_criteria             jsonb         NOT NULL DEFAULT '[]'::jsonb,
    extraction_notes           jsonb         NOT NULL DEFAULT '[]'::jsonb,
    last_verified_at           timestamptz,
    last_crawl_attempt_at      timestamptz,
    consecutive_crawl_failures integer       NOT NULL DEFAULT 0,
    is_active                  boolean       NOT NULL DEFAULT true,
    is_stale                   boolean       NOT NULL DEFAULT false,
    created_at                 timestamptz   NOT NULL DEFAULT now(),
    updated_at                 timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT loan_products_lender_product_uq UNIQUE (lender_name, product_name),
    CONSTRAINT loan_products_rate_range_ck CHECK (interest_rate_min IS NULL OR interest_rate_max IS NULL OR interest_rate_min <= interest_rate_max),
    CONSTRAINT loan_products_rate_bounds_ck CHECK (
        (interest_rate_min IS NULL OR interest_rate_min BETWEEN 0 AND 60) AND
        (interest_rate_max IS NULL OR interest_rate_max BETWEEN 0 AND 60)),
    CONSTRAINT loan_products_score_range_ck CHECK (min_credit_score IS NULL OR max_credit_score IS NULL OR min_credit_score <= max_credit_score),
    CONSTRAINT loan_products_age_range_ck CHECK (min_age IS NULL OR max_age IS NULL OR min_age <= max_age)
);

CREATE INDEX loan_products_active_idx ON loan_products (id) WHERE is_active;

CREATE TABLE matching_runs (
    id                        uuid PRIMARY KEY,
    upload_batch_id           uuid        NOT NULL REFERENCES upload_batches (id),
    status                    text        NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    users_considered          integer     NOT NULL DEFAULT 0,
    active_products           integer     NOT NULL DEFAULT 0,
    total_possible_pairs      bigint      NOT NULL DEFAULT 0,
    sql_candidate_pairs       bigint      NOT NULL DEFAULT 0,
    sql_eliminated_pairs      bigint      NOT NULL DEFAULT 0,
    deterministic_rejections  bigint      NOT NULL DEFAULT 0,
    eligible_matches          bigint      NOT NULL DEFAULT 0,
    potential_matches         bigint      NOT NULL DEFAULT 0,
    llm_invocations           integer     NOT NULL DEFAULT 0,
    chunks_processed          integer     NOT NULL DEFAULT 0,
    error_message             text,
    started_at                timestamptz NOT NULL DEFAULT now(),
    finished_at               timestamptz
);

CREATE INDEX matching_runs_batch_idx ON matching_runs (upload_batch_id, started_at DESC);

CREATE TABLE matches (
    id                 bigserial PRIMARY KEY,
    user_id            bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    loan_product_id    bigint      NOT NULL REFERENCES loan_products (id) ON DELETE CASCADE,
    upload_batch_id    uuid        NOT NULL REFERENCES upload_batches (id),
    matching_run_id    uuid        REFERENCES matching_runs (id),
    -- ELIGIBLE: every standard criterion was known and passed.
    -- POTENTIAL: every known criterion passed, at least one criterion is unknown.
    match_status       text        NOT NULL CHECK (match_status IN ('ELIGIBLE', 'POTENTIAL')),
    -- Fraction of the standard criteria that were known for this product (0..1).
    criteria_coverage  numeric(4,3) NOT NULL CHECK (criteria_coverage BETWEEN 0 AND 1),
    passed_criteria    jsonb       NOT NULL,
    unknown_criteria   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    decided_by         text        NOT NULL DEFAULT 'deterministic',
    matched_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT matches_user_product_batch_uq UNIQUE (user_id, loan_product_id, upload_batch_id)
);

CREATE INDEX matches_batch_user_idx ON matches (upload_batch_id, user_id);
CREATE INDEX matches_product_idx ON matches (loan_product_id);

CREATE TABLE notification_logs (
    id                    bigserial PRIMARY KEY,
    user_id               bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    upload_batch_id       uuid        NOT NULL REFERENCES upload_batches (id),
    -- The address the message is actually sent to (always the configured test inbox).
    recipient_email       text        NOT NULL,
    -- Masked original address for traceability, e.g. "a***@example.com".
    original_recipient_masked text    NOT NULL,
    match_count           integer     NOT NULL,
    subject               text        NOT NULL,
    delivery_mode         text        NOT NULL CHECK (delivery_mode IN ('dry_run', 'smtp', 'ses')),
    status                text        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENT', 'DRY_RUN', 'FAILED', 'GAVE_UP')),
    provider_message_id   text,
    attempt_count         integer     NOT NULL DEFAULT 0,
    last_error            text,
    claimed_at            timestamptz,
    idempotency_key       text        NOT NULL UNIQUE,
    sent_at               timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX notification_logs_batch_status_idx ON notification_logs (upload_batch_id, status);
