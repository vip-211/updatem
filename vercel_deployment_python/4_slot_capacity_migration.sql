-- REQUIRED: allow multiple deployments in a slot, up to Super Manager capacity.
-- Run once in the Supabase SQL editor before using the updated app.
-- The old exclusion constraint blocks every overlap, even when capacity is 4.
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS no_overlapping_deployment_slots;

-- Keep capacity config present with the default of 1.
CREATE TABLE IF NOT EXISTS public.release_calendar_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_cr_per_slot INTEGER NOT NULL DEFAULT 1 CHECK (max_cr_per_slot BETWEEN 1 AND 100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO public.release_calendar_config (id, max_cr_per_slot)
VALUES (1, 1)
ON CONFLICT (id) DO NOTHING;

-- Keep blocked-date messages available to the calendar.
CREATE TABLE IF NOT EXISTS public.release_calendar_blocks (
    blocked_date DATE PRIMARY KEY,
    message VARCHAR(1000) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
