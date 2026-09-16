---
id: TASK-20
title: Add Docker deployment support and convert dashboard to React/Vite
status: To Do
assignee: []
created_date: '2026-09-16 15:36'
labels:
  - software
  - ui
  - deployment
dependencies: []
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Add Docker deployment support for the app (webserver.py backend + scrape/timer jobs), and convert the current dashboard — ~1300 lines of HTML/CSS/vanilla JS embedded as a Python string literal in webserver.py (Library, History, search/filter/sort, Open Top 3, Transmission integration) — into a standalone React/Vite frontend, with webserver.py trimmed down to a pure JSON API (/api/torrents, /api/searches, /api/stats, etc., already present and reusable as-is).
<!-- SECTION:DESCRIPTION:END -->
