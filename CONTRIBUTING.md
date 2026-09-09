# Contributing

Read `AGENTS.md` and the protocol/model-profile documentation before changing
behavior. Preserve the upstream MIT license and keep protocol evidence separate
from hypotheses.

## Test environment

The pinned environment uses Python 3.14 and Home Assistant 2026.9.0. It includes
HA's optional Bluetooth/USB imports and the Home Assistant pytest fixtures.
Tests require no live server, Bluetooth adapter, purifier, or credentials.

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
env PYTHONPATH=. .venv/bin/ruff check .
env PYTHONPATH=. .venv/bin/python scripts/validate_model_profiles.py
env PYTHONPATH=. .venv/bin/pytest -q --cov=custom_components.govee_ble_air_purifier --cov-report=term-missing
.venv/bin/python -m compileall -q custom_components tests
git diff --check
```

Run all test modules. `.github/workflows/tests.yml` runs these checks on pushes
and pull requests. Existing HACS/Hassfest workflows are retained.

The preexisting `ClientStatus(str, Enum)` has a file-scoped Ruff `UP042`
exception so model support does not change that public type.

Direct dependencies are in `requirements-dev.in`; the resolved pins are in
`requirements-dev.txt`. Update them together and rerun the complete suite:

```bash
uv pip compile --generate-hashes --python .venv/bin/python requirements-dev.in -o requirements-dev.txt
uv pip sync --python .venv/bin/python requirements-dev.txt
```

See [model evidence](docs/h7123-h712c-evidence.md) for hardware observations and
H7123's unresolved disconnects, and [security review](docs/security-review.md)
for the dependency audit's limitations. Automated tests do not certify radio
reliability. Do not commit raw captures, device inventories, credentials, session
keys, or live deployment scripts.
