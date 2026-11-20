ALTER TABLE deployment_tracker
    ADD COLUMN IF NOT EXISTS slot_start TIMESTAMP,
    ADD COLUMN IF NOT EXISTS slot_end TIMESTAMP;

ALTER TABLE deployment_tracker
ADD CONSTRAINT chk_slot_duration
CHECK (
    slot_start IS NULL
    OR slot_end IS NULL
    OR slot_end = slot_start + INTERVAL '30 minutes'
);

CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE deployment_tracker
    ADD COLUMN IF NOT EXISTS slot_start TIMESTAMP,
    ADD COLUMN IF NOT EXISTS slot_end TIMESTAMP;
ALTER TABLE deployment_tracker
ADD CONSTRAINT chk_slot_duration
CHECK (
    slot_start IS NOT NULL
    AND slot_end = slot_start + INTERVAL '30 minutes'
);

ALTER TABLE deployment_tracker
ADD CONSTRAINT no_overlapping_deployment_slots
EXCLUDE USING gist (
    tsrange(slot_start, slot_end, '[)') WITH &&
);

CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE deployment_tracker
ADD CONSTRAINT no_overlapping_deployment_slots
EXCLUDE USING gist (
    tsrange(slot_start, slot_end, '[)') WITH &&
);


BEGIN;

-- Remove the existing exclusion constraint.
ALTER TABLE public.deployment_tracker
DROP CONSTRAINT no_overlapping_deployment_slots;

-- Allow slot conflicts to be checked only for records
-- that have both timestamps populated.
ALTER TABLE public.deployment_tracker
ADD CONSTRAINT no_overlapping_deployment_slots
EXCLUDE USING gist (
    tsrange(slot_start, slot_end, '[)') WITH &&
)
WHERE (
    slot_start IS NOT NULL
    AND slot_end IS NOT NULL
);

-- Ensure both slot timestamps are either NULL together
-- or populated together.
ALTER TABLE public.deployment_tracker
ADD CONSTRAINT deployment_slot_pair_check
CHECK (
    (slot_start IS NULL AND slot_end IS NULL)
    OR
    (slot_start IS NOT NULL AND slot_end IS NOT NULL)
);

COMMIT;