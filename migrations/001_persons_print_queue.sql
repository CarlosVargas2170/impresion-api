ALTER TABLE persons
    ADD COLUMN IF NOT EXISTS pending_to_print INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS print_status VARCHAR(20) NOT NULL DEFAULT 'not_requested',
    ADD COLUMN IF NOT EXISTS print_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS print_claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS printed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS print_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS print_error TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'persons_print_status_check'
          AND conrelid = 'persons'::regclass
    ) THEN
        ALTER TABLE persons
            ADD CONSTRAINT persons_print_status_check
            CHECK (print_status IN (
                'not_requested',
                'pending',
                'processing',
                'printed',
                'failed'
            ));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'persons_pending_to_print_check'
          AND conrelid = 'persons'::regclass
    ) THEN
        ALTER TABLE persons
            ADD CONSTRAINT persons_pending_to_print_check
            CHECK (pending_to_print IN (0, 1));
    END IF;
END
$$;

DROP INDEX IF EXISTS persons_pending_to_print_idx;

CREATE INDEX IF NOT EXISTS persons_unprinted_idx
    ON persons (print_claimed_at, created_at, id)
    WHERE pending_to_print = 0 AND is_active = TRUE;

COMMENT ON COLUMN persons.print_status IS
    'Estado de la cola: not_requested, pending, processing, printed o failed';

-- pending_to_print = 0 indica que la persona debe imprimirse.
-- Para solicitar o reintentar la impresion de una persona:
-- UPDATE persons
-- SET pending_to_print = 0,
--     print_status = 'pending',
--     print_requested_at = NOW(),
--     print_claimed_at = NULL,
--     printed_at = NULL,
--     print_error = NULL
-- WHERE id = :person_id;
-- El worker cambia el indicador a 1 antes de imprimir; si falla, vuelve a 0.
