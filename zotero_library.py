"""Read-only personal-library export and encrypted semantic recommendation context."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import zlib
from pathlib import Path

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plain(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def read_library(database, data_dir, pdf_cache):
    """Attachments contribute to their parent; feeds, trash and notes are excluded."""
    connection = sqlite3.connect(f"{Path(database).as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT i.itemID, i.key, t.typeName,
                   MAX(CASE WHEN f.fieldName='title' THEN v.value END) title,
                   MAX(CASE WHEN f.fieldName='abstractNote' THEN v.value END) abstract,
                   MAX(CASE WHEN f.fieldName='DOI' THEN v.value END) doi
            FROM items i JOIN libraries l USING(libraryID)
            JOIN itemTypes t USING(itemTypeID)
            LEFT JOIN itemData d USING(itemID)
            LEFT JOIN fields f USING(fieldID)
            LEFT JOIN itemDataValues v USING(valueID)
            WHERE l.type='user'
              AND NOT EXISTS(SELECT 1 FROM deletedItems x WHERE x.itemID=i.itemID)
              AND NOT EXISTS(SELECT 1 FROM feedItems x WHERE x.itemID=i.itemID)
              AND t.typeName NOT IN ('note', 'annotation')
            GROUP BY i.itemID
        """).fetchall()
        attachments = connection.execute("""
            SELECT a.*, i.key FROM itemAttachments a JOIN items i USING(itemID)
            JOIN libraries l USING(libraryID)
            WHERE l.type='user' AND a.contentType='application/pdf'
              AND NOT EXISTS(SELECT 1 FROM deletedItems x WHERE x.itemID=i.itemID)
        """).fetchall()
    finally:
        connection.close()
    by_parent = {}
    for a in attachments:
        by_parent.setdefault(a['parentItemID'] or a['itemID'], []).append(a)
    papers, seen = [], set()
    stats = dict(pdf_extracted=0, pdf_failed=0, metadata_abstract=0, title_only=0)
    for row in rows:
        if row['typeName'] == 'attachment' and row['itemID'] not in by_parent:
            continue
        title, abstract = plain(row['title']), plain(row['abstract'])
        source = 'metadata' if abstract else 'title'
        if abstract:
            stats['metadata_abstract'] += 1
        else:
            for a in by_parent.get(row['itemID'], []):
                path = a['path'] or ''
                if path.startswith('storage:'):
                    pdf = Path(data_dir) / 'storage' / a['key'] / path[8:]
                elif path.startswith('attachments:'):
                    stats['pdf_failed'] += 1
                    continue
                else:
                    pdf = Path(path)
                if not pdf.is_file():
                    stats['pdf_failed'] += 1
                    continue
                stamp = digest([str(pdf), pdf.stat().st_size, pdf.stat().st_mtime_ns])
                text = pdf_cache.get(stamp)
                if text is None:
                    try:
                        import pymupdf
                        with pymupdf.open(pdf) as doc:
                            text = '\n'.join(doc[p].get_text() for p in range(min(3, len(doc))))
                        pdf_cache[stamp] = text
                    except Exception:
                        stats['pdf_failed'] += 1
                        continue
                match = re.search(r'(?is)\babstract\b[\s:.-]*(.*?)(?:\bkeywords\b|\bintroduction\b|$)', text)
                abstract = plain(match.group(1) if match else text)[:4000]
                if abstract:
                    source = 'pdf_abstract' if match else 'pdf_excerpt'
                    stats['pdf_extracted'] += 1
                    break
        if not title and not abstract:
            continue
        # Standalone PDFs often share generic names such as "Full Text".
        identity = (plain(row['doi']).lower() or
                    (row['key'] if row['typeName'] == 'attachment' else title.casefold()) or row['key'])
        if identity in seen:
            continue
        seen.add(identity)
        if not abstract:
            stats['title_only'] += 1
        papers.append(dict(key=row['key'], title=title, abstract=abstract,
                           evidence_source=source))
    return sorted(papers, key=lambda p: p['key']), stats


def encoder():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL, threads=2,
                         cache_dir=os.environ.get('ZOTERO_EMBEDDING_CACHE'))


def export(database, private_dir, output, key_file):
    from cryptography.fernet import Fernet
    from zotero_read_sync import create_database_snapshot
    private_dir.mkdir(parents=True, exist_ok=True)
    cache_path = private_dir / 'library-cache.json'
    cache = json.loads(cache_path.read_text('utf-8')) if cache_path.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = create_database_snapshot(database, Path(tmp))
        papers, stats = read_library(snapshot, database.parent, cache.setdefault('pdf', {}))
    if not papers:
        raise RuntimeError('No personal-library documents; refusing to replace existing profile')
    vectors = cache.setdefault('vectors', {})
    texts = [p['title'] + '\n' + p['abstract'][:1800] for p in papers]
    hashes = [digest([MODEL, text]) for text in texts]
    missing = dict((h, text) for h, text in zip(hashes, texts) if h not in vectors)
    if missing:
        for h, vector in zip(missing, encoder().embed(list(missing.values()), batch_size=32)):
            vectors[h] = [round(float(v), 6) for v in vector]
    profile = dict(model=MODEL, papers=papers, vectors=[vectors[h] for h in hashes])
    profile['version'] = digest([MODEL, papers])
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(Fernet.generate_key())
    cipher = Fernet(key_file.read_bytes().strip())
    unchanged = False
    if output.exists():
        raw = cipher.decrypt(output.read_bytes())
        compressed = not raw.startswith(b'{')
        old = json.loads(zlib.decompress(raw) if compressed else raw)
        unchanged = compressed and old.get('version') == profile['version']
    if not unchanged:
        temporary = output.with_suffix('.tmp')
        temporary.write_bytes(cipher.encrypt(zlib.compress(
            json.dumps(profile, ensure_ascii=False, separators=(',', ':')).encode(), level=9)))
        temporary.replace(output)
    temp_cache = cache_path.with_suffix('.tmp')
    temp_cache.write_text(json.dumps(cache, ensure_ascii=False), 'utf-8')
    temp_cache.replace(cache_path)
    print(json.dumps(dict(papers=len(papers), changed=not unchanged, **stats)))


class LibraryMatcher:
    def __init__(self, profile, embedding=None):
        import numpy as np
        if profile['model'] != MODEL:
            raise ValueError('Library embedding model mismatch')
        self.profile = profile
        self.version = profile['version']
        self.embedding = embedding
        self.matrix = np.asarray(profile['vectors'], dtype=np.float32)
        self.matrix /= np.maximum(np.linalg.norm(self.matrix, axis=1, keepdims=True), 1e-12)

    def match(self, records):
        import numpy as np
        if self.embedding is None:
            self.embedding = encoder()
        texts = [r['title'] + '\n' + r.get('abstract', '')[:1800] for r in records]
        for record, vector in zip(records, self.embedding.embed(texts, batch_size=32)):
            vector = np.asarray(vector)
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            scores = self.matrix @ vector
            indices = np.argsort(-scores)[:3]
            record['_library_context'] = [dict(
                title=self.profile['papers'][int(i)]['title'],
                abstract=self.profile['papers'][int(i)]['abstract'][:900],
                evidence_source=self.profile['papers'][int(i)]['evidence_source'],
            ) for i in indices]
            record['library_similarity'] = round(float(scores[indices[0]]), 4)


def prepare(config):
    settings = config.get('zotero_library', {})
    if not settings.get('enabled'):
        return
    key = os.environ.get('ZOTERO_LIBRARY_KEY', '').strip()
    if not key:
        raise RuntimeError('ZOTERO_LIBRARY_KEY is required for enabled library recommendations')
    from cryptography.fernet import Fernet
    raw = Fernet(key.encode()).decrypt(Path(settings['encrypted_path']).read_bytes())
    profile = json.loads(zlib.decompress(raw))
    config['_library_matcher'] = LibraryMatcher(profile)
    config['_library_version'] = profile['version']
    print(f"[info] personal-library documents={len(profile['papers'])}")


def publish(output, repository):
    """Only encrypted bytes leave the machine; do not touch the working tree."""
    payload = output.read_bytes()
    expected_sha = hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()
    endpoint = f'repos/{repository}/contents/zotero-library.enc'
    for attempt in range(3):
        current = subprocess.run(['gh', 'api', endpoint, '--jq', '.sha'],
                                 capture_output=True, text=True)
        if current.returncode:
            raise RuntimeError('Cannot read remote profile metadata; retry on next scheduled run')
        sha = current.stdout.strip()
        if sha == expected_sha:
            print('[info] remote library profile unchanged')
            return
        body = dict(message='Update encrypted personal-library profile', sha=sha,
                    content=base64.b64encode(payload).decode(), branch='main')
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / 'request.json'
            request.write_text(json.dumps(body), 'utf-8')
            result = subprocess.run(['gh', 'api', endpoint, '--method', 'PUT',
                                     '--input', str(request), '--jq', '.commit.sha'],
                                    capture_output=True, text=True)
        if result.returncode == 0:
            print('[info] encrypted library profile published')
            return
    raise RuntimeError('Failed to publish encrypted library profile after 3 attempts')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--private-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--publish', help='GitHub owner/repository; encrypted file must already exist')
    args = parser.parse_args()
    export(args.database, args.private_dir, args.output, args.key_file)
    if args.publish:
        publish(args.output, args.publish)
