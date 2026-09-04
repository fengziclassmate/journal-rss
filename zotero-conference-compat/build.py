"""Package the conference-only parser compatibility fix."""
import json
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
version = json.loads((root / "manifest.json").read_text())["version"]
output = root / "dist" / f"conference-rss-parser-fix-{version}.xpi"
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
    for name in ("manifest.json", "bootstrap.js", "walker.js"):
        package.write(root / name, name)
print(output)
