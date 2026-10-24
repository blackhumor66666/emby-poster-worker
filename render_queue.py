"""Durable single-item work queue; a shared lock serializes work with full scans."""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path


class RenderQueue:
    def __init__(self, path, run_lock, execute, changed=None):
        self.path = Path(path)
        self.run_lock = run_lock
        self.execute = execute
        self.changed = changed or (lambda: None)
        self.condition = threading.Condition(threading.RLock())
        self.jobs = []
        self.latest = {}
        self.revision = 0
        self.thread = None
        self.stopped = False
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        self.jobs = payload.get('jobs', [])
        self.latest = payload.get('latest', {})
        for job in self.jobs:
            previous = self.latest.get(job['item_id'])
            if not previous or job['updated_at'] >= previous['updated_at']:
                self.latest[job['item_id']] = copy.deepcopy(job)
        self.revision = int(payload.get('revision', 0))
        recovered = False
        for job in self.jobs:
            if job['status'] == 'running':
                newer = next((j for j in self.jobs if j['item_id'] == job['item_id'] and j['status'] == 'queued'), None)
                if newer:
                    if job.get('refresh') and newer['action'] == 'render':
                        newer['refresh'] = True
                    job.update(status='superseded', finished_at=time.time())
                else:
                    job.update(status='queued', recovered=True)
                recovered = True
        if recovered:
            self._save()

    def _save(self):
        self.revision += 1
        for job in self.jobs:
            previous = self.latest.get(job['item_id'])
            if not previous or job['updated_at'] >= previous['updated_at']:
                self.latest[job['item_id']] = copy.deepcopy(job)
        pending = [j for j in self.jobs if j['status'] in {'queued', 'running'}]
        history = [j for j in self.jobs if j['status'] not in {'queued', 'running'}][-100:]
        self.jobs = sorted(pending + history, key=lambda j: j['created_at'])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps({'revision': self.revision, 'jobs': self.jobs, 'latest': self.latest}, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)

    def enqueue(self, item_id, name='', action='render', refresh=False, context=None):
        if action not in {'render', 'restore'} or not item_id:
            raise ValueError('Invalid work item')
        with self.condition:
            job = next((j for j in self.jobs if j['item_id'] == str(item_id) and j['status'] == 'queued'), None)
            if job:
                job.update(name=name or job['name'], action=action,
                           refresh=(bool(job.get('refresh')) or refresh) if action == 'render' else False,
                           updated_at=time.time(), context=copy.deepcopy(context or {}))
            else:
                job = {'id': uuid.uuid4().hex, 'item_id': str(item_id), 'name': name or str(item_id),
                       'action': action, 'refresh': bool(refresh), 'status': 'queued',
                       'created_at': time.time(), 'updated_at': time.time(), 'context': copy.deepcopy(context or {})}
                self.jobs.append(job)
            self._save()
            self.condition.notify_all()
            return copy.deepcopy(job)

    def clear_finished(self):
        """Drop finished jobs; the latest-per-item map stays for UI status."""
        with self.condition:
            keep = [job for job in self.jobs if job['status'] in {'queued', 'running'}]
            removed = len(self.jobs) - len(keep)
            if removed:
                self.jobs = keep
                self.revision += 1
                self._save()
                self.condition.notify_all()
            return removed

    def retry(self, job_id):
        with self.condition:
            old = next((j for j in self.jobs if j['id'] == job_id and j['status'] == 'failed'), None)
            if not old:
                raise ValueError('任务不存在或无需重试')
            return self.enqueue(old['item_id'], old['name'], old['action'], old.get('refresh', False), old.get('context'))

    def latest_for(self, item_id):
        with self.condition:
            return copy.deepcopy(self.latest.get(str(item_id)))

    def latest_items(self):
        with self.condition:
            return copy.deepcopy(self.latest)

    def snapshot(self):
        with self.condition:
            jobs = copy.deepcopy(self.jobs)
            active = [j for j in jobs if j['status'] in {'queued', 'running'}]
            counts = {state: sum(j['status'] == state for j in jobs) for state in ('queued', 'running', 'succeeded', 'failed')}
            return {'revision': self.revision, 'counts': counts, 'active': active,
                    'recent': [j for j in reversed(jobs) if j['status'] not in {'queued', 'running'}][:20]}

    def start(self):
        with self.condition:
            if self.thread and self.thread.is_alive():
                return
            self.stopped = False
            self.thread = threading.Thread(target=self._worker, name='poster-work-queue', daemon=True)
            self.thread.start()

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=3)

    def _worker(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or any(j['status'] == 'queued' for j in self.jobs))
                if self.stopped:
                    return
            # Do not claim a job until a scan releases the shared lock. New edits
            # can still merge into a waiting job while the library is scanning.
            if not self.run_lock.acquire(timeout=1):
                continue
            try:
                with self.condition:
                    if self.stopped:
                        return
                    job = next((j for j in self.jobs if j['status'] == 'queued'), None)
                    if not job:
                        continue
                    job.update(status='running', started_at=time.time())
                    self._save()
                    work = copy.deepcopy(job)
                try:
                    result = self.execute(work)
                    status, error = 'succeeded', None
                except Exception as exc:
                    result, status, error = None, 'failed', str(exc)
                with self.condition:
                    job.update(status=status, result=result, error=error, finished_at=time.time())
                    self._save()
                self.changed()
            finally:
                self.run_lock.release()
