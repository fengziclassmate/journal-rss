"""Register the generated conference feeds in a running Zotero instance.

Start Zotero with its debugger server before running this script:
    zotero.exe --start-debugger-server 6011
"""

from __future__ import annotations

import json
import socket
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "conference-config.json"
HOST = "127.0.0.1"
PORT = 6011

class ZoteroDebugger:
    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self.sock = socket.create_connection((host, port), timeout=30)
        self.stream = self.sock.makefile("rb")
        self._receive()

        process = self._command("root", "getProcess", id=0)
        descriptor = process["processDescriptor"]
        self.target = self._command(descriptor["actor"], "getTarget")["process"]

    def close(self) -> None:
        self.stream.close()
        self.sock.close()

    def _receive(self) -> dict:
        length = bytearray()
        while (char := self.stream.read(1)) != b":":
            if not char:
                raise EOFError("Zotero debugger connection closed")
            length.extend(char)
        return json.loads(self.stream.read(int(length)))

    def _command(self, actor: str, command_type: str, **kwargs) -> dict:
        payload = json.dumps(
            {"to": actor, "type": command_type, **kwargs}, ensure_ascii=True
        ).encode()
        self.sock.sendall(str(len(payload)).encode() + b":" + payload)
        while True:
            result = self._receive()
            if result.get("from") == actor:
                return result

    def evaluate(self, script: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="zotero-conference-") as temp:
            report = Path(temp) / "result.json"
            wrapper = """(async () => {
try {
  const value = await (SCRIPT);
  await IOUtils.writeJSON(REPORT, {ok: true, value: JSON.parse(value)});
} catch (error) {
  await IOUtils.writeJSON(REPORT, {ok: false, error: String(error), stack: error.stack});
}
})()""".replace("SCRIPT", script).replace("REPORT", json.dumps(str(report)))
            self._command(self.target["consoleActor"], "evaluateJSAsync", text=wrapper)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if report.exists():
                    result = json.loads(report.read_text(encoding="utf-8"))
                    if not result["ok"]:
                        raise RuntimeError(result)
                    return result["value"]
                time.sleep(0.25)
            raise TimeoutError("Zotero did not finish; inspect subscriptions before retrying")


def feed_specs() -> list[dict[str, str]]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base_url = config["base_url"].rstrip("/")
    feeds = [
        {
            "name": f"TopConf {conference['id']:02d} | {conference['acronym']}",
            "url": f"{base_url}/{conference['slug']}.xml",
        }
        for conference in config["conferences"]
    ]
    feeds.append(
        {
            "name": "TopConf Daily | \u9876\u4f1a\u6bcf\u65e5\u901f\u9012",
            "url": f"{base_url}/top-conference-daily.xml",
        }
    )
    return feeds


def installation_script(feeds: list[dict[str, str]]) -> str:
    return """(async () => {
const specs = SPECS;
if (Zotero.DataDirectory.dir.replaceAll('\\\\', '/').toLowerCase() !== 'f:/zotero') {
  throw new Error('Unexpected Zotero data directory; refusing to modify another library');
}
const result = {created: [], updated: [], errors: []};
const pauseToken = await Zotero.Feeds.pause();
try {
  for (const [index, spec] of specs.entries()) {
    try {
      let feed = Zotero.Feeds.getByURL(spec.url);
      if (feed) {
        feed.name = spec.name;
        feed.refreshInterval = 1440;
        await feed.saveTx({skipSelect: true});
        result.updated.push({id: feed.libraryID, name: feed.name, url: feed.url});
      }
      else {
        feed = new Zotero.Feed({
          name: spec.name,
          url: spec.url,
          refreshInterval: 1440
        });
        // Stagger initial imports over 18 hours instead of loading 100k entries at once.
        const nextCheck = new Date(Date.now() + (30 + index * 30 - 1440) * 60000);
        feed._set('_feedLastCheck', Zotero.Date.dateToSQL(nextCheck, true));
        await feed.saveTx({skipSelect: true});
        result.created.push({id: feed.libraryID, name: feed.name, url: feed.url});
      }
    }
    catch (error) {
      result.errors.push({operation: 'save', url: spec.url, error: String(error)});
    }
  }
}
finally {
  pauseToken.resume();
}
result.total = Zotero.Feeds.getAll().length;
return JSON.stringify(result);
})()""".replace("SPECS", json.dumps(feeds, ensure_ascii=False))


def main() -> None:
    debugger = ZoteroDebugger()
    try:
        result = debugger.evaluate(installation_script(feed_specs()))
    finally:
        debugger.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)
    if len(result["created"]) + len(result["updated"]) != 36:
        raise SystemExit("Expected 36 conference subscriptions to be registered")


if __name__ == "__main__":
    main()
