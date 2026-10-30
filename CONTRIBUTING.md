# Contributing

Thanks for taking the time. This is a small, local-first project; the bar is
"keeps working offline, keeps the tests green, never leaks data".

## Setup

```bash
uv venv --python 3.13
uv pip sync requirements.lock
cp .env.example .env
.venv/bin/python -m pytest
```

Ollama is only needed to run the app, not the unit suite. The suite is fully
offline; anything that would call Ollama, the Claude API or TypeSafe is
stubbed, and tests assert that the default backends never make a network call.

## Making changes

- Open an issue first for anything beyond a bug fix, so the approach can be
  agreed before code is written.
- Keep opt-in behaviour opt-in. New network-backed features go behind a
  setting that defaults off and fall back to the local result on any error.
- Add or update tests next to the code you touch. Endpoint tests use the
  FastAPI test client against a throwaway database (see
  `tests/test_review_endpoints.py` for the fixture pattern).
- Run `.venv/bin/python -m pytest` and `.venv/bin/python qa/run_qa.py --ocr mock`
  before opening a pull request. CI runs the same two.
- If you change dependencies: `uv lock`, then regenerate the pip-compatible
  lock with
  `uv export --format requirements-txt --no-hashes --no-emit-project -o requirements.lock`.
- Documentation lives in `docs/`; update the relevant page when behaviour or
  an endpoint changes. `docs/api.md` is the endpoint reference.

## Secrets and data

- Never commit anything from `data/`, `.env`, or an API key. A global
  [gitleaks](https://github.com/gitleaks/gitleaks) pre-commit hook is the
  recommended safety net:
  `git config --global core.hooksPath ~/.config/git/hooks` with a hook that
  runs `gitleaks protect --staged`.
- Test fixtures must be synthetic. Do not add real receipts, statements or
  names to the repository.

## Commit messages

Imperative subject line, a short body explaining why when it is not obvious.
Reference the issue number when there is one.
