# What's New in Sales Buddy

Recent updates and improvements, newest first.

Each entry is tagged with the short SHA of the **merge commit** that
brought the change into `main`, so the admin Updates card can show
*exactly* which entries are pending vs already on your machine.
Format: `## M/D/YYYY - <merge-short-sha>`. See
`scripts/tag-changelog.ps1` for the helper that fills this in.

## 7/6/2026

- Fixed WorkIQ-powered features (meeting attendee lookup, customer and partner contact scraping, daily meeting sync, and action-item suggestions) silently returning nothing. WorkIQ had started splitting some values across lines, which broke the response parsing. Sales Buddy now repairs those responses before reading them, so these features work again.

## 6/2/2026 - 852ce79

- Fixed a bug where Sales Buddy could lose its sign-in if you signed out of `az` in any other terminal, even though the app is supposed to keep its own separate session. Sales Buddy's session is now truly independent - signing in or out of `az` elsewhere no longer affects the app.

## 5/29/2026 - 9c53e89

- Fixed a bug where the server would silently lock up some time after auto-starting at login. The startup script was capturing the server's output through a pipe that nothing ever read, so once the OS pipe buffer filled, every waitress worker thread blocked on its next log write and the server stopped responding (manual `start.bat` launches were unaffected). The script now writes server output to a rotated log file at `%LOCALAPPDATA%\SalesBuddy\logs\server.log` (5 MB cap, one prior file kept), and no longer blocks on a "press any key" prompt when running headless under the scheduled task.

## 5/22/2026 - c72af75

- Updated auth system for better interoperability with operating-system-level `az login`. Sales Buddy now keeps its Azure CLI state in an isolated per-environment directory (`%USERPROFILE%\SalesBuddy\.azure` for production, `%USERPROFILE%\SalesBuddyDev\.azure` for development) so signing out of `az` in your normal terminal no longer logs the app out, and vice versa. Existing credentials are auto-migrated on first run - no re-authentication required.

## 5/7/2026 - 95517f9

- Improved the MSX Account Team endpoint probe. Probes now return in seconds instead of minutes when the endpoint is hung, classify timeouts as real failures (not VPN drops), and every real account-sync call to the same endpoint now contributes a free up/down signal to telemetry.

## 5/6/2026 - 3aee1d7

- Reworked the admin Updates card to be more resilient. The card now always shows the latest state when you open admin (no more "click Check Now to see what's actually there"), pulls the changelog directly from GitHub's API to avoid CDN staleness, and tracks the last deployed update in the database so "View last update" works across restarts, tabs, and browsers instead of only in the same tab that ran the update.

## 5/5/2026 - a0a20e4

- Added better WorkIQ tracking: every WorkIQ call now records whether it succeeded, the server was down, or the response failed to parse. This will give us a real picture of WorkIQ reliability over time.

## 5/5/2026 - 6506fe0

- Added background telemetry that probes the MSX Account Teams endpoint hourly so we can monitor when the recurring outage is happening.

## 5/5/2026 - d79313e

- "Check Now" in the admin Updates card now also refreshes the "View changelog" modal, so a forced re-fetch picks up brand-new entries in both places at once instead of just the card.

## 5/5/2026 - eb0c138

- Synapse Customers report polish: the Current tab now uses the most recent **complete** month for "Latest Month" and the 4-month average (so on May 5 it shows April's numbers, not May's partial data). The active tab is also remembered across visits via localStorage and applied before the page renders, so you no longer see the wrong tab flash on load.

## 5/5/2026 - b0e43c9

- Updated the MSX Account Teams API outage error message to ask users to send the error to Alex so he can engage the MSX team. The previous "try again in a few hours" wording implied the issue would self-resolve, which it won't.

## 5/5/2026 - 5bb2472

- Renamed the "New Synapse Customers" report to "Synapse Customers" with two tabs: **New** (customers who started using Azure Synapse Analytics in the last 6 months, same as before) and **Current** (every customer with any Synapse spend). The Current view replaces the "First Usage" column with an "Avg (Last 4mo)" column so you can spot drop-off, and customers within each seller group are sorted by latest month revenue descending. Old `/reports/new-synapse-users` URLs redirect to the new page.

## 5/1/2026 - 9c6efba

- Removed dead JS from the customer edit form that was throwing a non impacting error.

## 5/1/2026 - c5bb242

- Fixed admin "What just landed" sometimes coming up empty right after an update. The changelog used to be polled hourly in the background, which meant the first poll could grab a stale copy from GitHub's CDN seconds after a push and then sit on it for an hour. Now the changelog is lazy-loaded the first time you open the admin panel after boot, so what you see is always fresh.

## 5/1/2026 - 9902bb4

- Fixed ghost-aura retention so prefetched meetings stay around for 5 business days behind today instead of getting nuked the morning after the meeting. The home calendar will now actually show the trailing ghost aura it was always supposed to.
- Per-day calendar refresh spinners now persist while you navigate to other months and back, and the day auto-redraws with the new ghosts when the WorkIQ pull finishes (no more dead spinners or needing a hard refresh to see the result).
- Manual per-day refreshes no longer trigger the "purge expired ghosts" pass. That now only runs during the morning aura sync and the startup catchup, so clicking refresh on one day can't delete ghosts from other days.

## 4/30/2026 - 8f1ce35

- Fixed a bug in "What just landed" post update that caused it to sometimes not show the deployed changes.

## 4/30/2026 - 50807e7

- Changelog modal now shows the most recent 10 updates instead of the last 30 days, so a busy week doesn't flood it and a quiet stretch doesn't leave it empty.
- Activity and Action Items calendars on the home page now use a uniform cell height across every day (including weekends and out-of-month filler), so rows line up consistently no matter how full or empty a given day is.

## 4/30/2026 - 3ed0c48

- Improved WorkIQ meeting sync: fixed a timezone display bug and made the sync more resilient to flaky responses.

## 4/30/2026 - 8fe53c7

- Added a "View changelog" link in the admin Updates card header that opens a modal with the last 30 days of updates and a link to the full changelog on GitHub.

## 4/29/2026 - 19043e4

- Cache-bust the changelog fetch so the Updates card shows new entries right after a push instead of waiting for GitHub's CDN to expire.
- Restructure the changelog so each entry is tagged with the merge commit it covers, and the admin Updates card filters by commit hash instead of just date. This means multiple updates on the same day each get their own block, and you can see exactly which ones are pending vs already on your machine.

## 4/29/2026 - eb0ed08

- Rewrote milestone sync for a big performance boost (roughly 3-4x faster). Updated how we actually sync from MSX:
  - Opportunities: now scoped to the current Microsoft fiscal year through the next FY (a ~24-month window), plus any opp with no close date set. Previously we pulled all open opps regardless of close date but skipped recently-Won / Lost ones - now those come back too while their milestones still matter.
  - Stale milestone refresh: any local milestone that wasn't returned by the active sync (out-of-window, closed opp, etc.) is now refreshed in batches by milestone GUID directly, instead of round-tripping through the parent opportunity one at a time.
- Sync progress bar now reflects actual time spent per phase so it stops sitting at 82% for half the run.

## 4/28/2026 - 704abcc

- Added changelog viewer to admin Updates card so you can see what's new before and after applying an update.

## 4/28/2026 - 2cccf19

- Removed dead "Committed to bottom" toggle from U2C report (the toggle never did anything because committed and remaining milestones live in separate tables).

## 4/27/2026 - 65d2abb

- Fix Import Attendees getting stuck in "ready" mode after a failed scrape

## 4/27/2026 - 04c8d5e

- Stop milestone sync from re-marking completed milestones as stale

## 4/27/2026 - e1798fa

- Add DSS opportunity comment writeback option when creating notes.  Disabled by default, change in Settings.

## 2026-04-24

- Calendar columns now equal width with proper text truncation so long meeting titles don't break the layout

## 2026-04-23

- Add stale customer report broken down by territory and seller
- Show calendar sync icon when hovering a date so you can refresh just that day
- Improve meeting picker UX consistency and add ghost highlight styling
- Use the daily meeting cache for past-day meeting imports (faster, no live MSX call)
- Make scheduled task failures show up in the admin panel instead of silently failing
- Improve customer matching by trying the first token of the customer name
- Fix WorkIQ parser when meeting subjects contain pipe characters
- Fix a bug where ghost-aura sync was wiping calendar days

## 2026-04-22

- Add morning aura sync that prefetches the day's meetings on app start, with calendar dots showing which days have synced
- Add surgical per-day refresh so you can re-sync just one day from the calendar
- Note form now waits for ghost-aura before showing meeting picker so you don't see stale data
- Add WorkIQ failure telemetry to App Insights for diagnosing scrape issues
- Fix ANSI escape codes corrupting WorkIQ scrape output

## 2026-04-21

- Notes list now paginates so it loads fast even on customers with hundreds of notes
- Customer JSON backup now runs async, so saving a note no longer blocks for 20-60 seconds
- Fix SQLite lock contention during the async backup
- Auto-paste contact avatar after creating a new contact

## 2026-04-17

- Add offline page that explains what's happening when the network drops
- Daily 7AM meeting cache job (closes #120)
- MSI auto-launch and salesbuddy:// protocol handler

## 2026-04-16

- Batched milestone sync and improved opportunity sorting

## 2026-04-15

- WorkIQ status card is now draggable so you can move it out of the way

## 2026-04-14

- Use project title instead of topic name for general note calendar labels (cleaner display)
- Fix installer git detection so reinstalls work cleanly

## 2026-04-13

- Revenue import improvements (better matching, fewer false negatives)

## 2026-04-12

- Remove revenue engagements (replaced by direct revenue tracking)
- Revenue analyzer refinements
- Stale milestone improvements

## 2026-04-10

- Preserve milestone status during stale opportunity sync (no more accidental status drops)
- Fix Enter key in engagement contact dropdown
- Day-normalize MoM/CV calculations in revenue analysis for more accurate trend detection

## 2026-04-09

- Add Recently Viewed entities so you can jump back to where you were (closes #85)
- Fuzzy matching for customer domains
- Inline contact creation from forms (no more modal jump)
- New Action Items hub page
- SalesIQ MCP improvements: new tools, URL linking, milestone filters, system prompt hints

## 2026-04-08

- Action items now have due dates and a calendar tab (closes #116)
- Convert key individuals to engagement contacts so they get the full contact treatment (closes #114)
- Prefetch WorkIQ meetings on note page load for faster meeting picker (closes #117)
- Customer M&A handling: detect stale customers and provide a merge tool (closes #41)

## 2026-04-07

- Auto-add partner from attendees: partner contacts in a meeting auto-link their partner to the note
- Fix duplicate task creation when saving a note with an existing task linked
- Committed milestones no longer show overdue styling and countdown text
- Task improvements: modal chaining, workload filter, back-to-milestones button
- New Connect Impact report ranked by committed ACR/mo
- SalesIQ chat now renders markdown tables
- Split U2C% and Attainment% into separate cards for clarity
- Action item description now opens in a Quill rich-text flyout

## 2026-04-06

- Engagement AI fields, dynamic note form layout, related notes, engagement badges
- Inline editable engagement panels on note form (#113)
- Action item flyout description field with click-to-edit badges (closes #112)
- U2C snapshot report for quarterly milestone attainment tracking (closes #32)
- Add CSAM, DSS, and DAE stats to account sync summary
- Add Marketing Insights to Reports nav menu
- Marketing insights sync and report
- Table paste support in rich text fields

## 2026-04-05

- Fix PWA install card race condition that hid the install prompt
- Add Opportunities and Milestones to Browse nav menu
- MSX workspace report now has seller filter and calculated ACR

## 2026-04-04

- Native MSI installer v1.0.0 (resilient install/uninstall, idempotent)
- MCP server for VS Code Copilot integration

## 2026-04-03

- Switch AI to Azure Management JWT auth, removing the consent flow and ai_enabled toggle
- Add Internal Contacts (model, UI, MSX sync)
- Rename "call logs" to "notes" throughout the app
- Rename "Copilot" to "SalesIQ" throughout the app

## 2026-04-02

- SalesIQ Phase 4: tools registry, chat panel UI, chat endpoint with tool-calling
- Manual Milestone Sync button in admin panel (closes #105)
- Sign MSI with Azure Artifact Signing for trusted installs
- Fix attendee modal backdrop stacking on retry/cancel (closes #109)
- Partner scrape: append notes instead of replacing, detect WorkIQ server errors (closes #107)
- Detect SYSTEM-owned backup task in admin panel (closes #106)

## 2026-04-01

- Customer names in exports now link to TPID URL
- Remove Quick Actions panel from analytics (closes #93)
- Reports route cleanup with consistent header standardization (closes #94)
- AI partner recommendations for engagements
- Commitment status filter on milestone tracker (closes #90)
- Add help icon to navbar (closes #98)
- Configurable date range for What's New report
- Whitespace bucket drill-down (closes #92)
- Dismiss and "not useful" feedback on SalesIQ task suggestions (closes #96)
- Specialties selector keyboard behavior consistency (closes #102)
- Delay revenue import reminder to the 10th of the month
- Fix duplicate project dropdown and JS errors on general notes
- Fix territory-to-POD parsing for suffixed names (closes #104)
- Fix comment posting in milestone modal (closes #103)

## 2026-03-31

- Favorites for milestones, engagements, and opportunities (#86)
- Milestone tracker multiselect filters
- Milestone tracker "lost" hygiene status
- Workload report ACR deduplication
- Contact photo support
- Contact scraper for meeting attendees

## 2026-03-30

- New What's New report
- Make What's New collapsible
- Light mode visual improvements
- Milestone view shows all statuses
- Edit partner contacts inline
- Milestone audit trail (see who changed what when)
- Milestone team hint and on-team badge so you know which milestones are yours
- One-on-one report reorganized
- Fix PWA navigation and cancel-button referrer behavior
- Fix milestone dropdown overflow

## 2026-03-29

- Keyboard shortcuts throughout the app
- Branding update
- MSI installer fixes and OS theme detection
- Milestone calendar tabs
- Fix dirty working tree handling on reinstall
- Suppress git credential popups during update
- Fix NuGet Python PATH detection during install

## 2026-03-28

- New Whitespace report
- Milestone sync scheduler with MWF schedule and startup catchup
- Navbar customer search with `/` keyboard shortcut
- UX navigation overhaul
