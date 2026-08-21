# Sales Buddy Backlog

Loose ideas and follow-ups that aren't urgent enough for a feature branch yet.
Newer items go on top.

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
