# Market Radar Phase 2C Web Cockpit Design

**Status:** Approved design; written specification awaiting review

**Date:** 2026-07-30

**Base:** Market Radar Phase 2C read API at `f29c1d1e`

## 1. Purpose

Add the first user-facing Market Radar monitoring cockpit to the existing React Web application. The page turns the latest persisted A-share radar snapshot into a compact operational view for scanning market regime, data confidence, sector rank, supported ETFs, position caps, and invalidation evidence.

The Web layer is a read-only presenter. It never recomputes ranking, regime, ETF eligibility, confidence, or position policy.

## 2. Scope

### Included

- a lazy-loaded `/market-radar` application route;
- a permanent sidebar navigation entry with Chinese and English labels;
- typed client contracts for the existing `/api/v1/market-radar/latest`, `/sectors`, and `/sectors/{sector_id}` routes;
- one responsive cockpit page with overview, ranking, and selected-sector detail regions;
- manual refresh, loading, empty-bootstrap, legacy-snapshot, and API-error states;
- focused API, routing, navigation, and page interaction tests;
- Web documentation, flat changelog entry, and desktop/mobile PR screenshots.

### Excluded

- running a Market Radar scan from Web;
- automatic polling, scheduling, lifecycle transitions, signals, alerts, notifications, and reports;
- historical run selection or comparison;
- outcome evaluation and calibration;
- Hong Kong support;
- any backend, database, provider, score, or policy change.

## 3. Existing-System Fit

The page follows the established `AppPage` + `PageHeader` + common component structure. `App.tsx` lazy-loads it inside the existing authenticated `Shell`; `SidebarNav.tsx` adds one `Radar` icon entry near Decision Signals. Text is added to both maps in `i18n/uiText.ts` and accessed only through `useUiLanguage`.

The client module `api/marketRadar.ts` uses the shared Axios instance and `getParsedApiError` conventions. Domain types live in `types/marketRadar.ts`; no generated client or new state library is introduced. Page-local request and selection state is sufficient because the page is the only consumer in this slice.

## 4. Data Flow

On mount, the page requests `/market-radar/latest` and `/market-radar/sectors` in parallel. The latest response supplies regime and position policy; the sector response supplies authoritative persisted ranking order. Once both resolve:

1. if neither has a run, render the bootstrap empty state;
2. if run identities differ, render a refreshable consistency error instead of combining snapshots;
3. select the first ranked sector by default;
4. request `/sectors/{sector_id}` whenever the selected sector changes;
5. ignore a stale detail response if the user has already selected another sector;
6. manual refresh clears detail, reloads the two summary endpoints, and then reloads the current sector if it still exists.

The page does not poll. Later scheduling work may add server-driven freshness or polling without changing this page contract.

## 5. Information Architecture

The page uses three unframed responsive bands rather than nested cards.

### 5.1 Header And Overview

The page header contains the literal product name `Market Radar`, a short A-share scope subtitle, the snapshot time, and an icon refresh button with a tooltip.

A four-column overview grid collapses to two and then one column:

- market regime with deterministic localized label and tone;
- data quality plus regime coverage/confidence;
- total model-position range;
- supported sector count and snapshot freshness.

Missing legacy fields display an explicit unavailable label, never zero.

### 5.2 Sector Ranking

Desktop uses a dense accessible table with rank, sector, state, score, confidence, quality, and supported ETF. Rows are buttons with stable height and a visible selected state. Mobile uses one compact row layout with the same fields and no horizontal page overflow.

The ranking does not sort client-side. It preserves the API order and one-based rank. State, regime, quality, and ETF status labels are localized through explicit maps; raw enums remain available as secondary text where useful for diagnosis.

### 5.3 Selected-Sector Detail

The detail region shows:

- score, state, confidence, gross score, and risk deduction;
- six factor values in the server-provided breakdown, without renormalization;
- missing fields and risk reason codes;
- ETF alternatives with status, eligibility, score, confidence, and reason codes;
- matching position suggestion with sector/ETF caps and invalidation codes;
- run-level position-policy reason codes and correlation coverage.

No chart is added in this slice because the API exposes a current snapshot rather than a time series. Numerical values use compact bars and aligned labels only where they improve comparison.

## 6. State And Failure Handling

- **Initial loading:** use the existing page loading/state components without shifting the page header.
- **Bootstrap empty:** explain only that no persisted Market Radar snapshot is available and provide refresh; do not advertise unimplemented run controls.
- **Summary error:** use `ApiErrorAlert` and a retry command; do not render partial data from mismatched or failed summary calls.
- **Detail loading/error:** keep ranking usable and scope the error to the detail region.
- **Legacy snapshot:** show ranking while regime, position, ETF, and suggestion sections display unavailable states.
- **Auth failure:** remains owned by the existing global auth middleware and `AuthContext` redirect flow.

Raw API errors and unknown enum values fall back to stable neutral labels. The UI must not turn missing evidence into favorable or adverse market evidence.

## 7. Accessibility And Responsive Behavior

- every interactive ranking row is keyboard reachable and exposes selected state;
- icon-only refresh controls have tooltip and accessible labels;
- status is conveyed by text as well as color;
- the page has one `h1`, section headings in order, and loading/error announcements;
- fixed row, icon-button, score-bar, and overview dimensions prevent layout shifts;
- content supports 360px mobile width and wide desktop without overlap or clipped labels.

The page stays consistent with the existing restrained terminal/dashboard visual system. It does not add a marketing hero, decorative illustration, gradient background, or a new palette.

## 8. File Boundaries

- `apps/dsa-web/src/types/marketRadar.ts`: API/domain types only.
- `apps/dsa-web/src/api/marketRadar.ts`: three GET calls only.
- `apps/dsa-web/src/pages/MarketRadarPage.tsx`: request orchestration and page composition.
- `apps/dsa-web/src/components/market-radar/MarketRadarOverview.tsx`: overview metrics.
- `apps/dsa-web/src/components/market-radar/MarketRadarSectorList.tsx`: accessible ranking selection.
- `apps/dsa-web/src/components/market-radar/MarketRadarSectorDetail.tsx`: evidence and policy detail.
- `apps/dsa-web/src/App.tsx`, `SidebarNav.tsx`, and `uiText.ts`: route, navigation, and bilingual labels.

Components remain focused, but the design avoids a store, hook abstraction, or generic dashboard framework until a second consumer exists.

## 9. Testing And Visual Evidence

Test-first coverage includes:

1. API paths, identifier encoding, and typed response passthrough;
2. lazy route registration and authenticated shell rendering;
3. sidebar label and active navigation state in both languages;
4. overview formatting for complete and legacy snapshots;
5. persisted ranking order and keyboard/click selection;
6. selected detail aggregation, ETF alternatives, position caps, and reason codes;
7. bootstrap empty, summary error, detail error, retry, and stale-response protection;
8. responsive semantics that do not duplicate interactive rows in the DOM.

Verification runs focused Vitest tests, `npm run lint`, and `npm run build`. The existing full-suite baseline currently has two unrelated failures: `AlertRuleForm` expects absent JP/KR options and one `HomePage` history test times out. They are recorded rather than patched in this scope. GitHub `web-gate` is the merge authority for the final head.

After implementation, run the Vite development server and capture desktop and mobile screenshots of a deterministic mocked or seeded snapshot. Screenshots are attached to the PR description or comments and are not committed to the repository.

## 10. Documentation And Rollback

Update `docs/market-radar.md` with the Web route, information hierarchy, read-only behavior, and empty/error semantics. Update both documentation indexes only if their Phase 2C description does not already cover the cockpit. Add one flat `[Unreleased]` changelog entry. `README.md` remains unchanged.

Rollback reverts the Web route, navigation entry, client/types, cockpit components, tests, and documentation. The backend API and persisted data remain intact; no migration or configuration rollback is required.
