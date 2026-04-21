ALTER TABLE editorial_state
    ADD COLUMN source_urls text[] NOT NULL DEFAULT '{}';
