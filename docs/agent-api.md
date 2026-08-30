# Tandoor Agent API and MCP

The Agent API is the client-neutral semantic boundary for ChatGPT/MCP and other
agent clients. Tandoor remains the source of truth. The MCP service never receives
database access and there is intentionally no generic HTTP, REST-proxy, SQL, shell,
or arbitrary-model tool.

## Security and correctness rules

1. Every request uses the authenticated user's active Tandoor space.
2. Recipe reads/writes reuse Tandoor's existing recipe authorization rules.
3. Mutations use `Idempotency-Key`, transactions and append-only `AgentAuditEvent` records.
4. Existing mutable records use optimistic revisions (`updated_at`) where practical.
5. High-impact/ambiguous inventory reconciliation is previewed as an expiring `AgentProposal` and requires explicit apply confirmation.
6. Nutrition arithmetic and unit conversion are deterministic server code. AI may identify/propose food; AI never performs the final macro arithmetic.
7. Missing nutrition remains missing. Coverage/confidence is returned instead of fabricated precision.
8. Recipe variants are ordinary Tandoor recipes plus `RecipeVariantLink` lineage; the original recipe is never silently overwritten by personalization.

## Headers for writes

Agent clients should send:

- `X-Agent-Client`: stable client identifier
- `X-Request-ID`: tracing identifier
- `Idempotency-Key`: stable key for one intended mutation/retry sequence

A successful mutation and its audit event commit in the same transaction. Retrying
the same idempotency key returns the stored response.

## Nutrition provenance

`FoodNutritionProfile` stores nutrition for a specific food/product and records
basis amount/unit, source, confidence, verification, optional density and optional
barcode/brand metadata.

Source priority is explicit:

1. `user_label` - user-verified package/nutrition-label values
2. `branded` - verified exact branded record
3. `reference` - verified reference database food
4. `estimated` - comparable-food estimate
5. `ai_estimate` - explicit model estimate
6. `manual` - manual entry

The nutrition engine supports deterministic common mass/volume conversion and exact
count/custom-unit matching. Mass/volume cross-conversion requires a food-specific
`grams_per_ml` density. Unknown scoop/piece weights are not guessed.

## USDA FoodData Central

The optional server-side resolver uses the official Food Search and Food Details
API endpoints. Configure:

```text
USDA_FDC_API_KEY=<data.gov API key>
```

`nutrition/fdc/search/` returns candidates only. Nothing is persisted until a
specific candidate is submitted to `foods/{id}/nutrition/fdc/verify/` with
`confirmed=true`. Verified records are cached, saved as a nutrition profile and the
FDC ID is stored on the Tandoor food. A confirmed FDC ID that later returns 404 is
invalidated/cleared rather than silently reused. Reference foods are normalized on
a 100 g basis; branded foods preserve a documented 100 g or 100 ml basis. Ambiguous
branded serving units are rejected instead of being guessed.

Package-label photos should normally be interpreted by the external agent and saved
through `nutrition-profiles/` as a verified `user_label` profile. This keeps image
reasoning outside deterministic nutrition persistence.

## Recipe endpoints

All paths below are relative to `/api/agent/`.

- `GET|POST recipes/` - search/list or create recipes
- `GET|PATCH recipes/{id}/` - read/update with optimistic concurrency
- `POST recipes/{id}/clone/`
- `GET recipes/{id}/nutrition/`
- `POST recipes/{id}/scale-preview/` - mathematical scale
- `POST recipes/{id}/practical-scale-preview/` - explicit count rounding + custom-unit warnings
- `POST recipes/{id}/variant-preview/`
- `POST recipes/{id}/save-variant/`
- `POST recipes/{id}/pantry-check/`
- `POST recipes/{id}/substitution-context/` - configured Tandoor substitutes only
- `POST recipes/recommend/` - deterministic pantry + verifiable macro ranking

Macro-constrained variant saves are blocked if a requested target fails **or** if
nutrition coverage is insufficient to verify that target.

## Pantry endpoints

- `GET pantry/locations/`
- `GET pantry/entries/`
- `POST pantry/adjust/` - explicit signed delta such as "used 2 eggs"
- `POST pantry/reconcile-preview/` - vision/agent observations to reviewable proposal
- `GET proposals/{uuid}/`
- `POST proposals/{uuid}/apply/`

Reconciliation modes:

- `augment` (default): may add/increase observed stock but never removes unseen stock.
- `snapshot`: one explicit inventory location is treated as a complete snapshot and may propose decreases/removals. Apply still requires confirmation and an unchanged inventory revision. A snapshot with any unresolved vision observation is blocked from apply.

Recipe pantry checks accept `target_servings`, consume compatible inventory entries
in expiry order, and return complete/partial/missing/unknown quantities.

## Household endpoints

- `GET|POST shopping/lists/`
- `GET|POST shopping/entries/`
- `PATCH|DELETE shopping/entries/{id}/`
- `GET meal-types/`
- `GET|POST meal-plans/`
- `PATCH|DELETE meal-plans/{id}/`

Deletes require explicit `confirmed=true`. Shopping-entry and meal-plan updates
require the `expected_updated_at` revision returned by the corresponding read.
Meal-plan recipe references are limited to recipes visible to the authenticated user.

## Nutrition endpoints

- `GET foods/?q=...`
- `GET|POST nutrition-profiles/`
- `GET|PATCH nutrition-profiles/{id}/`
- `POST nutrition/evaluate-draft/`
- `GET nutrition/fdc/search/?q=...`
- `POST foods/{id}/nutrition/fdc/verify/`

Recipe/draft analysis returns total and per-serving nutrients, ingredient-level
provenance, per-field coverage, average confidence, `complete_core_macros`, and
warnings for unmatched units/foods.

## MCP service

`tandoor_mcp` is a separate thin service. Its tool registry maps only to the
semantic endpoints above. It contains no Tandoor models, serializers or database
credentials.

Required outbound configuration:

```text
TANDOOR_BASE_URL=https://cook.example.com
TANDOOR_API_TOKEN=<Tandoor OAuth access token with read/write scope>
```

Local stdio:

```bash
python -m tandoor_mcp --transport stdio
```

Remote Streamable HTTP:

```text
MCP_BEARER_TOKEN=<long random inbound MCP bearer token>
```

```bash
python -m tandoor_mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

Remote HTTP refuses to start without `MCP_BEARER_TOKEN` unless
`--allow-unauthenticated-http` is deliberately supplied. The production image is
`Dockerfile.mcp`; it runs as an unprivileged user and contains only the MCP adapter
and its minimal dependencies.

## MCP tool families

The MCP registry exposes semantic tools for:

- recipe search/read/create/update/clone/nutrition/practical scaling/recommendation
- pantry availability, explicit inventory deltas and photo reconciliation
- configured substitution context
- variant preview/save
- Tandoor food and nutrition-label persistence
- USDA candidate search/verification
- shopping lists and entries
- meal types and meal-plan list/create/update/delete
- audit history

There is no generic fetch, arbitrary URL, SQL, database, shell, Docker or filesystem
tool.

## Social Recipe Inbox relationship

The existing Social Recipe Inbox remains the acquisition layer for shared/pasted
social URLs. The Agent API/MCP layer consumes normal saved Tandoor recipes and does
not bypass the Social Inbox review-before-save workflow.

## Deployment boundary

This branch intentionally contains application code and deployable images but does
not change a live deployment. Run the Tandoor web application and Social Import
worker as before, and run `Dockerfile.mcp` as a separate service when MCP access is
enabled. The MCP container needs only its outbound Tandoor API token and inbound MCP
auth secret; it must not receive the Tandoor database, Docker socket, or private AI
runtime volume.
