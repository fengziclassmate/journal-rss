# Personal-library recommendations

## Current mode: no AI, email daily archive only

As requested on 2026-09-28, paid recommendations are retired, not awaiting automatic
resumption. `arxiv_email_daily.py` maintains `arxiv-email-daily.xml`: one item per
mail receipt day, with all parsed paper titles, authors, original abstracts and
links. No ranking, score threshold, daily paper cap, translation or model call.
This means the paper content included in the email, not the full PDF or private
mail headers/unsubscribe information. Stable daily GUIDs and the existing URL
are preserved. Same-day duplicate arXiv IDs are merged; different days keep their
own snapshots. All matching emails from 2026-09-01 onward are scanned, without
the previous last-ten-email limit. Legacy-only days are marked as unverified
cached records until their original mail is fetched.

The workflow no longer injects a DeepSeek key, prepares library embeddings or
runs the research-selection pipeline. The local `Journal RSS Library Sync` task
is disabled. Existing selected-paper feeds and historical AI archives are retained
but no longer regenerated. The keyword-filtered conference daily digest is also
retired; independent journal/conference feeds are unchanged.
The mechanisms below document the retired implementation and its safeguards.

The existing research and QQ-mail recommendations now use the entire personal
Zotero library. Feed libraries, groups, trash, notes and duplicate PDF attachments
are excluded. No collection needs to be created or maintained. Saved documents
without PDFs are included too. Standalone PDFs are included. This is not full-text
LLM reading: missing metadata abstracts use the first three PDF pages; excerpts
are explicitly distinguished from abstracts. Unreadable/scanned PDFs retain
their title and are counted in export diagnostics. Relative linked attachments
requiring a custom base directory currently fall back to metadata/title.

## Data flow

1. A local scheduled export reads a validated current SQLite snapshot, never
   writes Zotero, and extracts document title/abstract or PDF opening text.
2. Multilingual MiniLM embeddings are computed locally and cached by model/text.
   The full library participates, without a document-count cap or recency bias.
3. Metadata and vectors are encrypted using Fernet. Only `zotero-library.enc`
   is committed; the key is in GitHub Actions secret `ZOTERO_LIBRARY_KEY`.
   Local PDF and embedding caches remain outside the repository. Do not upload
   these caches, keys, notes or PDFs. Ciphertext size/change timing is public.
4. Each candidate retrieves its three closest library documents. Only limited
   matched title/abstract excerpts go to DeepSeek, never the full PDF/library.
   Prompts request a methodological explanation without revealing saved titles
   or private library membership. Generated explanations are public RSS content.
5. Research preselection blends keyword score and cosine similarity equally
   (50 points each) before its existing 30-candidate LLM limit. QQ-mail mode
   still analyzes all collected candidates, selecting up to 30 per email day.
6. Completed relevance and translation/summary caches are frozen by default.
   Failed LLM analyses cannot qualify on keyword score alone in enhanced mode.

Independent journal/conference feeds, original RSS URLs, receipt-day grouping,
read suppression and Zotero retention settings are unchanged. Conference daily
selection itself is not modified by this feature.

## Operations

Install `requirements-library-local.txt` locally. Run `zotero_library.py --help`.
The `--publish owner/repo` option updates only the encrypted file via GitHub's
contents API, retrying optimistic concurrency conflicts. Schedule once daily,
with no overlapping exports; the computer must be on for local changes to sync.
GitHub uses the last successfully exported library when the computer is off.
The initial encrypted file must be deployed before enabling scheduled publish.

Cloud execution fails explicitly if enabled context/key is unavailable rather
than silently reverting to non-personalized recommendations. Offline RSS rebuilds
do not require private context. Embedding model weights are public; do not place
decrypted library data in public Actions caches or Pages artifacts.

Existing historical digests are not rebuilt merely because the profile changes.
The change applies to candidates processed in subsequent successful runs.

## Paid-analysis safety (2026-09-28)

Paid analysis is OFF until a budget is approved. Both `api_safety.paid_enabled`
and repository variable `RSS_PAID_ANALYSIS_ALLOWED=true` are required. Ordinary
RSS collection/publishing can run with both disabled and makes no DeepSeek calls.

Completed scores are frozen regardless of library, prompt, model or metadata
changes. Existing library contents still inform each NEW analysis. Legacy title
translations/summaries are reused; historical unprocessed records are not silently
enrolled for paid backfill. QQ email enrollment starts on 2026-09-29. Historical
rescoring/backfill requires a separate explicitly reviewed operation.

The two recommendation streams share an encrypted journal on branch
`rss-analysis-ledger`, separate from `main`. A reservation is committed BEFORE
each API call; results, provider request ID, usage and finish reason are committed
afterward. Git publishing failures cannot erase this checkpoint. Missing journal
credentials, failed persistence or a concurrent-write conflict fail closed.
An interrupted reservation remains consumed; ambiguous calls are never silently
retried. Library plaintext/excerpts are not stored in the journal.

Provisional limits (inactive while paused): 10 requests per Beijing calendar day,
3 papers per request, and 100,000 reserved workload units (UTF-8 request bytes plus
maximum output tokens). This is NOT an exact token count or a yuan budget. Attempts
are limited to ONE per paper by default, shared across both streams and runs.
The API runs serially to make reservations and checkpoints unambiguous. Deferred
papers remain queued; reaching the quota does not delete them. Failed/ambiguous
papers require manual review before any further paid retry. High-volume email
days will create a backlog under these conservative limits.

Only real provider `usage` values are usage telemetry; pre-request reservations
remain charged against the local quota even if the provider outcome is unknown.
Never interpret missing usage as zero cost. The journal is encrypted and is not
copied into public Pages artifacts. There are no automatic DeepSeek test calls.
