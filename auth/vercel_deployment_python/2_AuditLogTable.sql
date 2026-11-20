CREATE TABLE IF NOT EXISTS public.deployment_tracker_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_time TIMESTAMP WITHOUT TIME ZONE NOT NULL
        DEFAULT CURRENT_TIMESTAMP,
    log_level VARCHAR(20) NOT NULL,
    message TEXT NOT NULL,
    cr_number VARCHAR(50),
    rfid CHAR(8)
);

BEGIN;

CREATE TABLE IF NOT EXISTS public.audit_users (
    email VARCHAR(254) PRIMARY KEY,
    passcode_digest CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP WITHOUT TIME ZONE,
    failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until TIMESTAMP WITHOUT TIME ZONE
);

CREATE TABLE IF NOT EXISTS public.deployment_change_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deployment_id BIGINT NOT NULL,
    cr_number VARCHAR(50),
    rfid CHAR(8),
    modified_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_by VARCHAR(254) NOT NULL,
    action VARCHAR(30) NOT NULL,
    changes JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE public.deployment_change_history
    ALTER COLUMN modified_by TYPE VARCHAR(254);

CREATE INDEX IF NOT EXISTS idx_deployment_change_history_modified_at
    ON public.deployment_change_history (modified_at DESC);

CREATE INDEX IF NOT EXISTS idx_deployment_change_history_deployment_id
    ON public.deployment_change_history (deployment_id, modified_at DESC);

COMMIT;
