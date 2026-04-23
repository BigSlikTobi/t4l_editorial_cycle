"""Ingestion pipeline.

Populates `public.raw_articles` + `public.article_entities` +
`public.article_topics` by chaining the three extraction cloud functions.
The editorial cycle reads from these tables; ingestion runs on its own
cron so the two phases are fully decoupled.
"""
