# Personal-library recommendations

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
6. Library changes invalidate relevance, not valid translation/summary caches.
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
