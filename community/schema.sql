-- SPDX-License-Identifier: GPL-3.0-or-later
-- Simmer community (Cloudflare D1). Apply with: wrangler d1 execute simmer-community --remote --file schema.sql
CREATE TABLE IF NOT EXISTS recipes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  servings INTEGER DEFAULT 0,
  prep INTEGER DEFAULT 0,
  cook INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0,
  ingredients TEXT NOT NULL,
  steps TEXT NOT NULL,
  notes TEXT DEFAULT '',
  tags TEXT DEFAULT '[]',
  saves INTEGER DEFAULT 0,
  reports INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0,
  created INTEGER NOT NULL,
  device TEXT NOT NULL,
  delete_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recipes_new ON recipes (hidden, created DESC);
CREATE INDEX IF NOT EXISTS recipes_popular ON recipes (hidden, saves DESC);
CREATE TABLE IF NOT EXISTS saves (recipe TEXT NOT NULL, device TEXT NOT NULL, PRIMARY KEY (recipe, device));
CREATE TABLE IF NOT EXISTS reports (recipe TEXT NOT NULL, device TEXT NOT NULL, reason TEXT, at INTEGER,
  PRIMARY KEY (recipe, device));
CREATE TABLE IF NOT EXISTS limits (day TEXT NOT NULL, kind TEXT NOT NULL, who TEXT NOT NULL, n INTEGER DEFAULT 0,
  PRIMARY KEY (day, kind, who));
