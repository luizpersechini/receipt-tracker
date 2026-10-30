## Summary

## Why

## Checklist

- [ ] `.venv/bin/python -m pytest` passes
- [ ] `.venv/bin/python qa/run_qa.py --ocr mock` passes (if the change touches the app flow)
- [ ] Default behaviour unchanged; new network calls are opt-in with a local fallback
- [ ] Docs updated (`docs/`, `.env.example`) where behaviour or endpoints changed
- [ ] No data from `data/`, no keys, no real receipts in the diff
