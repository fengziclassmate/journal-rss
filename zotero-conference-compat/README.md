# Conference RSS Parser Fix

Zotero 9 and Zotero 10 use a SAXXMLReader that recursively visits sibling XML nodes. Long flat RSS feeds
can fail with `InternalError: too much recursion` (shown as `Processing failed`
in the subscription UI), even when the XML is valid.

This add-on uses an iterative DOM traversal only for URLs starting with
`https://fengziclassmate.github.io/journal-rss/conference-feeds/` and the explicitly
listed large journal feeds `https://fengziclassmate.github.io/journal-rss/sustainability.xml`
and `https://fengziclassmate.github.io/journal-rss/journal-feeds/sustainability.xml`.
All items and existing GUIDs are retained. Other URLs use Zotero's original implementation.
It adds no timers, translation, archive or item observers.
Version 1.1 also checks the published `read-filter-status.json` receipt during
normal refreshes of managed Pages feeds. One small request is shared for 30 seconds.
An exact SHA-256 of the actual response and its GUID membership is checkpointed
only after successful native import. An unchanged feed skips XML downloading and
parsing, while still updating last-check status and running native expiry/unread
handling. Missing local entries, changed payloads, receipt failures and invalid
checkpoints use the complete native importer. Other hosts retain native downloading
and parsing. Checkpoints also record Zotero's last metadata-update timestamp.
Large managed native imports are serialized to avoid long-transaction contention.
An import-generation UUID in Zotero preferences rejects stale checkpoints even
if a read-only cache directory prevents deleting/replacing its previous file.
The disposable `rss-refresh-cache` directory in Zotero's data directory contains
hashes and GUIDs only, not abstracts, translations, reading history or PDF contents.
The first refresh builds the cache; updated feeds still import all their papers.
Conference subscription views also sort by publication date descending rather
than Zotero's default import-ID order. Other subscription views are unchanged.
Year-only and month-only dates in conference entries retain their precision
instead of being expanded by Zotero's FeedItem importer to January 1/the first day.
Disabling it restores the original parser, refresh and sorting. It skips installation of the parser patch
if a future Zotero version no longer has the recognized recursive walk method.

Build and test:

```text
node zotero-conference-compat/test-walker.cjs
node zotero-conference-compat/test-refresh.cjs
python zotero-conference-compat/build.py
```

Install the generated XPI through Zotero's add-on manager. The patch is currently
targeted at Zotero 9.0.x through 10.0.x. It does not change Zotero's installed application files.
The parser still loads the complete feed in memory; this fixes stack overflow,
not the storage and processing cost of importing tens of thousands of papers.

For previously imported dates, `repair_conference_dates_offline.py --apply`
requires Zotero to be closed, matches each GUID against the current public RSS,
backs up the database, and corrects only known year-to-January-1 expansions.
It verifies unchanged feed identities/read states and SQLite integrity before
committing. Omitting `--apply` makes it a dry run. The API-based
`repair_conference_date_precision.py` is available for smaller repairs while
the local debugger is enabled; it saves changes in small transactions.
