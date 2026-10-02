"""Publication precision and discovery metadata, independent of RSS transport dates."""
import datetime as dt
import html
import re
import xml.etree.ElementTree as ET

DC = 'http://purl.org/dc/elements/1.1/'
NS = 'https://fengziclassmate.github.io/journal-rss/ns/dates/1'
START = '<!-- journal-dates:start -->'
END = '<!-- journal-dates:end -->'


def partial_date(value):
    try:
        parts = value['date-parts'][0]
        if not 1 <= len(parts) <= 3:
            return ''
        year, month, day = (list(parts) + [1, 1])[:3]
        date = dt.date(int(year), int(month), int(day))
        return date.isoformat()[:{1:4, 2:7, 3:10}[len(parts)]]
    except (KeyError, IndexError, TypeError, ValueError):
        return ''


def crossref_dates(record):
    result = {}
    for field, name in [('published-online','online'), ('published-print','print'),
                        ('published','publisher'), ('created','registered'), ('deposited','metadata_updated')]:
        value = partial_date(record.get(field))
        if value:
            result[name] = value
    for field in ('online', 'print', 'publisher'):
        if field in result:
            result['publication'] = result[field]
            result['publication_source'] = field
            break
    return result


def describe(description, dates):
    description = re.sub(re.escape(START) + '.*?' + re.escape(END), '', description, flags=re.S).rstrip()
    publication = dates.get('publication', '')
    label = {4:'Publication year', 7:'Publication month', 10:'Publication date'}.get(len(publication),'Publication date')
    lines = [f'{label}: {publication}' if publication else 'Publication date: not available']
    if dates.get('publication_source'):
        lines[0] += ' (source: ' + dates['publication_source'] + ')'
    for key, name in [('online','Online publication'), ('print','Print/issue publication'),
                      ('registered','DOI registration (not publication)'),
                      ('metadata_updated','Metadata update (not publication)'),
                      ('issue_month','Estimated issue month (not verified publication)'),
                      ('first_seen','First observed by this collector (not publication)')]:
        if dates.get(key):
            lines.append(name + ': ' + dates[key])
    return description + START + ''.join('<p>' + html.escape(line) + '</p>' for line in lines) + END


def append_metadata(node, dates):
    ET.register_namespace('dc', DC)
    ET.register_namespace('jrdate', NS)
    if dates.get('publication'):
        ET.SubElement(node, '{' + DC + '}date').text = dates['publication']
    for key, value in dates.items():
        ET.SubElement(node, '{' + NS + '}' + key).text = value
