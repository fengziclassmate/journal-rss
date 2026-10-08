"""Reject known access/error documents without treating them as empty research data."""
import re
import json
import urllib.error
import urllib.parse

from bs4 import BeautifulSoup


class SourceResponseError(ValueError):
    pass


def safe_source_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))
    except ValueError:
        return '[invalid URL]'


def validate_response(raw, url=''):
    parts = urllib.parse.urlsplit(url)
    crossref_works = parts.hostname == 'api.crossref.org' and '/works' in parts.path
    expects_json = crossref_works or parts.path.endswith('.json') or urllib.parse.parse_qs(parts.query).get('format') == ['json']
    if raw and expects_json:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise SourceResponseError('Invalid JSON response, not source data: '+safe_source_url(url)) from error
        if crossref_works and (not isinstance(payload,dict) or payload.get('status') != 'ok'
                               or not isinstance(payload.get('message'),dict)
                               or not isinstance(payload['message'].get('items'),list)):
            raise SourceResponseError('Invalid Crossref work-list schema: '+safe_source_url(url))
    head = raw[:65536].decode('utf-8', errors='replace')
    document_start = re.sub(r'^\s*<\?xml[^>]*\?>', '', head.lstrip('\ufeff')).lstrip()
    if not re.match(r'<(?:!doctype\s+html|html|head)\b', document_start, re.I):
        return
    soup = BeautifulSoup(head, 'html.parser')
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if soup.find(id='anubis_challenge') or any(text in title for text in (
        "making sure you're not a bot", 'just a moment', 'attention required',
        'verify you are human', 'access denied', 'request rejected', 'service unavailable',
    )):
        raise SourceResponseError('Access verification/error HTML, not source data: '+safe_source_url(url))


def format_source_error(error, url=''):
    if isinstance(error, urllib.error.HTTPError):
        text = f'HTTP {error.code}: {error.reason}'
    else:
        text = type(error).__name__+': '+str(error)
    text = re.sub(r'https?://[^\s<>\"\']+', lambda m: safe_source_url(m.group()), text)
    if url:
        text += '; source='+safe_source_url(url)
    return ' '.join(text.split())[:600]
