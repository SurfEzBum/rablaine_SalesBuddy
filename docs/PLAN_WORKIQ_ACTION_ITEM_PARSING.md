# Plan: Harden the WorkIQ action-item parser

**Status:** Not started. Logged 8/6/2026 after a live parse failure.
**Owner file:** `app/services/copilot_actions.py`

---

## What happened

The daily Copilot action-item sync asked WorkIQ for a JSON array and got prose:

```
I found several candidate follow-ups across meetings and email threads. After
excluding the Purview documentation item and ignoring informational/news
emails, these appear to be the top 3 unresolved items that still have an
action associated with you or are awaiting your response/input:

1. MCIC (Fabric + AI Costing Session) - Send Data Agent Demo + Continue ...
```

`parse_action_items()` looks for `[` ... `]`, finds no array, logs, and returns
`[]`. `sync_copilot_action_items()` then bails with `"No action items parsed"`.

**Nothing is corrupted.** The existing items are only deleted *after* a
successful parse, so a failure is a clean no-op. This is a quality-of-result
bug, not a data bug.

The failure is already reported through
`queue_workiq_call('copilot_actions', 'parse_failed', ...)`, so telemetry can
say how often it happens before we spend effort on it.

## Root cause

WorkIQ is a model, not an API. The JSON instruction in `_build_prompt()` is a
request, not a contract, and it ignored it. Note the answer was *good* - it
found real items and even honoured the exclusion list. We threw away a correct
answer because of its shape.

## Options

### A. Tighten the prompt (cheap, unreliable)
Restate the JSON requirement at the end of the prompt, add a one-line example,
and forbid prose. Model-dependent and can silently regress.

### B. Fall back to a markdown-list parser (moderate, reliable)
When no JSON array is found, parse the numbered-list shape WorkIQ actually
returns:

- `^\s*\d+\.\s+(.*)$` for the title
- following indented lines / bullets until the next number for the description
- first URL in the block for `source_url`

Reuses the same `{title, description, source_url}` dict, so nothing downstream
changes.

### C. Second-pass reformat (expensive)
Feed the prose back to WorkIQ/the gateway with "convert this to JSON". Doubles
latency on an already 30-60s call and can fail the same way.

## Recommendation

**B, with A as a freebie.** B makes the sync resilient to the shape WorkIQ
actually produces, and A costs one string edit. Skip C.

Order:
1. Extract the JSON attempt into `_parse_json_items(text)`.
2. Add `_parse_markdown_items(text)` for the numbered-list shape.
3. `parse_action_items()` tries JSON, then markdown, then gives up.
4. Keep the telemetry call on total failure only, and add a distinct
   `failure_type='parse_fell_back_to_markdown'` so we can see how often the
   model ignores the JSON instruction.

## Tests

- JSON array parses (existing behaviour, no regression)
- JSON inside a ```json fence still parses
- The captured prose sample above yields 3 items with correct titles
- Prose with no numbered list still returns `[]` and logs
- A markdown item carrying a URL populates `source_url`

## Not doing

- Retrying the WorkIQ call on parse failure. It costs 30-60s and the daily sync
  is best-effort; the next run picks it up.
- Storing raw responses. They contain meeting and customer detail, and the
  telemetry rules forbid shipping business data.
