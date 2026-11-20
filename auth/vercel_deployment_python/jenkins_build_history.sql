-- Persistent Jenkins build/deployment history, linked by normalized CR number.
-- Safe to run repeatedly. The Flask application also creates this table automatically.
CREATE TABLE IF NOT EXISTS public.jenkins_build_history (
    cr_number TEXT NOT NULL,
    build_url TEXT NOT NULL,
    job_name TEXT,
    build_number TEXT,
    server TEXT,
    build_type TEXT,
    platform TEXT,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    triggered_by TEXT,
    status TEXT,
    building BOOLEAN NOT NULL DEFAULT FALSE,
    duration_ms BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (build_url, cr_number)
);

CREATE INDEX IF NOT EXISTS idx_jenkins_build_history_cr_start
    ON public.jenkins_build_history (cr_number, start_time DESC);
