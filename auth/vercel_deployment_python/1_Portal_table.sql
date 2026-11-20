
BEGIN;

-- =========================================================
-- 1. Required extension
-- =========================================================
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- =========================================================
-- 2. Create table for a new database
-- =========================================================
CREATE TABLE IF NOT EXISTS public.deployment_tracker (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    cr_number VARCHAR(50) NOT NULL,

    rfid CHAR(8) NOT NULL DEFAULT LPAD(
        FLOOR(RANDOM() * 100000000)::BIGINT::TEXT,
        8,
        '0'
    ),

    deployment_date DATE NOT NULL DEFAULT CURRENT_DATE,
    slots VARCHAR(100),
    slot_start TIMESTAMP WITHOUT TIME ZONE,
    slot_end TIMESTAMP WITHOUT TIME ZONE,

    application_name VARCHAR(150) NOT NULL,

    deployment_type VARCHAR(20) NOT NULL
        CHECK (deployment_type IN ('Automation', 'Manual')),

    change_type VARCHAR(30) NOT NULL
        CHECK (change_type IN (
            'Normal CR',
            'Emergency CR',
            'Standard CR',
            'Forward Fix CR',
            'Bug Fix CR'
        )),

    released_by VARCHAR(150),
    verified_and_deployed_by VARCHAR(150),
    change_requestor VARCHAR(150),
    release_notes TEXT,
    incident_number VARCHAR(50),
    comments TEXT,

    roll_back VARCHAR(3) NOT NULL DEFAULT 'No'
        CHECK (roll_back IN ('Yes', 'No')),

    status VARCHAR(30) NOT NULL DEFAULT 'Pending'
        CHECK (status IN (
            'Pending',
            'Scheduled',
            'In Progress',
            'Successful',
            'Failed',
            'Rolled Back',
            'Cancelled',
            'Approved',
            'Rejected'
        )),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_rfid_8_digits
        CHECK (rfid ~ '^[0-9]{8}$'),

    CONSTRAINT deployment_slot_pair_check
        CHECK (
            (slot_start IS NULL AND slot_end IS NULL)
            OR
            (slot_start IS NOT NULL AND slot_end IS NOT NULL)
        ),

    CONSTRAINT chk_slot_duration
        CHECK (
            slot_start IS NULL
            OR slot_end = slot_start + INTERVAL '30 minutes'
        )
);

-- =========================================================
-- 3. Add missing columns to existing tables
-- =========================================================
ALTER TABLE public.deployment_tracker
    ADD COLUMN IF NOT EXISTS slot_start TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS slot_end TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS comments TEXT;

-- =========================================================
-- 4. Remove unique constraints on CR and RFID
--    Multiple deployment records can share either value.
-- =========================================================
DO $$
DECLARE
    c RECORD;
    column_names TEXT[];
BEGIN
    FOR c IN
        SELECT con.conname, con.conrelid, con.conkey
        FROM pg_constraint con
        JOIN pg_class tbl ON tbl.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
        WHERE ns.nspname = 'public'
          AND tbl.relname = 'deployment_tracker'
          AND con.contype = 'u'
    LOOP
        SELECT array_agg(att.attname ORDER BY k.ordinality)
        INTO column_names
        FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
        JOIN pg_attribute att
          ON att.attrelid = c.conrelid
         AND att.attnum = k.attnum;

        IF column_names IN (
            ARRAY['cr_number']::TEXT[],
            ARRAY['rfid']::TEXT[]
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.deployment_tracker DROP CONSTRAINT %I',
                c.conname
            );
        END IF;
    END LOOP;
END $$;

-- =========================================================
-- 5. Refresh slot and status constraints
-- =========================================================
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS deployment_slot_pair_check,
    DROP CONSTRAINT IF EXISTS chk_slot_duration,
    DROP CONSTRAINT IF EXISTS deployment_tracker_status_check,
    DROP CONSTRAINT IF EXISTS no_overlapping_deployment_slots;

ALTER TABLE public.deployment_tracker
    ADD CONSTRAINT deployment_slot_pair_check
    CHECK (
        (slot_start IS NULL AND slot_end IS NULL)
        OR
        (slot_start IS NOT NULL AND slot_end IS NOT NULL)
    ),

    ADD CONSTRAINT chk_slot_duration
    CHECK (
        slot_start IS NULL
        OR slot_end = slot_start + INTERVAL '30 minutes'
    ),

    ADD CONSTRAINT deployment_tracker_status_check
    CHECK (status IN (
        'Pending',
        'Scheduled',
        'In Progress',
        'Successful',
        'Failed',
        'Rolled Back',
        'Cancelled',
        'Approved',
        'Rejected'
    )),

    ADD CONSTRAINT no_overlapping_deployment_slots
    EXCLUDE USING gist (
        tsrange(slot_start, slot_end, '[)') WITH &&
    )
    WHERE (
        slot_start IS NOT NULL
        AND slot_end IS NOT NULL
        AND LOWER(TRIM(COALESCE(status, '')))
            NOT IN ('cancelled', 'rejected')
    );

-- =========================================================
-- 6. Create indexes
-- =========================================================
CREATE INDEX IF NOT EXISTS idx_deployment_tracker_cr_normalized
    ON public.deployment_tracker (LOWER(TRIM(cr_number)));

CREATE INDEX IF NOT EXISTS idx_deployment_date
    ON public.deployment_tracker (deployment_date);

CREATE INDEX IF NOT EXISTS idx_application_name
    ON public.deployment_tracker (application_name);

CREATE INDEX IF NOT EXISTS idx_deployment_status
    ON public.deployment_tracker (status);

COMMIT;
