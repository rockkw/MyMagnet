---
id: TASK-8
title: Add search-term side panel on Library tab
status: Done
assignee: []
created_date: '2026-08-14 19:19'
updated_date: '2026-08-14 19:42'
labels:
  - software
  - ui
  - medium-priority
dependencies: []
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Opens a side panel with search-term navigation list vertically on the left, scrollable, when clicking a term or the search/spyglass icon
<!-- SECTION:DESCRIPTION:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Fixed via capped/scrollable term-bar (.term-bar.compact) instead of a full side panel — solves the actual overflow problem with 100s of terms without a bigger UI rewrite.
<!-- SECTION:NOTES:END -->
