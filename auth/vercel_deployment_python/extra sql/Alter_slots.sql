
BEGIN;

-- 1. Ensure slot columns exist.
ALTER TABLE public.deployment_tracker
    ADD COLUMN IF NOT EXISTS slot_start TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS slot_end   TIMESTAMP WITHOUT TIME ZONE;

-- 2. Ensure both slot timestamps are either NULL together
--    or populated together.
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS deployment_slot_pair_check;

ALTER TABLE public.deployment_tracker
    ADD CONSTRAINT deployment_slot_pair_check
    CHECK (
        (slot_start IS NULL AND slot_end IS NULL)
        OR
        (slot_start IS NOT NULL AND slot_end IS NOT NULL)
    );

-- 3. Enforce exactly 30 minutes for scheduled deployments.
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS chk_slot_duration;

ALTER TABLE public.deployment_tracker
    ADD CONSTRAINT chk_slot_duration
    CHECK (
        slot_start IS NULL
        OR slot_end = slot_start + INTERVAL '30 minutes'
    );

CREATE EXTENSION IF NOT EXISTS btree_gist;


ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS no_overlapping_deployment_slots;

ALTER TABLE public.deployment_tracker
    ADD CONSTRAINT no_overlapping_deployment_slots
    EXCLUDE USING gist (
        tsrange(slot_start, slot_end, '[)') WITH &&
    )
    WHERE (
        slot_start IS NOT NULL
        AND slot_end IS NOT NULL
    );

COMMIT;



ALTER TABLE public.deployment_tracker
DROP CONSTRAINT deployment_tracker_status_check;

ALTER TABLE public.deployment_tracker
ADD CONSTRAINT deployment_tracker_status_check
CHECK (
    status IN (
        'Pending',
        'Scheduled',
        'In Progress',
        'Successful',
        'Failed',
        'Rolled Back',
        'Cancelled',
        'Approved',
        'Rejected'
    )
);