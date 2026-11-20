-- Run in Supabase SQL Editor after taking a backup.
-- This allows multiple deployment rows for one CR and allows the same RFID
-- to be stored on each row belonging to that CR.

BEGIN;

DO $$
DECLARE
    c RECORD;
    column_names TEXT[];
BEGIN
    FOR c IN
        SELECT con.oid, con.conname, con.conrelid
        FROM pg_constraint con
        JOIN pg_class tbl ON tbl.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
        WHERE ns.nspname = 'public'
          AND tbl.relname = 'deployment_tracker'
          AND con.contype = 'u'
    LOOP
        SELECT array_agg(att.attname ORDER BY key_col.ordinality)
        INTO column_names
        FROM unnest((SELECT conkey FROM pg_constraint WHERE oid = c.oid))
             WITH ORDINALITY AS key_col(attnum, ordinality)
        JOIN pg_attribute att
          ON att.attrelid = c.conrelid
         AND att.attnum = key_col.attnum;

        IF column_names IN (ARRAY['cr_number']::TEXT[], ARRAY['rfid']::TEXT[]) THEN
            EXECUTE format(
                'ALTER TABLE public.deployment_tracker DROP CONSTRAINT %I',
                c.conname
            );
        END IF;
    END LOOP;
END $$;

-- Fast case-insensitive CR lookup; intentionally not UNIQUE.
CREATE INDEX IF NOT EXISTS idx_deployment_tracker_cr_number_normalized
    ON public.deployment_tracker (LOWER(TRIM(cr_number)));

COMMIT;
