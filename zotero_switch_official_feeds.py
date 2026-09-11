"""Switch Zotero publisher feeds between direct and read-filtered mirror URLs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from zotero_subscribe_conferences import ZoteroDebugger


def migration_script(mirrors: list[dict], rollback: bool = False) -> str:
    return """(async () => {
const specs = SPECS;
if (Zotero.DataDirectory.dir.replaceAll('\\\\', '/').toLowerCase() !== 'f:/zotero') {
  throw new Error('Unexpected Zotero data directory');
}
const result = {updated: 0, already: 0, missing: [], errors: []};
const pauseToken = await Zotero.Feeds.pause();
try {
  for (const spec of specs) {
    try {
      const currentURL = spec.current_url;
      const targetURL = spec.target_url;
      let feed = Zotero.Feeds.getByURL(currentURL);
      if (!feed) {
        if (Zotero.Feeds.getByURL(targetURL)) result.already++;
        else result.missing.push(currentURL);
        continue;
      }
      feed.url = targetURL;
      feed.refreshInterval = 1440;
      feed.cleanupReadAfter = 1;
      feed.cleanupUnreadAfter = 999;
      await feed.saveTx({skipSelect: true});
      result.updated++;
    }
    catch (error) {
      result.errors.push({name: spec.name, error: String(error)});
    }
  }
}
finally {
  pauseToken.resume();
}
result.totalFeeds = Zotero.Feeds.getAll().length;
return JSON.stringify(result);
})()""".replace(
        "SPECS",
        json.dumps(
            [
                {
                    "name": item["name"],
                    "current_url": item["mirror_url"] if rollback else item["source_url"],
                    "target_url": item["source_url"] if rollback else item["mirror_url"],
                }
                for item in mirrors
            ],
            ensure_ascii=True,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("official-feed-config.json"))
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    mirrors = json.loads(args.config.read_text(encoding="utf-8"))["mirrors"]
    debugger = ZoteroDebugger()
    try:
        result = debugger.evaluate(migration_script(mirrors, args.rollback))
    finally:
        debugger.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["missing"] or result["errors"] or result["updated"] + result["already"] != len(mirrors):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
