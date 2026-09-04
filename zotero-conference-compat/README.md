# Conference RSS Parser Fix

Zotero 9's SAXXMLReader recursively visits sibling XML nodes. Long flat RSS feeds
can fail with `InternalError: too much recursion` (shown as `Processing failed`
in the subscription UI), even when the XML is valid.

This add-on uses an iterative DOM traversal only for URLs starting with
`https://fengziclassmate.github.io/journal-rss/conference-feeds/`. All items and
existing GUIDs are retained. Other URLs use Zotero's original implementation.
It adds no timers, network requests, translation, cache, archive or item observers.
Conference subscription views also sort by publication date descending rather
than Zotero's default import-ID order. Other subscription views are unchanged.
Year-only and month-only dates in conference entries retain their precision
instead of being expanded by Zotero's FeedItem importer to January 1/the first day.
Disabling it restores the original parser and sorting. It skips installation of the parser patch
if a future Zotero version no longer has the recognized recursive walk method.

Build and test:

```text
node zotero-conference-compat/test-walker.cjs
python zotero-conference-compat/build.py
```

Install the generated XPI through Zotero's add-on manager. The patch is currently
targeted at Zotero 9.0.x. It does not change Zotero's installed application files.
The parser still loads the complete feed in memory; this fixes stack overflow,
not the storage and processing cost of importing tens of thousands of papers.

For previously imported dates, `repair_conference_dates_offline.py --apply`
requires Zotero to be closed, matches each GUID against the current public RSS,
backs up the database, and corrects only known year-to-January-1 expansions.
It verifies unchanged feed identities/read states and SQLite integrity before
committing. Omitting `--apply` makes it a dry run. The API-based
`repair_conference_date_precision.py` is available for smaller repairs while
the local debugger is enabled; it saves changes in small transactions.
