# Market Radar Phase 2C Web Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a responsive, bilingual, read-only Market Radar cockpit over the existing Phase 2C API.

**Architecture:** A typed Axios client converts snake_case API payloads to camelCase domain contracts. One page owns summary/detail request state and composes three focused presentation components; the existing authenticated Shell owns routing, navigation, theme, and language.

**Tech Stack:** React 19, TypeScript 5.9, React Router 7, Axios, Tailwind CSS 4, Vitest, Testing Library, Lucide icons

## Global Constraints

- The Web layer must not recompute rank, regime, ETF eligibility, confidence, or position policy.
- The page is read-only and A-share-only; no run, schedule, alert, or mutation control is added.
- Preserve API ordering and represent missing evidence as unavailable, never zero.
- Reuse `AppPage`, `PageHeader`, `ApiErrorAlert`, `EmptyState`, `Badge`, shared Axios, Shell, theme, and UI language context.
- Add no dependency, state library, backend change, database change, configuration, or environment variable.
- Support 360px mobile width and wide desktop with no duplicate interactive rows or horizontal page overflow.
- Update both Chinese and English UI text and attach desktop/mobile screenshots to the PR, not the repository.
- Existing unrelated baseline failures in `AlertRuleForm.test.tsx` and `HomePage.test.tsx` are recorded, not patched.

---

### Task 1: Typed Market Radar Client

**Files:**
- Create: `apps/dsa-web/src/types/marketRadar.ts`
- Create: `apps/dsa-web/src/api/marketRadar.ts`
- Create: `apps/dsa-web/src/api/__tests__/marketRadar.test.ts`

**Interfaces:**
- Consumes: shared `apiClient.get<Record<string, unknown>>()` and `toCamelCase<T>()`
- Produces: `marketRadarApi.getLatest()`, `marketRadarApi.listSectors()`, `marketRadarApi.getSector(sectorId)`
- Produces: complete camelCase types for snapshot, sector score, ETF selection, regime, and position plan

- [ ] **Step 1: Write failing API tests**

```ts
it('loads and camel-cases the latest snapshot', async () => {
  get.mockResolvedValueOnce({ data: {
    available: true,
    run: { run_key: 'cn:1', as_of: '2026-07-30T07:00:00Z', position_plan: null },
  } });
  const result = await marketRadarApi.getLatest();
  expect(get).toHaveBeenCalledWith('/api/v1/market-radar/latest');
  expect(result.run?.runKey).toBe('cn:1');
});

it('encodes canonical sector ids in the detail path', async () => {
  get.mockResolvedValueOnce({ data: { run_key: 'cn:1', as_of: '2026-07-30T07:00:00Z' } });
  await marketRadarApi.getSector('industry:semiconductor/value');
  expect(get).toHaveBeenCalledWith(
    '/api/v1/market-radar/sectors/industry%3Asemiconductor%2Fvalue',
  );
});
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `npm.cmd run test -- src/api/__tests__/marketRadar.test.ts`

Expected: FAIL because `../marketRadar` and the Market Radar types do not exist.

- [ ] **Step 3: Add complete domain contracts**

Define literal unions and nested interfaces matching the API after camel-casing:

```ts
export type MarketRadarQuality = 'complete' | 'partial' | 'stale' | 'unavailable';
export type MarketRadarSectorState =
  | 'leading' | 'improving' | 'neutral' | 'weakening' | 'avoid' | 'insufficient_data';
export type MarketRadarRegime =
  | 'risk_on' | 'selective' | 'defensive' | 'risk_off' | 'insufficient_data';

export interface MarketRadarSectorScore {
  sectorId: string;
  name: string;
  kind: 'industry' | 'concept';
  scoringVersion: 'cn-v1';
  grossScore: number;
  riskDeduction: number;
  score: number;
  confidence: number;
  state: MarketRadarSectorState;
  factors: Record<string, unknown>;
  riskReasons: string[];
  missingFields: string[];
  source: string;
  observedAt: string;
  quality: MarketRadarQuality;
  observation: Record<string, unknown>;
}

export interface MarketRadarLatestResponse {
  available: boolean;
  run: MarketRadarSnapshot | null;
}

export interface MarketRadarSectorListResponse {
  available: boolean;
  runKey: string | null;
  asOf: string | null;
  items: Array<{ rank: number; sector: MarketRadarSectorScore }>;
  total: number;
}
```

Add exact interfaces for ETF observation/selection, component scores, regime components/assessment, position suggestion/group/plan, complete snapshot, and sector detail. Nullable fields must remain nullable and extensible evidence maps use `Record<string, unknown>`.

- [ ] **Step 4: Implement the three GET calls**

```ts
export const marketRadarApi = {
  async getLatest(): Promise<MarketRadarLatestResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/market-radar/latest');
    return toCamelCase<MarketRadarLatestResponse>(response.data);
  },
  async listSectors(): Promise<MarketRadarSectorListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/market-radar/sectors');
    return toCamelCase<MarketRadarSectorListResponse>(response.data);
  },
  async getSector(sectorId: string): Promise<MarketRadarSectorDetailResponse> {
    const path = `/api/v1/market-radar/sectors/${encodeURIComponent(sectorId)}`;
    const response = await apiClient.get<Record<string, unknown>>(path);
    return toCamelCase<MarketRadarSectorDetailResponse>(response.data);
  },
};
```

- [ ] **Step 5: Verify GREEN and commit**

Run: `npm.cmd run test -- src/api/__tests__/marketRadar.test.ts`

Expected: all Market Radar API tests pass.

```bash
git add apps/dsa-web/src/types/marketRadar.ts apps/dsa-web/src/api/marketRadar.ts apps/dsa-web/src/api/__tests__/marketRadar.test.ts
git commit -m "feat(web): add Market Radar API client"
```

### Task 2: Cockpit Components And Page State

**Files:**
- Create: `apps/dsa-web/src/components/market-radar/MarketRadarOverview.tsx`
- Create: `apps/dsa-web/src/components/market-radar/MarketRadarSectorList.tsx`
- Create: `apps/dsa-web/src/components/market-radar/MarketRadarSectorDetail.tsx`
- Create: `apps/dsa-web/src/components/market-radar/index.ts`
- Create: `apps/dsa-web/src/pages/MarketRadarPage.tsx`
- Create: `apps/dsa-web/src/pages/__tests__/MarketRadarPage.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`

**Interfaces:**
- Consumes: Task 1 response types and `marketRadarApi`
- Produces: default-exported `MarketRadarPage`
- Produces component callbacks: `MarketRadarSectorList({ items, selectedSectorId, onSelect })`

- [ ] **Step 1: Write failing page behavior tests**

Mock `marketRadarApi` and provide one complete two-sector fixture. Cover the following independent tests:

```tsx
it('renders overview and preserves persisted ranking order', async () => {
  renderPage();
  expect(await screen.findByText('Selective')).toBeInTheDocument();
  const rows = screen.getAllByRole('button', { name: /sector rank/i });
  expect(rows.map((row) => row.textContent)).toEqual([
    expect.stringContaining('Semiconductor'),
    expect.stringContaining('Bank'),
  ]);
});

it('loads detail when a sector is selected', async () => {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: /Bank/ }));
  await waitFor(() => expect(marketRadarApi.getSector).toHaveBeenCalledWith('industry:bank'));
  expect(await screen.findByText('Bank ETF')).toBeInTheDocument();
});

it('shows bootstrap state and retries both summaries', async () => {
  mockEmptySummaries();
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }));
  expect(marketRadarApi.getLatest).toHaveBeenCalledTimes(2);
  expect(marketRadarApi.listSectors).toHaveBeenCalledTimes(2);
});
```

Also add tests for mismatched `runKey`, summary error, detail-only error, legacy null policy, unknown enums, manual refresh, and stale detail response protection.

- [ ] **Step 2: Run the page test and verify RED**

Run: `npm.cmd run test -- src/pages/__tests__/MarketRadarPage.test.tsx`

Expected: FAIL because `MarketRadarPage` does not exist.

- [ ] **Step 3: Add bilingual text keys**

Add the same keys to both `zh` and `en`, including:

```ts
'marketRadar.title': 'Market Radar',
'marketRadar.refresh': '刷新', // 'Refresh'
'marketRadar.emptyTitle': '暂无 Market Radar 快照',
'marketRadar.summaryError': 'Market Radar 概览加载失败',
'marketRadar.regime.selective': '精选',
'marketRadar.state.leading': '领先',
'marketRadar.quality.partial': '部分可用',
'marketRadar.unavailable': '不可用',
```

Include all regime, state, quality, ETF status, overview, ranking column, detail, evidence, position, and error labels. Do not surface untranslated enum strings as primary UI copy.

- [ ] **Step 4: Implement focused presentation components**

`MarketRadarOverview` renders four stable metric cells. `MarketRadarSectorList` renders one responsive interactive row per sector with `aria-pressed`. `MarketRadarSectorDetail` renders score/factors, risks/missing evidence, ETF alternatives, and policy caps. Shared formatting stays local and pure:

```ts
const percent = (value: number | null | undefined, digits = 1) =>
  value == null || !Number.isFinite(value) ? unavailable : `${value.toFixed(digits)}%`;

const confidence = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value) ? unavailable : `${Math.round(value * 100)}%`;
```

- [ ] **Step 5: Implement summary/detail orchestration**

Use a monotonically increasing request id in refs to ignore stale async responses:

```tsx
const summaryRequestRef = useRef(0);
const detailRequestRef = useRef(0);

const loadSummary = useCallback(async () => {
  const requestId = ++summaryRequestRef.current;
  setSummaryState({ status: 'loading' });
  try {
    const [latest, sectors] = await Promise.all([
      marketRadarApi.getLatest(),
      marketRadarApi.listSectors(),
    ]);
    if (requestId !== summaryRequestRef.current) return;
    if (latest.run && sectors.runKey && latest.run.runKey !== sectors.runKey) {
      throw new Error('Market Radar summary snapshot mismatch');
    }
    setSummaryState({ status: 'ready', latest, sectors });
  } catch (error) {
    if (requestId === summaryRequestRef.current) {
      setSummaryState({ status: 'error', error: getParsedApiError(error) });
    }
  }
}, []);
```

Select the first API-ranked sector when the prior selection is absent. Keep summary usable during detail loading/error. Set `document.title` from localized text and restore nothing on unmount, consistent with existing pages.

- [ ] **Step 6: Verify GREEN, lint the slice, and commit**

Run:

```bash
npm.cmd run test -- src/pages/__tests__/MarketRadarPage.test.tsx src/api/__tests__/marketRadar.test.ts
npm.cmd run lint -- src/pages/MarketRadarPage.tsx src/components/market-radar src/api/marketRadar.ts src/types/marketRadar.ts
```

Expected: focused tests and ESLint pass.

```bash
git add apps/dsa-web/src/components/market-radar apps/dsa-web/src/pages/MarketRadarPage.tsx apps/dsa-web/src/pages/__tests__/MarketRadarPage.test.tsx apps/dsa-web/src/i18n/uiText.ts
git commit -m "feat(web): add Market Radar cockpit"
```

### Task 3: Shell Integration, Documentation, And Visual Verification

**Files:**
- Modify: `apps/dsa-web/src/App.tsx`
- Modify: `apps/dsa-web/src/App.test.tsx`
- Modify: `apps/dsa-web/src/components/layout/SidebarNav.tsx`
- Modify: `apps/dsa-web/src/components/layout/__tests__/SidebarNav.test.tsx`
- Modify: `apps/dsa-web/src/i18n/uiText.ts`
- Modify: `docs/market-radar.md`
- Modify: `docs/INDEX.md`
- Modify: `docs/INDEX_EN.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2 default `MarketRadarPage`
- Produces: authenticated `/market-radar` route and sidebar entry

- [ ] **Step 1: Write failing route and navigation tests**

```tsx
vi.mock('./pages/MarketRadarPage', () => ({
  default: () => <div data-testid="market-radar-page">Market Radar</div>,
}));

it('routes /market-radar to the cockpit', async () => {
  window.history.pushState({}, '', '/market-radar');
  render(<App />);
  expect(await screen.findByTestId('market-radar-page')).toBeInTheDocument();
  expect(setCurrentRoute).toHaveBeenCalledWith('/market-radar');
});

it('renders the Market Radar navigation item and marks it active', () => {
  render(<MemoryRouter initialEntries={['/market-radar']}><SidebarNav /></MemoryRouter>);
  const link = screen.getByRole('link', { name: '市场雷达' });
  expect(link).toHaveAttribute('href', '/market-radar');
  expect(link).toHaveClass('font-medium');
});
```

- [ ] **Step 2: Run integration tests and verify RED**

Run: `npm.cmd run test -- src/App.test.tsx src/components/layout/__tests__/SidebarNav.test.tsx`

Expected: FAIL because the route and nav entry are absent.

- [ ] **Step 3: Register lazy route and nav entry**

Add:

```tsx
const MarketRadarPage = lazy(() => import('./pages/MarketRadarPage'));
// inside Shell routes
<Route path="/market-radar" element={<MarketRadarPage />} />
```

Add `Radar` from `lucide-react` and a nav item directly after Decision Signals:

```ts
{ key: 'market-radar', labelKey: 'layout.nav.marketRadar', to: '/market-radar', icon: Radar },
```

Add Chinese/English nav and route metadata keys.

- [ ] **Step 4: Update focused docs and changelog**

Document `/market-radar`, information order, refresh/empty/error semantics, read-only behavior, and A-share scope in `docs/market-radar.md`. Update both indexes from generic Phase 2C API wording to API + Web cockpit wording. Append one flat line under `[Unreleased]`:

```text
- [新功能] Market Radar 新增 A 股 Web 监控驾驶舱，集中展示市场状态、板块排名、ETF 候选、仓位上限与失效证据。
```

- [ ] **Step 5: Run full Web verification**

Run:

```bash
npm.cmd run test -- src/api/__tests__/marketRadar.test.ts src/pages/__tests__/MarketRadarPage.test.tsx src/App.test.tsx src/components/layout/__tests__/SidebarNav.test.tsx
npm.cmd run lint
npm.cmd run build
```

Expected: all focused tests pass, ESLint exits 0, and TypeScript/Vite build exits 0. Re-run the full test suite only to compare with the recorded two-failure baseline; do not claim it is green unless it is.

- [ ] **Step 6: Start Vite and capture visual evidence**

Run: `npm.cmd run dev -- --host 127.0.0.1 --port 5173`

Use a deterministic mocked/seeding path without committing fixture-only production behavior. Capture the full page at desktop (1440x1000) and mobile (390x844), verify no overlap or horizontal overflow, and attach screenshots to the PR body/comments.

- [ ] **Step 7: Review and commit**

Run: `git diff --check && git status --short`

Expected: only planned files are modified and there are no whitespace errors.

```bash
git add apps/dsa-web/src/App.tsx apps/dsa-web/src/App.test.tsx apps/dsa-web/src/components/layout/SidebarNav.tsx apps/dsa-web/src/components/layout/__tests__/SidebarNav.test.tsx apps/dsa-web/src/i18n/uiText.ts docs/market-radar.md docs/INDEX.md docs/INDEX_EN.md docs/CHANGELOG.md
git commit -m "docs: document Market Radar Web cockpit"
```

- [ ] **Step 8: Remote handoff**

Synchronize remote refs, push `codex/market-radar-phase-2c-web`, create one ready PR with complete scope/verification/risk/rollback sections and desktop/mobile screenshots, then wait for `web-gate`, `backend-gate`, `docker-build`, and `ai-governance`. Merge only after blocking checks and independent review pass.
