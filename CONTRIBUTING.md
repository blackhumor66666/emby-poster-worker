# Contributing

Thanks for taking a look. This is a single-maintainer project, but issues and
pull requests are welcome.

## Before you start

- Keep runtime secrets and private deployment details out of commits: no `.env`,
  API keys, NAS paths, internal hostnames, or real library screenshots.
- The worker **replaces** Emby Primary images. Any change that touches rendering,
  signatures, or delivery must be tested with `DRY_RUN=true` first.

## Local check

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -t tests -v
python -m py_compile *.py
```

`tests/` covers routing/auth (`test_http.py`), rating accuracy, matching, the
render queue, cache GC and module boundaries. Please keep it green.

## Changing behaviour

1. `APP_VERSION` — bump for any behaviour or bug-fix change.
2. `RENDER_VERSION` — bump **only** when the artwork rules change. It is part of
   every item's signature, so a bump forces a full-library re-render on the next
   scan. Do not bump it for internal refactors.
3. If a new environment variable is read by the code, add it to `.env.example`
   with a short comment. An undocumented variable is a support ticket.
4. Describe rating-source and metadata assumptions in the README.

## Rendering changes

Rendering is compared byte-for-byte. Before/after a refactor, render the same item
twice and compare MD5 — an unintended pixel shift is otherwise invisible until
users notice their posters changed.
