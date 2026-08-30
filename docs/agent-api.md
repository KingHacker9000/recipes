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

For a nutrition-label photo, the vision-capable client should transcribe the label,
resolve the existing Tandoor food, then create a `user_label` profile. The image
itself does not need to be sent to Tandoor for macro calculation.

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

The same conversion rules are reused when comparing recipes with pantry inventory.

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

### Recipes

- `GET recipes/?q=...`
- `POST recipes/`
- `GET recipes/{id}/`
- `PATCH recipes/{id}/`
- `POST recipes/{id}/clone/`
- `GET recipes/{id}/nutrition/`
- `POST recipes/{id}/scale-preview/`
- `POST recipes/{id}/variant-preview/`
- `POST recipes/{id}/save-variant/`
- `POST recipes/{id}/pantry-check/`

Recipe updates require `expected_updated_at`. Saving a variant requires
`expected_parent_updated_at`. Stale writes return a conflict with the current
recipe instead of overwriting a newer edit.

### Foods and nutrition

- `GET foods/?q=...`
- `GET nutrition-profiles/?food_id=...`
- `POST nutrition-profiles/`
- `GET nutrition-profiles/{id}/`
- `PATCH nutrition-profiles/{id}/`
- `POST nutrition/evaluate-draft/`

Nutrition-profile updates require `expected_updated_at`.

### Pantry

- `GET pantry/locations/`
- `GET pantry/entries/`
- `POST pantry/reconcile-preview/`
- `GET proposals/{proposal_id}/`
- `POST proposals/{proposal_id}/apply/`

`recipes/{id}/pantry-check/` accepts an optional `target_servings`, so availability
can be checked for a different portion count without modifying the recipe.

### Audit / capabilities

- `GET health/`
- `GET audit/`

## Headers for writes

Agent adapters should send:

- `X-Agent-Client`: stable client identifier
- `X-Request-ID`: tracing identifier
- `Idempotency-Key`: unique mutation identifier

The first successful mutation for an idempotency key is recorded in
`AgentAuditEvent`; retries replay the stored response instead of duplicating the
write.

## Recipe input boundary

The Agent API intentionally accepts a smaller schema than Tandoor's generic nested
REST serializer. It allows recipe fields, keywords, steps and ingredients, with
food/unit references by ID or exact name.

Before invoking Tandoor's native `RecipeSerializer`, the Agent API verifies that:

- referenced foods and units belong to the active space
- nested step/ingredient IDs really belong to the recipe being updated
- new recipes cannot smuggle in existing nested-object IDs
- recipe/step/ingredient list sizes stay bounded
- servings and ingredient amounts are numeric and sane

This keeps Tandoor's mature nested serializer behavior while removing a generic
nested-object escape hatch from agent clients.

## Exact scaling

`scale-preview` scales numeric ingredient quantities by the requested serving
ratio and recalculates total nutrition. It deliberately labels itself `exact`.
Practical culinary changes (rounding eggs, changing pan size, adjusting seasoning,
etc.) belong to the agent's reasoning pass and must be re-evaluated by the nutrition
engine before being saved.

Pantry checks use the same serving ratio when `target_servings` is provided.

## Recipe variants

`RecipeVariantLink` stores lineage for ordinary Tandoor recipes so variants remain
normal recipes everywhere else in Tandoor.

Supported deterministic macro constraints currently include:

- `calories_max` / `calories_min`
- `protein_min_g` / `protein_max_g`
- `carbohydrate_min_g` / `carbohydrate_max_g`
- `fat_min_g` / `fat_max_g`
- `fiber_min_g` / `fiber_max_g`

Other constraints such as `inventory_policy`, dietary rules and ingredients to
preserve are returned unchanged for the agent's culinary reasoning.

A low-calorie/high-protein flow is therefore:

1. read the original recipe
2. read/analyze nutrition and pantry availability
3. propose a candidate recipe
4. call `variant-preview`
5. use deterministic macro results to revise the candidate if needed
6. call `save-variant` only after the targets are fully verifiable and satisfied

The server blocks variant saves when requested macro targets fail or cannot be
verified because nutrition coverage is incomplete.

## Pantry and fridge-photo semantics

A vision-capable client converts a fridge image into structured observations such
as food, amount, unit, location and confidence. Tandoor turns those observations
into an `AgentProposal`; the model does not directly mutate inventory.

Two reconciliation modes exist:

### `augment` (default)

- adds a newly observed food
- can increase a matching existing quantity
- never decreases/removes inventory from visual absence
- safest default for ordinary fridge photos where items may be hidden

### `snapshot`

- limited to one inventory location per proposal
- treats the observations as an explicit complete snapshot
- may decrease quantities or set unobserved entries to zero
- marked high impact

Both modes require a separate apply call with `confirmed=true`. Proposals expire
after one hour. Apply checks an inventory revision under a transaction; if the
relevant inventory changed after preview, the write is rejected and a fresh
preview is required.

Each applied quantity change also creates Tandoor `InventoryLog` history.

Unknown foods are returned as unresolved instead of silently created. Unknown unit
names are rejected. This makes image-recognition uncertainty visible to the agent
and user.

## Current development phases

### Implemented foundation

- semantic API boundary
- space and recipe authorization
- audit/idempotency
- proposals
- food-level nutrition/provenance
- deterministic macro engine
- exact portion scaling
- recipe create/update/clone
- macro-target variant preview/save + lineage
- pantry read/search
- recipe-vs-pantry availability
- fridge reconciliation preview/apply

### Next: richer pantry-aware recipe intelligence

- explicit pantry adjustment proposals (`delta`, `set`, `remove`) for commands such as "I used 2 eggs"
- pantry-aware substitution candidate contract
- recipe ranking by pantry coverage + macro targets
- practical portion scaling (rounding/count ingredients while recalculating macros)
- nutrition source import helpers and label-review workflow

### Later: household actions

- shopping list updates from missing ingredients
- meal-plan updates
- macro-target meal suggestions

### Final adapter: MCP / ChatGPT

Run a separate, thin `tandoor-mcp` service. It translates MCP tool calls into the
semantic Agent API and contains no Tandoor database/business logic. The intended
MCP tools map closely to semantic operations such as `recipes.get`,
`recipes.variant_preview`, `pantry.reconcile_preview` and
`nutrition.evaluate_draft` rather than generic HTTP/SQL primitives.
