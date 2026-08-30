# Tandoor Agent API

The Agent API is a stable, client-neutral semantic boundary for ChatGPT/MCP and
future agent clients. It deliberately does **not** expose a generic proxy to the
legacy Tandoor REST API or direct database access.

## Design rules

1. Tandoor remains the source of truth for recipes, inventory, shopping and meal planning.
2. The agent performs language/vision/culinary reasoning; deterministic server code performs unit conversion, macro arithmetic, authorization and persistence.
3. Every object stays scoped to the authenticated user's active Tandoor space.
4. Private recipe access reuses Tandoor's existing object-level recipe permission rules.
5. Writes use idempotency keys, optimistic revision checks where applicable, transactions and append-only audit events.
6. High-impact or ambiguous bulk changes are previewed as `AgentProposal` objects before apply.
7. Unknown nutrition remains unknown. The API reports coverage/confidence instead of inventing precision.
8. MCP is an adapter. The MCP service must call this API and must never connect to the database directly.

## Nutrition provenance

`FoodNutritionProfile` stores nutrition per a declared amount/unit and records its
source and confidence. Source preference is intentionally explicit rather than
hidden inside an AI prompt:

1. `user_label` - user-verified package/nutrition-label data
2. `branded` - exact branded product record
3. `reference` - reference food database
4. `estimated` - comparable-food estimate
5. `ai_estimate` - model estimate
6. `manual` - manual entry

A profile can be marked `verified` and/or `is_default`. Only one default profile
may exist for the same food in a space.

## Deterministic unit conversion

The first implementation supports common mass and volume units plus exact custom
unit matching. It never guesses incompatible dimensions.

Mass is normalized to grams. Volume is normalized to millilitres. A profile can
supply `grams_per_ml` to permit deterministic volume/mass conversion for that
specific food. Custom/count units only match the same normalized unit (for example
`each` to `each`).

If an ingredient says `2 scoops` but the available profile is per 100 g and no
scoop weight is known, the ingredient is reported as unmatched rather than being
estimated silently.

## Macro result contract

Recipe and draft analysis returns:

- total calories/protein/carbohydrate/fat plus optional fiber/sugar/sodium
- per-serving values for stored recipes
- per-ingredient profile/provenance and nutrient contribution
- ingredient coverage
- per-field coverage
- average profile confidence
- `complete_core_macros`
- warnings for unmatched ingredients/units

This allows a client to say "approximately 520 kcal, 94% nutrition coverage" rather
than presenting false exactness.

## Current v1 endpoints

All endpoints are under `/api/agent/` and require an authenticated Tandoor user
with read/write token scope.

- `GET health/`
- `GET recipes/?q=...`
- `GET recipes/{id}/`
- `GET recipes/{id}/nutrition/`
- `POST recipes/{id}/scale-preview/`
- `GET foods/?q=...`
- `GET nutrition-profiles/?food_id=...`
- `POST nutrition-profiles/`
- `GET nutrition-profiles/{id}/`
- `PATCH nutrition-profiles/{id}/`
- `POST nutrition/evaluate-draft/`
- `GET audit/`

### Headers for writes

Agent adapters should send:

- `X-Agent-Client`: stable client identifier
- `X-Request-ID`: tracing identifier
- `Idempotency-Key`: unique mutation identifier

The first successful mutation for an idempotency key is recorded in
`AgentAuditEvent`; retries replay the stored response instead of duplicating the
write.

## Exact scaling

`scale-preview` scales numeric ingredient quantities by the requested serving
ratio and recalculates total nutrition. It deliberately labels itself `exact`.
Practical culinary changes (rounding eggs, changing pan size, adjusting seasoning,
etc.) belong to the agent's reasoning pass and must be re-evaluated by the nutrition
engine before being saved.

## Recipe variants

`RecipeVariantLink` stores lineage for ordinary Tandoor recipes so variants remain
normal recipes everywhere else in Tandoor. Planned variant constraints include:

- maximum calories per serving
- minimum protein per serving
- carbohydrate/fat limits
- target serving count
- pantry-only / prefer-pantry / preserve-original-quality policies
- required/preserved ingredients
- dietary constraints

The model proposes a candidate recipe, calls `nutrition/evaluate-draft/`, iterates
until the requested constraints are met (or reports why they cannot be met), then
saves a normal recipe plus lineage metadata only after explicit write approval.

## Planned phases

### Phase 2 - recipe mutation and variants

- create recipe
- update recipe with optimistic concurrency
- clone recipe
- `variant-preview`
- `save-variant`
- practical portion scaling

### Phase 3 - pantry intelligence

- pantry read/search
- recipe-vs-pantry availability calculation
- fridge/photo reconciliation proposals
- nutrition-label photo ingestion
- missing-ingredient and substitution previews

### Phase 4 - household actions

- shopping list updates
- meal-plan updates
- pantry-aware recipe ranking
- macro-target meal suggestions

### Phase 5 - MCP / ChatGPT adapter

Run a separate, thin `tandoor-mcp` service. It translates MCP tool calls into the
semantic Agent API and contains no Tandoor database/business logic. The intended
MCP tools map closely to semantic operations such as `recipes.get`,
`recipes.variant_preview`, `pantry.reconcile_preview` and
`nutrition.evaluate_draft` rather than generic HTTP/SQL primitives.
