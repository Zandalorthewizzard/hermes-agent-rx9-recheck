# RX9 A1 B-1 canonical runner recheck

## Scope and verdict

- Scope: independent recheck of A1 B-1 only.
- Final Hermes candidate tested: `a2160b557674af3a4d0b4039485c9790c2d0b823`.
- **B-1 verdict: PASS.**
- The prior A1 CONCERN is cleared: the affected gateway shutdown suite now has a genuine `scripts/run_tests.sh` receipt with the required async tests green.
- The prior A1 report identifies B-1 as its sole unresolved B finding. On that stated scope, **full A1 may be considered PASS**. A2 remains a separate pending phase; this report does not authorize S6 or reopen other A1 gates.

## Runner-selection semantics checked

The exact candidate `scripts/run_tests.sh` was inspected. Its selection order is:

1. repository `.venv`, if it has an importable `pytest`;
2. repository `venv`, if it has an importable `pytest`;
3. `$HOME/.hermes/hermes-agent/venv`, if it has an importable `pytest`;
4. `HERMES_PYTHON`, if executable and it has an importable `pytest`.

The selected interpreter is then used to invoke the canonical `scripts/run_tests_parallel.py`; the runner preserves the documented `env -i` hermetic environment and forwards the test-runner knobs. This is not a direct `pytest` invocation.

For the recorded disposable HOME, the selected path resolved as follows:

```text
/tmp/rx9-test-home/.hermes/hermes-agent/venv -> /tmp/rx9-hermes-test-venv
pytest 9.1.1
pytest-asyncio 1.4.0
python-dotenv 1.2.2
```

This supplies both the async plugin that was missing in the old release venv and the Hermes runtime dependency that was missing in the first disposable venv.

## Canonical receipt independently rerun

Exact command, with the recorded disposable HOME and no runner substitution:

```bash
HOME=/tmp/rx9-test-home HERMES_TEST_WORKERS=4 HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugin_runtime_slots.py tests/gateway/test_plugin_runtime_shutdown.py tests/gateway/test_gateway_shutdown.py -q
```

Observed receipt:

```text
4 files, 108 tests passed, 0 failed (100% complete)
tests/hermes_cli/test_plugins.py: 82 passed
tests/hermes_cli/test_plugin_runtime_slots.py: 8 passed
tests/gateway/test_plugin_runtime_shutdown.py: 1 passed
tests/gateway/test_gateway_shutdown.py: 17 passed
```

The relevant affected file `tests/gateway/test_gateway_shutdown.py` therefore passed all 17 collected tests through the canonical runner, including the async tests that reproduced the prior environment-only failure.

## Integrity and disposition

- Candidate source, `gateway/run_shutdown.py`, the gateway test file, runner code, config, fixtures, and protected/live surfaces were not modified by this recheck.
- The worktree was clean before and after the runner execution; `git diff --check` was clean.
- Only this evidence report is authored by this recheck.
- `rx-status.md` was not modified.
