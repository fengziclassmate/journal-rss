# RSS operations

## One-shot read cleanup

Run `clean-read-now.ps1` on the configured Zotero computer. The confirmation dialog
shows read/unread counts. It exports reading hashes, waits for a matching published
`read-filter-status.json`, restarts Zotero temporarily, backs up its current database,
and removes only still-read items through Zotero's native API in batches of 50.

Items read after the export, direct publisher feeds without a managed mirror, and
items whose identity or read state changed are skipped. Upload/publication failure
prevents deletion. Reports and backups are stored under `F:/Zotero/read-cleanup-*`.
The operation never changes retention settings or removes items from My Library.
Normal Zotero's own scheduled cleanup is not replaced by this on-demand tool.

## Health

Subscribe to `rss-health.xml`; `rss-health/index.html` shows per-source evidence.
Counts are recorded before personal read filtering and official-priority deduplication.
Unknown first-run increments are not labeled zero. Preserved/fallback/partial results
do not advance the last confirmed successful collection timestamp.
An empty result and a failed result are distinct. A >50% drop from a successful
collection of at least 20 entries is flagged, not silently treated as normal.

## Storage and publication

`research-data/arxiv-email-full-state.json` is a v2 manifest pointing to one JSON
file per day under the same stem directory. All papers and original abstracts remain.
Legacy v1 files are migrated on the next save. Unchanged shards and RSS content are
not rewritten. The full daily XML remains full-length: size warnings start at 50 MiB
and an early guard stops a commit at 99 MiB. This warns rather than dropping papers.

A push changing only read-suppression.json skips collection and performs filtering
and publishing only. Conference fallback is refreshed from current generated feeds
before personal filtering, so a filter-only deployment cannot restore stale outputs.

## Duplicate audit and daily reader

`duplicate-audit/index.html` searches the current official-priority audit. Strong
identities hide custom duplicates; title-only matches remain visible for review.
Previously hidden papers become available again when the next successful full
source collection includes them. Older suppression hashes remain stored, but title
hashes alone are no longer enough to hide an item.

Daily archive pages provide local search, categories, a table of contents and
collapsible abstracts. RSS descriptions retain the complete original paper list.
No recommendation model, paid API, private library export or new background
translation service is enabled.

## Sharing

Do not distribute your personalized filtered URLs as an unfiltered baseline:
others would inherit your exclusions. Share publisher-original URLs from
`official-feed-config.json` and the project method first. For an independent fork,
replace the repository/Pages base URLs, clear the *fork's* reading suppression,
generate a new local HMAC key and matching Actions secret, and keep each person's
reading history separate. Never copy mailbox credentials or local Zotero backups.
The existing public pages contain paper metadata, not mailbox authorization codes.
