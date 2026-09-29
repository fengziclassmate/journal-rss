"""Small, atomic writers shared by RSS operations."""
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET


def write_changed(path, content):
    path = Path(path)
    raw = content.encode('utf-8') if isinstance(content, str) else content
    if path.exists() and path.read_bytes() == raw:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(raw)
        name = stream.name
    os.replace(name, path)
    return True


def write_json(path, value):
    return write_changed(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write_feed(path, root):
    # Build timestamps must not make identical article content look like an update.
    path = Path(path)
    ET.indent(root, space='  ')
    if path.exists():
        try:
            previous = ET.parse(path).getroot()
            old = previous.find('./channel/lastBuildDate')
            new = root.find('./channel/lastBuildDate')
            if old is not None and new is not None:
                timestamp = new.text
                new.text = old.text
                if ET.tostring(root) != ET.tostring(previous):
                    new.text = timestamp
        except ET.ParseError:
            pass
    return write_changed(path, ET.tostring(root, encoding='utf-8', xml_declaration=True))
