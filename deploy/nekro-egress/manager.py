import concurrent.futures
import hmac
import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

SOCKET = "/tmp/verge/verge-mihomo.sock"
GROUP = "Nekro-Egress"
URL = "https://openrouter.ai/api/v1/models"
TOKEN = Path(os.environ["NEKRO_EGRESS_TOKEN_FILE"]).read_text().strip()
STATE = Path(os.environ["NEKRO_EGRESS_STATE_FILE"])
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def api(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    args = ["curl", "-fsS", "--max-time", "9", "--unix-socket", SOCKET, "-X", method]
    if payload is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]
    raw = subprocess.check_output(args + ["http://localhost" + path], stderr=subprocess.DEVNULL)
    return json.loads(raw or "{}")


class Manager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.version = 0
        self.cooldown: dict[str, float] = {}
        if STATE.exists():
            self.cooldown = json.loads(STATE.read_text()).get("cooldown", {})

    def save(self) -> None:
        temp = STATE.with_suffix(".tmp")
        temp.write_text(json.dumps({"cooldown": self.cooldown}))
        temp.replace(STATE)

    def candidates(self, proxies: dict) -> list[str]:
        names = [n for n, p in proxies.items() if p["type"] not in {"Selector", "URLTest", "Fallback", "LoadBalance"}
                 and any(s in n for s in (".JP ", ".SG ", "日本", "新加坡"))]
        return sorted(names, key=lambda n: ("AnyTLS" not in n, ".JP " not in n, n))

    def selected(self, proxies: dict) -> str:
        name = GROUP
        visited: set[str] = set()
        while name in proxies and name not in visited and proxies[name].get("now"):
            visited.add(name)
            name = proxies[name]["now"]
        return name

    def healthy(self, name: str) -> bool:
        try:
            result = api("/proxies/" + quote(name, safe="") + "/delay?timeout=5000&expected=200&url=" + quote(URL, safe=""))
            return result.get("delay", 0) > 0
        except (subprocess.SubprocessError, ValueError, OSError):
            return False

    def lease(self, failed: dict | None, tried: set[str]) -> dict:
        with self.lock:
            proxies = api("/proxies")["proxies"]
            names = self.candidates(proxies)
            current = self.selected(proxies)
            if failed:
                failed_node = failed.get("node", "")
                if failed_node in names:
                    self.cooldown[failed_node] = time.time() + 600
                    self.save()
            eligible = [n for n in names if n not in tried and self.cooldown.get(n, 0) <= time.time()]
            if current in eligible:
                eligible.remove(current)
                eligible.insert(0, current)
            # Probe concurrently, select in stable priority order after all probes finish.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = dict(zip(eligible, pool.map(self.healthy, eligible)))
            for name in eligible:
                if results[name]:
                    api("/proxies/" + quote(GROUP, safe=""), "PUT", {"name": name})
                    if name != current:
                        self.version += 1
                        logging.info("node switch %s -> %s", current, name)
                    self.save()
                    return {"node": name, "version": self.version, "proxy": f"http://172.20.0.1:{18000 + names.index(name)}", "candidates": names, "exhausted": False}
                self.cooldown[name] = time.time() + 600
            self.save()
            return {"node": "", "version": self.version, "candidates": names, "exhausted": True}

    def watch(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.lease(None, set())
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                logging.warning("health check unavailable")


manager = Manager()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/lease" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                self.send_error(400)
                return
            payload = json.loads(self.rfile.read(size))
            result = manager.lease(payload.get("failed"), set(payload.get("tried", [])))
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (subprocess.SubprocessError, ValueError, KeyError, TypeError, OSError):
            self.send_error(503, "Egress controller unavailable")

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=manager.watch, daemon=True).start()
    ThreadingHTTPServer(("172.20.0.1", 7900), Handler).serve_forever()
