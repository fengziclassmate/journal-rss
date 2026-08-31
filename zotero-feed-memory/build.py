"""Package only add-on code; never include caches or user profiles."""
import json
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
version = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"]
output = root / "dist" / f"journal-rss-memory-{version}.xpi"
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
    for name in ("manifest.json", "bootstrap.js", "core.js", "addon.js"):
        package.write(root / name, name)
print(output)
