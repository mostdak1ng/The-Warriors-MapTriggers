"""Small, best-effort atomic HUD status files; never gate game cleanup on I/O."""

import json
from pathlib import Path
import time


def read_status(path: Path, *, pending=False):
    # A failed rename leaves the newest complete payload in .tmp. Only the next
    # writer uses it to recover preferences; HUD readers see committed JSON.
    paths = (path.with_suffix(".tmp"), path) if pending else (path,)
    for candidate in paths:
        try:
            result = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(result, dict):
                return result
        except (OSError, ValueError):
            pass
    return {}


class StatusPublisher:
    def __init__(self, path: Path):
        self.path = path
        self.last_published = None
        self.failed = False

    def publish(self, payload):
        serialized = json.dumps(payload, sort_keys=True)
        if serialized == self.last_published and not self.failed:
            return True
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(serialized, encoding="utf-8")
            for attempt in range(3):
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.002)
        except OSError as error:
            if not self.failed:
                print(f"HUD status update deferred ({self.path.name}): {error}", flush=True)
            self.failed = True
            return False
        self.last_published = serialized
        if self.failed:
            print(f"HUD status updates resumed ({self.path.name})", flush=True)
        self.failed = False
        return True
