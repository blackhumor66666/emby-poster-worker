"""Test package.

Present so `python -m unittest discover -s tests -t .` can import the modules
instead of failing with "Start directory is not importable".

It also isolates the runtime directories. `app.py` defaults `CACHE_DIR` to
`/data/cache`, which does not exist (and cannot be created) on a CI runner or on
a developer machine without that mount. Importing this package before any test
module means every test sees a writable temporary directory instead.
"""
import os
import tempfile

os.environ.setdefault("CACHE_DIR", tempfile.mkdtemp(prefix="pw-test-cache-"))
os.environ.setdefault("ADMIN_DATA_DIR", tempfile.mkdtemp(prefix="pw-test-admin-"))
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8098")
