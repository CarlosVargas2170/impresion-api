ALTER TABLE persons
    DROP CONSTRAINT IF EXISTS persons_print_status_check;

ALTER TABLE persons
    ALTER COLUMN print_status DROP NOT NULL,
    ALTER COLUMN print_status DROP DEFAULT,
    ALTER COLUMN print_status TYPE INTEGER
    USING (
        CASE print_status::text
            WHEN '0' THEN 0
            WHEN '1' THEN 1
            WHEN 'pending' THEN 0
            WHEN 'failed' THEN 0
            WHEN 'printed' THEN 1
            ELSE NULL
        END
    );

ALTER TABLE persons
    ADD CONSTRAINT persons_print_status_check
    CHECK (print_status IS NULL OR print_status IN (0, 1));

COMMENT ON COLUMN persons.print_status IS
    'NULL: no imprimir; 0: pendiente; 1: reclamado o impreso';

DROP INDEX IF EXISTS persons_unprinted_idx;

CREATE INDEX persons_unprinted_idx
    ON persons (print_claimed_at, created_at, id)
    WHERE print_status = 0;
