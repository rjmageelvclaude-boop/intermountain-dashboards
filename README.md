# Hyperion Club — Live Contest Dashboard

Automated dashboard for Redwood's **Hyperion Club HVAC AOR Contest** (Jul 1 – Aug 31, 2026),
tracking the InterMountain group (Sierra, Ultimate, Brothers, Russett) against the national field.

## How it works

```
Enterprise Hub reports (Sales By Rep + Installed By Rep, whole network)
      │  emailed to rjmageelvclaude@gmail.com on a schedule
      ▼
build/parse_reports.py   ─►  site/hyperion-data.js   ─►  site/index.html
(aggregates per rep/company)   single source of truth      static dashboard, one URL
```

The dashboard is a **static site** — it reads one generated file (`site/hyperion-data.js`).
Nobody re-enters data. Refreshing the dashboard = re-running the parser on the latest reports.

## Regenerate the data (manual, today)

1. Export the two reports from Enterprise Hub (or use the emailed copies):
   - **Sales By Rep** → `data/sales_by_rep.xlsx`
   - **Installed Rev By Rep** → `data/installed_by_rep.xlsx`
2. Run:
   ```
   py build/parse_reports.py
   ```
   This rewrites `site/hyperion-data.js`.
3. Open `site/index.html` (or serve `site/` — see `.claude/launch.json`).

## The two reports (data contract)

| Metric | Source report | Group by | Value column |
|---|---|---|---|
| **Total Sales** (sold estimate) | Sales By Rep | `Assigned Technicians` | `Jobs Estimate Sales Subtotal` |
| **Total Installed** (completed rev) | Installed By Rep | `Sold By` | `Jobs Total` |

- **Installed is the contest measure**; Sales is the pipeline tracker.
- Both reports already span the **entire Redwood network (~22 teams / ~34 tenants)**, so the
  national leaderboard is automatic — no manual partner entry needed.
- Reports are filtered by **Job Completion Date**, so a rep's sold vs installed won't tie out
  exactly (installs completing this week were often sold earlier).
- Install rows with a blank `Sold By` are bucketed as company-level "Unattributed".

## Config

`build/config.json` — our 4 companies (+ colors), tenant→team display names, contest dates/goal.
Edit here to rename teams or adjust the goal.

## Views (site/index.html)

- **Overview** — stakes, group KPIs, weekly battles (vs matchup opponent), Big Hitters national
  points race, division standings + matchups, Hyperion Club top-10 race, scoring guide
- **Comfort Advisors** — national leaderboard, sortable by Total Installed or Total Sales,
  top-10 cut line, search, National / Our-Companies filter
- **Sierra / Ultimate / Brothers / Russett** — scoreboard hero vs opponent, performance bonus
  meter with projection, KPIs, CA roster table (installed + sales + pace)
- **Exceptions** — installed jobs with blank `Sold By`, filterable by team, CSV download for
  sending to partners to clean up attribution

Contest structure (divisions, week calendar, week-1 matchups, scoring) came from the
`Hyperion Contest.pptx` deck and lives in `build/config.json`. W/L/points auto-derive as
weeks close (all 0–0 during week 1). CA counts are estimated from reps active in the
reports — set `rosterOverrides` in config when official rosters are known.

## Roadmap

- [x] **Phase 1** — parser + dashboard on real data
- [x] **Phase 2** — full prototype design: Overview, CA national page, 4 company pages, Exceptions tab
- [ ] **Phase 3** — host `site/` on Azure Static Web App (one shareable URL)
- [ ] **Phase 4** — Gmail watcher (reports emailed hourly to rjmageelvclaude@gmail.com) →
      scheduled Azure Function runs parser → auto-publish. Hourly refresh, zero manual input.
- [ ] **Phase 5** — weekly W/L auto-close, SLC/Service/Call-Center role cards when those
      reports are added; `julyTotal` handling from Aug 1

## Notes

- RJ can schedule the reports to email **hourly** to rjmageelvclaude@gmail.com — the Phase 4
  watcher pulls the newest attachments and refreshes on that cadence.
- Built as plain HTML/CSS/JS (not the original "designer/DC" prototype format) so it can be
  hosted standalone on Azure without a proprietary runtime.
- Crisafulli Bros. is one tenant split into two teams (Albany / Glens Falls) by Business Unit;
  Best Care merges two tenants. Tony Y and redwoodservices are excluded (`nonContestTenants`).
