ALTER TABLE persons
    DROP CONSTRAINT IF EXISTS persons_pending_to_print_check;

ALTER TABLE persons
    ALTER COLUMN pending_to_print DROP NOT NULL,
    ALTER COLUMN pending_to_print DROP DEFAULT,
    ALTER COLUMN pending_to_print TYPE INTEGER
    USING (
        CASE pending_to_print::text
            WHEN '0' THEN 0
            WHEN '1' THEN 1
            ELSE NULL
        END
    );

ALTER TABLE persons
    ADD CONSTRAINT persons_pending_to_print_check
    CHECK (pending_to_print IS NULL OR pending_to_print IN (0, 1));

COMMENT ON COLUMN persons.pending_to_print IS
    'NULL: no imprimir; 0: pendiente; 1: reclamado o impreso';

DROP INDEX IF EXISTS persons_unprinted_idx;

CREATE INDEX persons_unprinted_idx
    ON persons (print_claimed_at, created_at, id)
    WHERE pending_to_print = 0;
