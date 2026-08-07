# Plan: Unify how background syncs stream progress

**Status:** Not started. Logged 8/6/2026 while adding batch progress to the
revenue sync.
**Trigger:** Four separate implementations of "run work in parallel, stream its
progress to an SSE consumer" now exist, and one of them can hang.

---

## 1. Current state

Every long-running sync solves the same problem: work runs on threads, but the
HTTP layer needs a generator that yields SSE. A callback on a worker thread
cannot `yield`, so each site bridges the gap by hand - differently.

| Site | Bridge | Issue |
|---|---|---|
| `app/routes/msx.py` (account import) | poll `all(f.done())` behind `time.sleep(0.3)`, then `_drain(q)` | up to 300ms event lag; the drain-and-yield body is duplicated verbatim after each loop, so a fix in one copy silently misses the other |
| `app/services/marketing_sync.py` | blocking `q.get()`, counting `('done', ...)` sentinels | **can hang forever** (see §2) |
| `app/services/milestone_sync.py` | mixed: several plain generators plus one queue (~line 1990) | not yet audited closely |
| `app/services/revenue_sync.py` | `q.get(timeout=0.4)` + `future.done()`, result via `yield from` | current best of the four |

Not every `ThreadPoolExecutor` in the codebase needs this. `admin.py` and
`connect_export.py` run parallel work without streaming progress and are out of
scope.

## 2. The concrete bug (fix this regardless)

`marketing_sync._fetch_worker` wraps its per-customer work in `try/finally` with
**no `except`**. If `get_marketing_summary` raises anything that isn't a VPN
block:

1. the `finally` clears the retry callback and the exception propagates out,
2. the terminal `progress_q.put(('done', None, None, None))` never runs,
3. `pool.submit(...)` captures the exception in a future nobody inspects,
4. the consumer sits in `msg = progress_q.get()` forever.

Result: a wedged marketing sync and a hung SSE response, with the real error
swallowed. This is worth a standalone fix even if the rest of this plan never
happens - either wrap the worker body so the sentinel always fires, or move the
consumer onto `future.done()` like the revenue sync does.

## 3. What to extract - and what not to

**Extract the mechanism only.** The genuinely identical part is ~15 lines:

```python
def stream_progress(fn, *args, **kwargs):
    """Run fn(*args, report=..., **kwargs) on a worker thread.

    Yields whatever fn reports; returns whatever fn returns, so callers write
    `result = yield from stream_progress(...)`.
    """
```

Requirements it must satisfy:

- events surface at the speed of the work, not a polling tick
- a worker that raises **propagates** (via `future.result()`), never hangs
- one drain path, so there is no second copy to forget
- the caller's return value survives, so the call still reads like a call

**Leave policy at the call sites.** These differ for real reasons and
centralizing them would create a config-bag abstraction that fits nobody:

- percent ranges and phase weighting
- user-facing message text
- which events are fatal (e.g. `vpn_blocked`)

**Explicitly out of scope: the SSE wire format.** `msx.py` emits bare `data:`
lines; milestone, marketing, and revenue use named `event:` types. Each has a
matching front-end reader. Unifying that means rewriting every JS consumer for
no operational gain, and every one of those readers is on a critical path.

Also out of scope: `msx_retry_state.callback` is a module-global mutated per
worker. That is a separate thread-safety question and should not be tangled
into this change.

## 4. Sequencing

1. **Fix the `marketing_sync` hang.** Standalone, small, independently valuable.
2. **Read `milestone_sync.py` properly** - all six executor sites. It is the
   largest consumer; if the helper does not fit it, the shape is wrong. Do this
   before writing the helper, not after.
3. **Add `stream_progress` with tests** (see §5). No call-site changes yet.
4. **Migrate `marketing_sync`** first - smallest, and already proven broken.
5. **Migrate `msx.py`** one site at a time, verifying the account import after
   each. This is the riskiest step: it needs VPN and live MSX, so it cannot be
   fully covered by tests.
6. **Leave the plain generators alone.** `milestone_sync`'s
   `Generator[Tuple[int, int, str], ...]` functions yield directly with no queue
   and no thread. That is the cleanest pattern in the codebase; the queue is
   only needed when work is parallel *and* reports through a callback.

## 5. Tests

The helper is pure plumbing, so it tests cleanly with a fake worker - no MSX, no
VPN, no network:

- reported events arrive in order and one per report
- the worker's return value reaches the caller through `yield from`
- a worker that raises propagates the exception instead of hanging
- a worker that reports nothing still returns and terminates
- events reported after the last drain are not lost

`tests/test_revenue_sync.py::TestPullProgress` already covers the first three
against `_pull_with_progress` and can be lifted wholesale.

## 6. Risks

- **Blast radius.** The account import and milestone sync are the two paths
  users hit on day one. A regression there is worse than the duplication being
  fixed. One site per commit, and stop if anything smells.
- **Hard to test end to end.** These need corpnet and live MSX, so CI cannot
  catch a regression. Manual verification per migrated site is mandatory.
- **Over-abstraction.** If the helper starts growing keyword arguments to
  accommodate a fourth caller, that is the signal to stop and leave the
  remaining sites as they are. Three good call sites beat five awkward ones.

## 7. Not doing

- Rewriting the front-end SSE readers.
- A generic job framework. `app/services/job_queue.py` already exists for
  durable background work; this is only about streaming progress to a live
  request, which is a different problem.
- Touching `heartbeat` / `SyncStatus` semantics. Those are per-sync and fine.
