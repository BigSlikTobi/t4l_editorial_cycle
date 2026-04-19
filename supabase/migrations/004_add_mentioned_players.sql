ALTER TABLE content.team_article
    ADD COLUMN mentioned_players text[] NOT NULL DEFAULT '{}';
