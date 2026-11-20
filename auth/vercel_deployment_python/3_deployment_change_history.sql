CREATE TABLE IF NOT EXISTS public.deployment_change_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deployment_id BIGINT NOT NULL,
    cr_number VARCHAR(50),
    rfid CHAR(8),
    modified_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    modified_by VARCHAR(150) NOT NULL,
    action VARCHAR(30) NOT NULL,
    changes JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_deployment_change_history_modified_at
    ON public.deployment_change_history (modified_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_change_history_deployment_id
    ON public.deployment_change_history (deployment_id, modified_at DESC);
