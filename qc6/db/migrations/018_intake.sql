-- One intake for every way URLs arrive (manual, sitemap, sheet, sitemap file, added later): each unique URL once,
-- with every source that gave it; rows that couldn't be used, with the reason.
CREATE TABLE IF NOT EXISTS list_urls (
    list_id   BIGINT NOT NULL REFERENCES url_lists(id) ON DELETE CASCADE,
    seq       INT NOT NULL,                      -- place in the crawl queue (1 = first)
    url       TEXT NOT NULL,                     -- the address fetched (first spelling seen)
    sources   TEXT[] NOT NULL,                   -- every source that listed it: "Sitemap: …", "Sheet: a.xlsx", "Manual entry"
    ref       TEXT NOT NULL DEFAULT '{}',        -- (JSON) where it was first seen: {"row": 12, "file": "a.xlsx", "cells": [...]}
    PRIMARY KEY (list_id, seq)
);
CREATE TABLE IF NOT EXISTS list_rejects (
    list_id   BIGINT NOT NULL REFERENCES url_lists(id) ON DELETE CASCADE,
    source    TEXT NOT NULL,
    ref       TEXT NOT NULL DEFAULT '{}',
    value     TEXT NOT NULL,
    reason    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS list_rejects_list ON list_rejects (list_id);
ALTER TABLE url_lists ADD COLUMN IF NOT EXISTS intake JSONB NOT NULL DEFAULT '{}';
