# Pattern: V-Model Test Alignment

> Type: Architecture Standard  
> Status: Adopted  
> ADR: ADR-027  
> Used by: All instances (test authorship), architect (traceability review), sys-edge (tier categorisation)

## Overview

The V-model is a software development methodology where each specification artefact on the left side has a corresponding verification artefact on the right side. This system's requirements model and test tiers map directly onto the V:

```
SPECIFICATION                           VERIFICATION
═══════════════════════════════════     ═══════════════════════════════════

User Stories (SysUserStory)        ↔    E2E tests
  "Manager restructures org and          Full multi-tool journeys spanning
   cascades strategy in one session"     multiple UCs and instances

       │                                         │
       ▼                                         ▼

Use Cases (SysUseCase)             ↔    UI Flow tests  [testType: usecase]
  UC-MGT-004: Manager creates           Playwright: single UC flow within
  a bounded-authority delegation        one instance's tools

       │                                         │
       ▼                                         ▼

Features / Modules (SysFeature)    ↔    Integration / Component tests  [testType: integration / component]
  F-DLG-001: Delegation lifecycle       Python pytest API tests,
  management                            Go httptest handler tests

       │                                         │
       ▼                                         ▼

Routines / Symbols (SysSymbol)     ↔    Unit tests  [testType: component]
  handleCreateDelegation()              Go *_test.go functions,
  DelegationStore.create()              vitest component tests
```

---

## Four test tiers

| Tier | testType | What it tests | Written by | Links to |
|------|----------|---------------|------------|----------|
| **E2E** | `e2e` | Full user journey spanning multiple UCs and tools (cross-instance) | master | SysUserStory |
| **UI Flow** | `usecase` | Single UC Playwright flow within one instance's tools | Owning instance | SysUseCase → SysFeature |
| **Integration** | `integration` | API endpoints, service contracts, backend behaviour | Owning instance | SysFeature |
| **Unit** | `component` | Individual functions, Go handlers, React components | Owning instance | SysSymbol |

**Prior state:** The system had 3 tiers (`component`, `integration`, `usecase`) which collapsed E2E and UI Flow into `usecase`. The V-model formalisation adds `e2e` as a distinct 4th tier at the User Story level, owned by master.

---

## Authorship responsibilities

```
E2E tests    →  master (cross-instance journeys derived from User Stories)
UI Flow      →  owning instance (single UC, see shared/patterns/uc-ui-testing.md)
Integration  →  owning instance (API/service tests)
Unit         →  owning instance (function/handler tests)
```

---

## Graph traceability per tier

```
SysUserStory  ←─── REALIZED_BY ───→  SysUseCase
                                           │
                                      REQUIRES
                                           │
                                           ▼
SysSymbol ──── IMPLEMENTS ──────────  SysFeature  ←────── SysEndpoint
                                           │
                                      VERIFIES (via SysTest)
                                           │
                                           ▼
                             SysTest {testType: e2e | usecase | integration | component}
```

**Coverage check in `briefing`:** The three displayed tiers (`cmp`, `int`, `uc`) correspond to `component`, `integration`, `usecase`. E2E tests are not yet shown per-feature (they link to User Stories, not Features). The health check surfaces missing tiers.

---

## Implications for new work

1. **When writing a new UC** (instance): also write a UI flow test for it (`testType: usecase`). See `uc-ui-testing.md`.
2. **When master closes a User Story** (all UCs realised, all features tested): write or verify the E2E test that covers the full journey.
3. **When architect reviews traceability**: check that every UC has at least one `usecase`-tier test, and every User Story has at least one `e2e`-tier test.
4. **Coverage gap** — a UC with only integration tests is insufficiently tested: the user's path through the UI is unverified even if the API is correct.

---

## Why the V-model over alternatives

- **Test pyramid** (many unit, few E2E): biases toward implementation details over behaviour. The V-model biases toward requirements, which is correct for a product built from formal ontology and use cases.
- **Ad-hoc testing**: no systematic link between what is specified and what is verified. The V-model makes every gap visible in the graph.
- **BDD (behaviour-driven)**: compatible — use case steps can map directly to Gherkin scenarios. The V-model adds the structural layer above BDD.
