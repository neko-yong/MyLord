# Navigation + Arbitration Freeze Gate

Overall: FAIL — diagnostic checkpoint, not a verified hotfix.

## Baseline and scope

Baseline: `bef2e47d58a018c2ccf87458a5ff4a9fe38be2d1` (clean main and fetched origin/main).
Branch: `hotfix/navigation-arbitration-freeze`, independent worktree.
Runtime tested: Windows, Python 3.11, Streamlit 1.62.0, in-app Chromium browser.
No production database, real LLM, production schema, authentication persistence, merge, push, or deployment was used/changed.

The two reported production failures have **not** been reproduced. No navigation or arbitration execution fix is proposed. Earlier drafts that bypassed `.open` or moved LLM execution to background threads were removed: forced `0000` flags and a slow synchronous call did not establish a production root cause. The retained changes are diagnostic only.

## Observed browser runs (2026-08-31)

| Check | Unmodified baseline UI | Instrumented UI |
| --- | --- | --- |
| Real tab clicks: map → mediation → map → mediation → final → mediation | 20 cycles / 120 checks | 20 cycles / 120 checks |
| Blank body / missing selected content | 0 observed | 0 observed; exactly one case heading at each settled DOM check |
| Lost authentication / missing identity | 0 observed | 0 observed |
| Poll + other client message | Observed working | Observed working, including MAP_READY → MEDIATING |
| Pause / resume via actual buttons | Working | Working |
| Notification acknowledgement | Decline + accept notifications working | Accept notification working |
| Request / confirmation DB delay | Memory DB, no injected delay | 0.5 seconds each |
| Slow mock LLM | 3 calls × 3 seconds | 3 calls × 6 seconds |
| Duplicate confirmation while spinner active | Not tested | One additional real click; no additional generation observed |
| Result visible without reload / re-login | Both clients | Both clients |
| Backend done but permanently spinning | Not observed | Not observed |

Normal browser reload was tested **after** the recovery checks: it returned to the login page, as the existing session-only authentication contract requires. No raw participant token was persisted in URL, localStorage, cookie, or application auth state by this change.

The browser connection handle briefly expired during baseline testing. Rebinding the existing tabs restored tooling access without reloading either page; this was not an application blank-page reproduction.

## Concrete state trace

`tests/freeze_trace_sample.json` contains test-only sampled logs, not production evidence. Earlier navigation stdout was truncated; the sample is deliberately not presented as a complete 240-switch server trace. The browser check counts above come from real click/DOM loops.

The instrumented confirmation trace `269e7e8e` recorded:

| Time from dialog fragment start | Event |
| --- | --- |
| 1.15 ms | Confirmation button handled |
| 1.29 → 502.50 ms | Delayed confirmation / evidence freeze |
| 502.55 ms | ARBITRATING |
| 503.44 → 6503.96 ms | First mock LLM |
| 6504.23 → 12504.82 ms | Swapped mock LLM |
| 12505.16 → 18506.10 ms | Meta mock LLM |
| 18506.54 → 18506.98 ms | Final artifact persisted, CLOSED |
| 18507.32 ms | App rerun requested from dialog fragment |
| Subsequent DOM observation | Both clients show final result, no spinner |

Thus this observed spinner was waiting for the synchronous mock LLM, **not** stuck after database persistence. This does not establish the cause of the reported production spinner.

`case_status` describes the last canonical page snapshot; `arbitration_state` also tracks acknowledged transitions during a confirmation, so they intentionally differ until the next snapshot. `render_complete` is server-side emission, **not** proof of browser rendering; the browser DOM gate provides that separate check.

## Diagnostics and reproduction workflow

Instrumentation is off unless `RERUN_STATE_TRACE=true`. It logs immediate allowlisted events, enum states, boolean presence flags, selected-tab flags, and revision digests. It never accepts free-form payloads, exception text, tokens, prompts, message bodies, statements, evidence, secrets, or database URLs. No DB calls are added. The LLM/persistence event hooks do not access Streamlit session state or add Streamlit yield points inside the operation.

Run only the synthetic loopback fixture, from this worktree:

```powershell
$env:RERUN_STATE_TRACE='true'
$env:FREEZE_GATE_DB_DELAY='0.5'
$env:FREEZE_GATE_LLM_DELAY='6'
python -m streamlit run tests/freeze_browser_app.py --server.address 127.0.0.1 --server.port 8519 --server.headless true --server.fileWatcherType none --browser.gatherUsageStats false
```

Use a Python environment with this project's dependencies. The fixture overrides settings, the database factory, and LLM calls before loading the production UI; it uses synthetic in-memory data and never reads production secrets. It is a test entry point, not a deployment entry point. Restarting it creates a new isolated case.

The page displays its synthetic Case ID. In two browser tabs, log in with the fixture-only A/B credentials defined in `tests/freeze_browser_support.py`. These strings are public synthetic test credentials, not participant credentials. `?baseline=1` executes the recorded baseline `app.py`; without that query the fixture executes the worktree UI. The auxiliary Python modules remain from the worktree, which currently contain only no-op-when-disabled trace hooks.

`tests/freeze_browser_gate.mjs` supplies the real-click navigation helper for the browser-skill SDK. Run it in batches of five cycles and interleave actual B messages, A pause/resume, arbitration decline/notification acknowledgement, and passive polling. It never sets `case_tab` or patches `.open`. For arbitration: A requests; switch tabs while pending; B confirms; repeat the confirmation click while the slow mock is active; observe A's revision/notification; acknowledge it; verify both clients display the final result without reload.

## Performance and regression

The existing performance architecture is unchanged: one selected-view snapshot per ordinary app/tab rerun; one revision query per passive two-second poll; one DB call for a normal message with a current cache; no hidden-tab expensive queries. A stale message predecessor retains its pre-existing single incremental snapshot recovery. Counts were checked with the existing tests and sampled PERF logs; no real remote latency was measured.

Full suite command: `python -m unittest discover -s tests -q`.
Final result: **189 tests, 174 passed, 15 skipped**, in 39.879 seconds. No failures.
The suite covers chat/sync, pause/resume, dispute maps, arbitration state machine/freeze/checkpoints, notification contracts, admin route/layout/search/delete contracts, and secret redaction. New tests cover immediate/default-off trace emission, payload allowlists, fragment cleanup, visible snapshot failures with retained auth, and missing-case auth invalidation. `tests/__init__.py` prevents an installed third-party `tests` package from shadowing this suite.

Real PostgreSQL tests are skipped: no explicitly authorized `TEST_DATABASE_URL` was available. In-memory checks are not a substitute for PostgreSQL concurrency verification.

## Unresolved gate / next evidence required

Navigation root cause: unknown. Arbitration production root cause: unknown. Shared root cause: unproven.

Needed next: the affected deployment's exact Streamlit/browser versions and a sanitized failing browser console + matching server state trace from an authorized test/staging reproduction. Do not share tokens, case content, prompts, secrets, or a database URL. No production instrumentation deployment is authorized by this checkpoint.

The cross-client UI tests above do not prove all concurrent request interleavings or recovery from a real network/proxy/browser failure. The P0 reproduction/fix gate remains FAIL, and this branch is not safe to integrate as a completed hotfix.
