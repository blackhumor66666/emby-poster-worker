"""Non-blocking persisted administrator views with single-flight refresh."""
import json
import threading
import time
from pathlib import Path


class Snapshot:
    def __init__(self, path, build, write, version, ttl=60):
        self.path, self.build, self.write = Path(path), build, write
        self.version, self.ttl = version, ttl
        self.lock = threading.Lock()
        self.data, self.updated, self.generation = None, 0, 0
        self.running, self.error = False, None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved.get('version') == version:
                self.data, self.updated = saved['data'], saved['updated']
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.updated = 0

    def read(self, force=False):
        with self.lock:
            if force:
                self.generation += 1
                self.updated = 0
            if not self.running and (self.data is None or time.time()-self.updated >= self.ttl):
                self.running = True
                threading.Thread(target=self._refresh, args=(self.generation,), daemon=True, name='admin-snapshot').start()
            return self.data, {'updated_at': self.updated or None, 'refreshing': self.running,
                               'stale': self.data is not None and time.time()-self.updated >= self.ttl,
                               'error': self.error}

    def _refresh(self, generation):
        try:
            data = self.build()
            updated = time.time()
            with self.lock:
                self.data = data
                self.updated = updated if generation == self.generation else 0
                self.error = None
                self.write(self.path, json.dumps({'version': self.version, 'updated': self.updated, 'data': data}, ensure_ascii=False))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            with self.lock:
                self.running = False
