
CREATE TABLE IF NOT EXISTS public.release_calendar_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_cr_per_slot INTEGER NOT NULL DEFAULT 1 CHECK (max_cr_per_slot BETWEEN 1 AND 100),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO public.release_calendar_config (id, max_cr_per_slot)
VALUES (1, 1)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.release_calendar_blocks (
    blocked_date DATE PRIMARY KEY,
    message VARCHAR(1000) NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
