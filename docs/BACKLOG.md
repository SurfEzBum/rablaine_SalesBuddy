# Sales Buddy Backlog

Loose ideas and follow-ups that aren't urgent enough for a feature branch yet.
Newer items go on top.

---

## BLOCKED: seller mapping from MSX account teams (revisit ~Sept 2026)

Parked 8/20/2026. Work is preserved on branch
`feature/enrichment-systemuser-qualifier` (6 commits, unmerged). `main` keeps
the account **discovery** fix, which is unaffected and working.

**Why blocked:** MSX account-team data no longer identifies who sells a
territory. Verified against the real 210-account book:

| Territory | Truth (our data) | What MSX yields |
|---|---|---|
| MAA.0101 | Dan Kraft 44, Jarred O'Connor 9 | Dan (4) - correct |
| SOU.0207 | Rick Bowles 13, Brandi Hurner 6 | Rob Jenkins - not a current seller |
| SOU.0206.A | Tim O'Shea 32 | Curtis Wesbur / Vincent Jordan - wrong |
| HLA.0506.A | Tim O'Shea 40 | Tim, but on only 3 of 20 accounts |

Tim owns 40 accounts and appears on 3. Rick owns 13 and appears on **0**.
Jarred owns 9 and appears on **0**. The right answers simply aren't in the
source data right now, so no classifier can derive them - and a sync today
would overwrite good assignments with wrong ones.

This worked ~2 weeks before, so it's likely a temporary backend state
(alignment churn). **On revisit:** re-check whether Tim/Rick/Jarred have
`msp_accountteams` rows again; if so the original logic may just work.

Details, the field semantics we mapped out, and seven disproven approaches are
in repo memory (`seller-mapping-blocked.md`) so we don't re-derive them.

**Worth salvaging from the branch when we return:**
- SE classification (Data/Infra/Apps) - validated correct, independent of the
  seller problem.
- Territory seller override + per-customer "keep this seller" pin - the only
  reliable path to truth regardless of what MSX does.

**Separate bugs found along the way (not fixed):**
- `sellers_territories` is append-only in the sync, so stale sellers never
  leave a territory page. PODs get rebuilt each sync; sellers don't.
- Territory page shows a seller's *global* customer count, not their count in
  that territory.
- `get_entity_metadata()` throws on attributes with a null DisplayName.

---

## Morning aura sync rewrite

The morning catch-up aura sync uses a separate codepath from the manual
day-refresh button, which means we maintain two ways of doing the same
thing and keep finding bugs in only one of them (e.g. spinner persistence,
purge timing). Rewrite so:

- Each day in the aura window fetches in parallel instead of sequentially,
  to cut total sync time.
- Reuse the manual-sync UX: same per-day spinner state, same in-flight
  tracking, same completion handler. One codepath, one set of bugs to fix.

Touches: [meeting_sync.py](../app/services/meeting_sync.py),
[meeting_prefetch.py](../app/services/meeting_prefetch.py),
[index.html](../templates/index.html) (calendar JS).

---

## Redraw sync spinners on hard refresh

Manual day-refresh spinners survive month navigation now (5/1/2026 fix),
but a full page reload still drops them - the in-flight state is held in
JS module-level Sets that don't persist. If a refresh is mid-flight when
the user F5s, the spinner just disappears even though the backend job is
still running.

Options:
- Persist in-flight date set in `sessionStorage`, restore + re-poll on
  load.
- Server-side endpoint that lists currently-running day refreshes; calendar
  queries it on render and paints spinners accordingly.

Server-side is the cleaner answer (single source of truth, works across
tabs) but more code. SessionStorage is the cheap version.
