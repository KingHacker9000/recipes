import argparse
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

import anyio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from tandoor_mcp.client import TandoorAgentClient, TandoorAgentClientError


@dataclass(frozen=True)
class ToolSpec:
    title: str
    description: str
    method: str
    path: str
    schema: dict[str, Any]
    mutation: bool = False
    path_args: tuple[str, ...] = ()


def obj(properties=None, required=None):
    return {
        'type': 'object',
        'properties': properties or {},
        'required': required or [],
        'additionalProperties': False,
    }


def integer(description):
    return {'type': 'integer', 'description': description}


def number(description):
    return {'type': 'number', 'description': description}


def string(description):
    return {'type': 'string', 'description': description}


def boolean(description):
    return {'type': 'boolean', 'description': description}


def array(item_schema, description=''):
    value = {'type': 'array', 'items': item_schema}
    if description:
        value['description'] = description
    return value


def free_object(description=''):
    value = {'type': 'object', 'additionalProperties': True}
    if description:
        value['description'] = description
    return value


IDEMPOTENCY = {
    'idempotency_key': string('Stable retry key. Reuse it only when retrying the same intended mutation.')
}


TOOLS: dict[str, ToolSpec] = {
    'tandoor_health': ToolSpec(
        'Tandoor health', 'Check Agent API connectivity and capabilities.', 'GET', '/api/agent/health/', obj()),
    'recipes_search': ToolSpec(
        'Search recipes', 'Search recipes visible to the authenticated Tandoor user.', 'GET', '/api/agent/recipes/',
        obj({'q': string('Name/description query.'), 'limit': integer('Maximum results, up to 50.')})),
    'recipe_get': ToolSpec(
        'Get recipe', 'Get one recipe including steps and ingredients.', 'GET', '/api/agent/recipes/{recipe_id}/',
        obj({'recipe_id': integer('Tandoor recipe ID.')}, ['recipe_id']), path_args=('recipe_id',)),
    'recipe_create': ToolSpec(
        'Create recipe', 'Create a recipe through the constrained Agent API recipe schema.', 'POST', '/api/agent/recipes/',
        obj({'recipe': free_object('Recipe fields, steps and ingredients.'), **IDEMPOTENCY}, ['recipe']), True),
    'recipe_update': ToolSpec(
        'Update recipe', 'Update an accessible recipe using its optimistic updated_at revision.', 'PATCH', '/api/agent/recipes/{recipe_id}/',
        obj({
            'recipe_id': integer('Recipe ID.'),
            'expected_updated_at': string('Revision returned by recipe_get.'),
            'recipe': free_object('Fields/steps to update.'),
            **IDEMPOTENCY,
        }, ['recipe_id', 'expected_updated_at', 'recipe']), True, ('recipe_id',)),
    'recipe_clone': ToolSpec(
        'Clone recipe', 'Clone an accessible recipe and optionally override safe recipe fields.', 'POST', '/api/agent/recipes/{recipe_id}/clone/',
        obj({
            'recipe_id': integer('Source recipe ID.'), 'name': string('Optional clone name.'),
            'overrides': free_object('Safe recipe overrides.'), **IDEMPOTENCY,
        }, ['recipe_id']), True, ('recipe_id',)),
    'recipe_nutrition': ToolSpec(
        'Analyze recipe nutrition', 'Calculate deterministic macros and nutrition coverage.', 'GET', '/api/agent/recipes/{recipe_id}/nutrition/',
        obj({'recipe_id': integer('Recipe ID.')}, ['recipe_id']), path_args=('recipe_id',)),
    'recipe_exact_scale': ToolSpec(
        'Preview exact scale', 'Mathematically scale ingredient quantities to target servings.', 'POST', '/api/agent/recipes/{recipe_id}/scale-preview/',
        obj({'recipe_id': integer('Recipe ID.'), 'target_servings': number('Desired servings.')}, ['recipe_id', 'target_servings']), path_args=('recipe_id',)),
    'recipe_practical_scale': ToolSpec(
        'Preview practical scale', 'Scale servings with explicit discrete-count rounding and custom-unit warnings.', 'POST', '/api/agent/recipes/{recipe_id}/practical-scale-preview/',
        obj({
            'recipe_id': integer('Recipe ID.'), 'target_servings': number('Desired servings.'),
            'count_step': number('Count rounding step, normally 1.'),
            'count_rounding': {'type': 'string', 'enum': ['nearest', 'up', 'down']},
        }, ['recipe_id', 'target_servings']), path_args=('recipe_id',)),
    'recipes_recommend': ToolSpec(
        'Recommend recipes', 'Rank recipes deterministically by pantry availability and verifiable macro targets.', 'POST', '/api/agent/recipes/recommend/',
        obj({
            'query': string('Optional recipe text filter.'), 'target_servings': number('Pantry target servings.'),
            'calories_max': number('Maximum calories per serving.'), 'protein_min_g': number('Minimum protein grams per serving.'),
            'limit': integer('Maximum recommendations, up to 25.'),
        })),
    'recipe_pantry_check': ToolSpec(
        'Check recipe pantry fit', 'Calculate whether inventory can satisfy a recipe at target servings.', 'POST', '/api/agent/recipes/{recipe_id}/pantry-check/',
        obj({'recipe_id': integer('Recipe ID.'), 'target_servings': number('Optional target servings.')}, ['recipe_id']), path_args=('recipe_id',)),
    'recipe_substitution_context': ToolSpec(
        'Get substitution context', 'Return missing ingredients and Tandoor-configured substitutes with pantry availability.', 'POST', '/api/agent/recipes/{recipe_id}/substitution-context/',
        obj({'recipe_id': integer('Recipe ID.'), 'target_servings': number('Optional target servings.')}, ['recipe_id']), path_args=('recipe_id',)),
    'recipe_variant_preview': ToolSpec(
        'Preview recipe variant', 'Evaluate an unsaved candidate against deterministic macro constraints.', 'POST', '/api/agent/recipes/{recipe_id}/variant-preview/',
        obj({'recipe_id': integer('Parent recipe ID.'), 'candidate': free_object('Candidate recipe.'), 'constraints': free_object('Macro/policy constraints.')}, ['recipe_id', 'candidate']), path_args=('recipe_id',)),
    'recipe_save_variant': ToolSpec(
        'Save recipe variant', 'Save a reviewed variant as a normal Tandoor recipe with lineage.', 'POST', '/api/agent/recipes/{recipe_id}/save-variant/',
        obj({
            'recipe_id': integer('Parent recipe ID.'), 'expected_parent_updated_at': string('Parent revision.'),
            'candidate': free_object('Candidate recipe.'), 'constraints': free_object('Constraints.'),
            'variant_type': string('Variant label.'), 'change_summary': array({}, 'Human-readable changes.'), **IDEMPOTENCY,
        }, ['recipe_id', 'expected_parent_updated_at', 'candidate']), True, ('recipe_id',)),
    'foods_search': ToolSpec(
        'Search foods', 'Find existing Tandoor foods and their selected nutrition profile.', 'GET', '/api/agent/foods/',
        obj({'q': string('Food query.'), 'limit': integer('Maximum results.')})),
    'nutrition_profiles': ToolSpec(
        'List nutrition profiles', 'List saved nutrition profiles, optionally for one food.', 'GET', '/api/agent/nutrition-profiles/',
        obj({'food_id': integer('Optional Tandoor food ID.')})),
    'nutrition_profile_create': ToolSpec(
        'Save nutrition profile', 'Save structured nutrition, including a user-verified package label.', 'POST', '/api/agent/nutrition-profiles/',
        obj({
            'food_id': integer('Food ID.'), 'basis_amount': number('Basis amount.'), 'basis_unit': string('Basis unit.'),
            'calories': number('Calories.'), 'protein_g': number('Protein grams.'), 'carbohydrate_g': number('Carb grams.'),
            'fat_g': number('Fat grams.'), 'fiber_g': number('Fiber grams.'), 'sugar_g': number('Sugar grams.'),
            'sodium_mg': number('Sodium milligrams.'), 'grams_per_ml': number('Optional density.'),
            'label': string('Product/label name.'), 'brand': string('Brand.'), 'barcode': string('Barcode.'),
            'source_type': {'type': 'string', 'enum': ['user_label', 'branded', 'reference', 'estimated', 'ai_estimate', 'manual']},
            'source_reference': string('Provenance reference.'), 'confidence': number('0 to 1.'),
            'verified': boolean('Human/user verified.'), 'is_default': boolean('Make default.'), **IDEMPOTENCY,
        }, ['food_id']), True),
    'nutrition_profile_update': ToolSpec(
        'Update nutrition profile', 'Update a profile with optimistic concurrency.', 'PATCH', '/api/agent/nutrition-profiles/{profile_id}/',
        obj({
            'profile_id': integer('Profile ID.'), 'expected_updated_at': string('Profile revision.'),
            'basis_amount': number('Basis amount.'), 'basis_unit': string('Basis unit.'), 'calories': number('Calories.'),
            'protein_g': number('Protein grams.'), 'carbohydrate_g': number('Carb grams.'), 'fat_g': number('Fat grams.'),
            'fiber_g': number('Fiber grams.'), 'sugar_g': number('Sugar grams.'), 'sodium_mg': number('Sodium mg.'),
            'grams_per_ml': number('Density.'), 'source_type': string('Source type.'), 'source_reference': string('Source.'),
            'confidence': number('0 to 1.'), 'verified': boolean('Verified.'), 'is_default': boolean('Default.'), **IDEMPOTENCY,
        }, ['profile_id', 'expected_updated_at']), True, ('profile_id',)),
    'nutrition_evaluate_draft': ToolSpec(
        'Evaluate draft nutrition', 'Calculate macros for unsaved ingredient items.', 'POST', '/api/agent/nutrition/evaluate-draft/',
        obj({'ingredients': array(free_object('Ingredient amount/food/unit.'), 'Draft ingredients.')}, ['ingredients'])),
    'fdc_search': ToolSpec(
        'Search USDA FoodData Central', 'Search for candidate reference/branded foods without persisting.', 'GET', '/api/agent/nutrition/fdc/search/',
        obj({'q': string('Food query.'), 'limit': integer('Maximum candidates, up to 25.')}, ['q'])),
    'fdc_verify': ToolSpec(
        'Verify USDA food match', 'Persist a specifically reviewed FDC candidate as a verified nutrition profile.', 'POST', '/api/agent/foods/{food_id}/nutrition/fdc/verify/',
        obj({
            'food_id': integer('Tandoor food ID.'), 'fdc_id': integer('Reviewed FDC ID.'), 'confirmed': boolean('Must be true.'),
            'is_default': boolean('Use as default.'), 'force_refresh': boolean('Bypass FDC cache.'), **IDEMPOTENCY,
        }, ['food_id', 'fdc_id', 'confirmed']), True, ('food_id',)),
    'pantry_locations': ToolSpec(
        'List pantry locations', 'Discover inventory location IDs and names.', 'GET', '/api/agent/pantry/locations/', obj()),
    'pantry_entries': ToolSpec(
        'List pantry entries', 'Read normalized inventory entries.', 'GET', '/api/agent/pantry/entries/',
        obj({'q': string('Search text.'), 'location_id': integer('Location ID.'), 'food_id': integer('Food ID.'), 'include_empty': boolean('Include zero amounts.'), 'limit': integer('Maximum results.')})),
    'pantry_adjust': ToolSpec(
        'Adjust pantry inventory', 'Apply an explicit signed delta such as used 2 eggs or bought 500 g chicken.', 'POST', '/api/agent/pantry/adjust/',
        obj({
            'entry_id': integer('Existing entry ID.'), 'food_id': integer('Food ID for resolution/create.'),
            'location_id': integer('Location ID for resolution/create.'), 'unit_id': integer('Optional unit ID.'),
            'delta': number('Signed amount change.'), 'expected_updated_at': string('Required existing-entry revision.'),
            'reason': string('Reason for history.'), 'note': string('Optional note.'), **IDEMPOTENCY,
        }, ['delta']), True),
    'pantry_reconcile_preview': ToolSpec(
        'Preview pantry reconciliation', 'Convert structured fridge/photo observations into a reviewable proposal.', 'POST', '/api/agent/pantry/reconcile-preview/',
        obj({
            'observations': array(free_object('One vision observation.'), 'Observed foods.'),
            'mode': {'type': 'string', 'enum': ['augment', 'snapshot']}, 'default_location_id': integer('Default location ID.'), **IDEMPOTENCY,
        }, ['observations']), True),
    'proposal_get': ToolSpec(
        'Get proposal', 'Retrieve a pending/applied AgentProposal and its review diff.', 'GET', '/api/agent/proposals/{proposal_id}/',
        obj({'proposal_id': string('Proposal UUID.')}, ['proposal_id']), path_args=('proposal_id',)),
    'proposal_apply': ToolSpec(
        'Apply proposal', 'Apply a reviewed proposal after explicit confirmation and revision validation.', 'POST', '/api/agent/proposals/{proposal_id}/apply/',
        obj({'proposal_id': string('Proposal UUID.'), 'confirmed': boolean('Must be true.'), **IDEMPOTENCY}, ['proposal_id', 'confirmed']), True, ('proposal_id',)),
    'shopping_lists': ToolSpec(
        'List shopping lists', 'List shopping lists in the active space.', 'GET', '/api/agent/shopping/lists/', obj()),
    'shopping_list_create': ToolSpec(
        'Create shopping list', 'Create a shopping list.', 'POST', '/api/agent/shopping/lists/',
        obj({'name': string('List name.'), 'description': string('Description.'), 'color': string('Optional hex color.'), **IDEMPOTENCY}, ['name']), True),
    'shopping_entries': ToolSpec(
        'List shopping entries', 'List shopping entries, optionally within one list.', 'GET', '/api/agent/shopping/entries/',
        obj({'shopping_list_id': integer('List ID.'), 'include_checked': boolean('Include completed entries.')})),
    'shopping_entry_add': ToolSpec(
        'Add shopping entry', 'Add a food/amount to one or more shopping lists.', 'POST', '/api/agent/shopping/entries/',
        obj({
            'food_id': integer('Food ID.'), 'amount': number('Amount.'), 'unit_id': integer('Optional unit ID.'),
            'shopping_list_ids': array({'type': 'integer'}, 'Target shopping list IDs.'), **IDEMPOTENCY,
        }, ['food_id']), True),
    'shopping_entry_update': ToolSpec(
        'Update shopping entry', 'Update quantity, unit, checked state or list membership.', 'PATCH', '/api/agent/shopping/entries/{entry_id}/',
        obj({
            'entry_id': integer('Entry ID.'), 'expected_updated_at': string('Entry revision.'), 'amount': number('New amount.'),
            'unit_id': integer('New unit.'), 'checked': boolean('Completed state.'),
            'shopping_list_ids': array({'type': 'integer'}, 'Target list IDs.'), **IDEMPOTENCY,
        }, ['entry_id', 'expected_updated_at']), True, ('entry_id',)),
    'shopping_entry_delete': ToolSpec(
        'Delete shopping entry', 'Delete one shopping entry after explicit confirmation.', 'DELETE', '/api/agent/shopping/entries/{entry_id}/',
        obj({'entry_id': integer('Entry ID.'), 'confirmed': boolean('Must be true.'), **IDEMPOTENCY}, ['entry_id', 'confirmed']), True, ('entry_id',)),
    'meal_types': ToolSpec(
        'List meal types', 'List configured meal types and default times.', 'GET', '/api/agent/meal-types/', obj()),
    'meal_plans': ToolSpec(
        'List meal plans', 'List accessible meal plans in an optional date range.', 'GET', '/api/agent/meal-plans/',
        obj({'from': string('Earliest overlap ISO date/datetime.'), 'to': string('Latest overlap ISO date/datetime.')})),
    'meal_plan_add': ToolSpec(
        'Add meal plan', 'Schedule an accessible recipe or titled meal.', 'POST', '/api/agent/meal-plans/',
        obj({
            'recipe_id': integer('Optional accessible recipe ID.'), 'meal_type_id': integer('Meal type ID.'),
            'servings': number('Planned servings.'), 'title': string('Optional title.'), 'note': string('Optional note.'),
            'from_date': string('ISO date/datetime.'), 'to_date': string('Optional ISO date/datetime.'), **IDEMPOTENCY,
        }, ['meal_type_id', 'from_date']), True),
    'meal_plan_delete': ToolSpec(
        'Delete meal plan', 'Delete an accessible meal plan after explicit confirmation.', 'DELETE', '/api/agent/meal-plans/{meal_plan_id}/',
        obj({'meal_plan_id': integer('Meal plan ID.'), 'confirmed': boolean('Must be true.'), **IDEMPOTENCY}, ['meal_plan_id', 'confirmed']), True, ('meal_plan_id',)),
    'audit_events': ToolSpec(
        'List agent audit events', 'Read the authenticated user’s recent Agent API audit history.', 'GET', '/api/agent/audit/',
        obj({'limit': integer('Maximum events, up to 200.')})),
}


async def invoke_tool(spec: ToolSpec, arguments: dict[str, Any]):
    client = TandoorAgentClient()
    args = dict(arguments or {})
    try:
        path = spec.path.format(**args)
    except KeyError as exc:
        raise TandoorAgentClientError(f'Missing required path argument: {exc.args[0]}.')
    payload = {key: value for key, value in args.items() if key not in spec.path_args and key != 'idempotency_key'}
    params = payload if spec.method == 'GET' else None
    body = payload if spec.method != 'GET' else None
    return await client.request(
        spec.method,
        path,
        params=params,
        json=body,
        mutation=spec.mutation,
        idempotency_key=args.get('idempotency_key'),
    )


async def handle_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, title=spec.title, description=spec.description, input_schema=spec.schema)
        for name, spec in TOOLS.items()
    ])


async def handle_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    spec = TOOLS.get(params.name)
    if spec is None:
        raise ValueError(f'Unknown tool: {params.name}')
    try:
        payload = await invoke_tool(spec, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))]
        )
    except TandoorAgentClientError as exc:
        return types.CallToolResult(is_error=True, content=[types.TextContent(type='text', text=str(exc))])


class BearerAuthMiddleware:
    def __init__(self, app, token):
        self.app = app
        self.token = token.encode('utf-8')

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            headers = {key.lower(): value for key, value in scope.get('headers', [])}
            supplied = headers.get(b'authorization', b'')
            expected = b'Bearer ' + self.token
            if not hmac.compare_digest(supplied, expected):
                await send({'type': 'http.response.start', 'status': 401, 'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build_server():
    return Server('tandoor-mcp', on_list_tools=handle_list_tools, on_call_tool=handle_call_tool)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Thin MCP adapter for the semantic Tandoor Agent API.')
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-unauthenticated-http', action='store_true')
    args = parser.parse_args(argv)

    # Validate outbound credentials at startup instead of failing on first tool use.
    TandoorAgentClient()
    app = build_server()

    if args.transport == 'streamable-http':
        import uvicorn

        token = str(os.environ.get('MCP_BEARER_TOKEN') or '').strip()
        if not token and not args.allow_unauthenticated_http:
            raise SystemExit('MCP_BEARER_TOKEN is required for streamable-http unless --allow-unauthenticated-http is explicitly set.')
        asgi = app.streamable_http_app()
        if token:
            asgi = BearerAuthMiddleware(asgi, token)
        uvicorn.run(asgi, host=args.host, port=args.port)
        return 0

    from mcp.server.stdio import stdio_server

    async def arun():
        async with stdio_server() as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    anyio.run(arun)
    return 0
