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
