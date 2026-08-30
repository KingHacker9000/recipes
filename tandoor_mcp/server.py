import argparse
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

import anyio
import mcp.types as types
from mcp.server import Server, ServerRequestContext

from tandoor_mcp.client import TandoorAgentClient, TandoorAgentClientError


@dataclass(frozen=True)
class ToolSpec:
    title: str
    description: str
    schema: dict[str, Any]
    invoke: Callable[[TandoorAgentClient, dict[str, Any]], Any]


def obj(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


def integer(description):
    return {'type': 'integer', 'description': description}


def number(description):
    return {'type': 'number', 'description': description}


def string(description):
    return {'type': 'string', 'description': description}


def boolean(description):
    return {'type': 'boolean', 'description': description}


def _idempotency(args):
    return args.get('idempotency_key')


async def _get(client, path, args, *, params=None):
    return await client.request('GET', path, params=params if params is not None else args)


async def _post(client, path, args, *, mutation=False, body=None):
    payload = dict(args if body is None else body)
    payload.pop('idempotency_key', None)
    return await client.request('POST', path, json=payload, mutation=mutation, idempotency_key=_idempotency(args))


async def _patch(client, path, args, *, mutation=True):
    payload = dict(args)
    payload.pop('idempotency_key', None)
    return await client.request('PATCH', path, json=payload, mutation=mutation, idempotency_key=_idempotency(args))


async def _delete(client, path, args):
    payload = dict(args)
    payload.pop('idempotency_key', None)
    return await client.request('DELETE', path, json=payload, mutation=True, idempotency_key=_idempotency(args))


IDEMPOTENCY = {'idempotency_key': string('Stable retry key for a write. Reuse it when retrying the same intended mutation.')}


TOOLS: dict[str, ToolSpec] = {
    'tandoor_health': ToolSpec(
        'Tandoor Agent API health',
        'Check Agent API connectivity and advertised capabilities.',
        obj(),
        lambda c, a: _get(c, '/api/agent/health/', {}),
    ),
    'recipes_search': ToolSpec(
        'Search recipes',
        'Search recipes visible to the authenticated Tandoor user.',
        obj({'q': string('Name/description query.'), 'limit': integer('Maximum results, up to 50.')}),
        lambda c, a: _get(c, '/api/agent/recipes/', a),
    ),
    'recipe_get': ToolSpec(
        'Get recipe',
        'Get one recipe including steps and ingredients.',
        obj({'recipe_id': integer('Tandoor recipe ID.')}, ['recipe_id']),
        lambda c, a: _get(c, f"/api/agent/recipes/{a['recipe_id']}/", {}),
    ),
    'recipe_nutrition': ToolSpec(
        'Analyze recipe nutrition',
        'Calculate deterministic macros and nutrition coverage for a recipe.',
        obj({'recipe_id': integer('Tandoor recipe ID.')}, ['recipe_id']),
        lambda c, a: _get(c, f"/api/agent/recipes/{a['recipe_id']}/nutrition/", {}),
    ),
    'recipe_practical_scale': ToolSpec(
        'Preview practical scaling',
        'Scale a recipe to a target serving count, explicitly rounding discrete count ingredients and flagging custom units.',
        obj({
            'recipe_id': integer('Tandoor recipe ID.'),
            'target_servings': number('Desired servings.'),
            'count_step': number('Discrete count rounding step, normally 1.'),
            'count_rounding': {'type': 'string', 'enum': ['nearest', 'up', 'down']},
        }, ['recipe_id', 'target_servings']),
        lambda c, a: _post(c, f"/api/agent/recipes/{a['recipe_id']}/practical-scale-preview/", {k: v for k, v in a.items() if k != 'recipe_id'}),
    ),
    'recipes_recommend': ToolSpec(
        'Recommend recipes',
        'Rank visible recipes using pantry completeness and fully verifiable calorie/protein targets.',
        obj({
            'query': string('Optional text filter.'),
            'target_servings': number('Target servings for pantry availability.'),
            'calories_max': number('Maximum calories per serving.'),
            'protein_min_g': number('Minimum protein grams per serving.'),
            'limit': integer('Maximum returned recommendations, up to 25.'),
        }),
        lambda c, a: _post(c, '/api/agent/recipes/recommend/', a),
    ),
    'recipe_pantry_check': ToolSpec(
        'Check recipe against pantry',
        'Determine whether pantry inventory can satisfy a recipe at a target serving count.',
        obj({'recipe_id': integer('Tandoor recipe ID.'), 'target_servings': number('Optional target servings.')}, ['recipe_id']),
        lambda c, a: _post(c, f"/api/agent/recipes/{a['recipe_id']}/pantry-check/", {k: v for k, v in a.items() if k != 'recipe_id'}),
    ),
    'recipe_substitution_context': ToolSpec(
        'Get substitution context',
        'Return missing ingredients plus Tandoor-configured substitutes and their pantry availability. Does not invent substitutions.',
        obj({'recipe_id': integer('Tandoor recipe ID.'), 'target_servings': number('Optional target servings.')}, ['recipe_id']),
        lambda c, a: _post(c, f"/api/agent/recipes/{a['recipe_id']}/substitution-context/", {k: v for k, v in a.items() if k != 'recipe_id'}),
    ),
    'recipe_variant_preview': ToolSpec(
        'Preview recipe variant',
        'Evaluate an unsaved candidate variant against deterministic macro constraints.',
        obj({
            'recipe_id': integer('Parent recipe ID.'),
            'candidate': {'type': 'object', 'description': 'Agent recipe candidate.'},
            'constraints': {'type': 'object', 'description': 'Macro and policy constraints.'},
        }, ['recipe_id', 'candidate']),
        lambda c, a: _post(c, f"/api/agent/recipes/{a['recipe_id']}/variant-preview/", {k: v for k, v in a.items() if k != 'recipe_id'}),
    ),
    'recipe_save_variant': ToolSpec(
        'Save recipe variant',
        'Persist a reviewed variant as a normal Tandoor recipe with lineage. Macro-constrained saves are blocked if targets cannot be verified.',
        obj({
            'recipe_id': integer('Parent recipe ID.'),
            'expected_parent_updated_at': string('Parent recipe revision from recipe_get.'),
            'candidate': {'type': 'object'},
            'constraints': {'type': 'object'},
            'variant_type': string('Variant label such as high_protein or low_calorie.'),
            'change_summary': {'type': 'array', 'items': {}},
            **IDEMPOTENCY,
        }, ['recipe_id', 'expected_parent_updated_at', 'candidate']),
        lambda c, a: _post(c, f"/api/agent/recipes/{a['recipe_id']}/save-variant/", {k: v for k, v in a.items() if k != 'recipe_id'}, mutation=True),
    ),
    'pantry_entries': ToolSpec(
        'List pantry inventory',
        'Read normalized Tandoor inventory entries, optionally filtering by food/location/text.',
        obj({
            'q': string('Optional search text.'),
            'location_id': integer('Inventory location ID.'),
            'food_id': integer('Food ID.'),
            'include_empty': boolean('Include zero-amount entries.'),
            'limit': integer('Maximum results.'),
        }),
        lambda c, a: _get(c, '/api/agent/pantry/entries/', a),
    ),
    'pantry_adjust': ToolSpec(
        'Adjust pantry inventory',
        'Apply an explicit delta such as used two eggs or bought 500 g chicken. Existing entries require their updated_at revision.',
        obj({
            'entry_id': integer('Existing inventory entry ID.'),
            'food_id': integer('Food ID when creating/resolving an entry.'),
            'location_id': integer('Inventory location ID when creating/resolving an entry.'),
            'unit_id': integer('Optional unit ID.'),
            'delta': number('Signed amount change.'),
            'expected_updated_at': string('Required revision for an existing entry.'),
            'reason': string('Human-readable reason for inventory history.'),
            'note': string('Optional entry note.'),
            **IDEMPOTENCY,
        }, ['delta']),
        lambda c, a: _post(c, '/api/agent/pantry/adjust/', a, mutation=True),
    ),
    'pantry_reconcile_preview': ToolSpec(
        'Preview fridge/pantry reconciliation',
        'Turn structured vision observations into a reviewable inventory proposal. augment never removes unseen stock; snapshot may propose removals.',
        obj({
            'observations': {'type': 'array', 'items': {'type': 'object'}},
            'mode': {'type': 'string', 'enum': ['augment', 'snapshot']},
            'default_location_id': integer('Default inventory location for observations.'),
            **IDEMPOTENCY,
        }, ['observations']),
        lambda c, a: _post(c, '/api/agent/pantry/reconcile-preview/', a, mutation=True),
    ),
    'proposal_apply': ToolSpec(
        'Apply reviewed proposal',
        'Apply a pending AgentProposal after explicit confirmation and revision checks.',
        obj({'proposal_id': string('Proposal UUID.'), 'confirmed': boolean('Must be true.'), **IDEMPOTENCY}, ['proposal_id', 'confirmed']),
        lambda c, a: _post(c, f"/api/agent/proposals/{a['proposal_id']}/apply/", {k: v for k, v in a.items() if k != 'proposal_id'}, mutation=True),
    ),
    'foods_search': ToolSpec(
        'Search Tandoor foods',
        'Find existing Tandoor foods and their best nutrition profile.',
        obj({'q': string('Food name query.'), 'limit': integer('Maximum results.')}),
        lambda c, a: _get(c, '/api/agent/foods/', a),
    ),
    'nutrition_profile_create': ToolSpec(
        'Save nutrition label',
        'Persist structured nutrition facts, including user-verified package label values, for an existing Tandoor food.',
        obj({
            'food_id': integer('Tandoor food ID.'),
            'basis_amount': number('Amount the label values apply to.'),
            'basis_unit': string('Unit for the basis amount.'),
            'calories': number('Calories for the basis amount.'),
            'protein_g': number('Protein grams.'),
            'carbohydrate_g': number('Carbohydrate grams.'),
            'fat_g': number('Fat grams.'),
            'fiber_g': number('Fiber grams.'),
            'sugar_g': number('Sugar grams.'),
            'sodium_mg': number('Sodium milligrams.'),
            'grams_per_ml': number('Optional density for deterministic mass/volume conversion.'),
            'label': string('Product/label name.'),
            'brand': string('Brand.'),
            'barcode': string('Barcode/UPC.'),
            'source_type': {'type': 'string', 'enum': ['user_label', 'branded', 'reference', 'estimated', 'ai_estimate', 'manual']},
            'source_reference': string('Source/provenance reference.'),
            'confidence': number('0 to 1.'),
            'verified': boolean('Whether a human/user verified this profile.'),
            'is_default': boolean('Make this the food default.'),
            **IDEMPOTENCY,
        }, ['food_id']),
        lambda c, a: _post(c, '/api/agent/nutrition-profiles/', a, mutation=True),
    ),
    'fdc_search': ToolSpec(
        'Search USDA FoodData Central',
        'Search FDC for candidate reference/branded foods. Search does not persist anything.',
        obj({'q': string('Food query.'), 'limit': integer('Maximum candidates, up to 25.')}, ['q']),
        lambda c, a: _get(c, '/api/agent/nutrition/fdc/search/', a),
    ),
    'fdc_verify': ToolSpec(
        'Verify USDA food match',
        'Persist a specifically reviewed FDC candidate as a verified nutrition profile and save its FDC ID on the Tandoor food.',
        obj({
            'food_id': integer('Tandoor food ID.'),
            'fdc_id': integer('Reviewed USDA FDC ID.'),
            'confirmed': boolean('Must be true.'),
            'is_default': boolean('Use the imported profile as default.'),
            'force_refresh': boolean('Bypass cached FDC details.'),
            **IDEMPOTENCY,
        }, ['food_id', 'fdc_id', 'confirmed']),
        lambda c, a: _post(c, f"/api/agent/foods/{a['food_id']}/nutrition/fdc/verify/", {k: v for k, v in a.items() if k != 'food_id'}, mutation=True),
    ),
    'shopping_lists': ToolSpec(
        'List shopping lists',
        'List shopping lists in the active Tandoor space.',
        obj(),
        lambda c, a: _get(c, '/api/agent/shopping/lists/', {}),
    ),
    'shopping_list_create': ToolSpec(
        'Create shopping list',
        'Create a shopping list.',
        obj({'name': string('List name.'), 'description': string('Description.'), 'color': string('Optional hex color.'), **IDEMPOTENCY}, ['name']),
        lambda c, a: _post(c, '/api/agent/shopping/lists/', a, mutation=True),
    ),
    'shopping_entries': ToolSpec(
        'List shopping entries',
        'List open shopping entries, optionally for one list.',
        obj({'shopping_list_id': integer('Shopping list ID.'), 'include_checked': boolean('Include completed entries.')}),
        lambda c, a: _get(c, '/api/agent/shopping/entries/', a),
    ),
    'shopping_entry_add': ToolSpec(
        'Add shopping entry',
        'Add a food/amount to one or more shopping lists.',
        obj({
            'food_id': integer('Tandoor food ID.'),
            'amount': number('Amount to buy.'),
            'unit_id': integer('Optional unit ID.'),
            'shopping_list_ids': {'type': 'array', 'items': {'type': 'integer'}},
            **IDEMPOTENCY,
        }, ['food_id']),
        lambda c, a: _post(c, '/api/agent/shopping/entries/', a, mutation=True),
    ),
    'shopping_entry_update': ToolSpec(
        'Update shopping entry',
        'Update quantity, unit, checked state, or list membership with optimistic concurrency.',
        obj({
            'entry_id': integer('Shopping entry ID.'),
            'expected_updated_at': string('Revision from shopping_entries.'),
            'amount': number('New amount.'),
            'unit_id': integer('New unit ID.'),
            'checked': boolean('Completed state.'),
            'shopping_list_ids': {'type': 'array', 'items': {'type': 'integer'}},
            **IDEMPOTENCY,
        }, ['entry_id', 'expected_updated_at']),
        lambda c, a: _patch(c, f"/api/agent/shopping/entries/{a['entry_id']}/", {k: v for k, v in a.items() if k != 'entry_id'}),
    ),
    'meal_types': ToolSpec(
        'List meal types',
        'List configured meal types and default times.',
        obj(),
        lambda c, a: _get(c, '/api/agent/meal-types/', {}),
    ),
    'meal_plans': ToolSpec(
        'List meal plans',
        'List meal plans, optionally bounded by from/to ISO dates.',
        obj({'from': string('Earliest overlap date/datetime.'), 'to': string('Latest overlap date/datetime.')}),
        lambda c, a: _get(c, '/api/agent/meal-plans/', a),
    ),
    'meal_plan_add': ToolSpec(
        'Add meal plan',
        'Schedule a recipe or titled meal in Tandoor.',
        obj({
            'recipe_id': integer('Optional Tandoor recipe ID.'),
            'meal_type_id': integer('Meal type ID.'),
            'servings': number('Planned servings.'),
            'title': string('Optional title.'),
            'note': string('Optional note.'),
            'from_date': string('ISO date or datetime.'),
            'to_date': string('Optional ISO date or datetime.'),
            **IDEMPOTENCY,
        }, ['meal_type_id', 'from_date']),
        lambda c, a: _post(c, '/api/agent/meal-plans/', a, mutation=True),
    ),
    'meal_plan_delete': ToolSpec(
        'Delete meal plan',
        'Delete a meal plan after explicit confirmation.',
        obj({'meal_plan_id': integer('Meal plan ID.'), 'confirmed': boolean('Must be true.'), **IDEMPOTENCY}, ['meal_plan_id', 'confirmed']),
        lambda c, a: _delete(c, f"/api/agent/meal-plans/{a['meal_plan_id']}/", {k: v for k, v in a.items() if k != 'meal_plan_id'}),
    ),
}


async def handle_list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, title=spec.title, description=spec.description, input_schema=spec.schema)
        for name, spec in TOOLS.items()
    ])


async def handle_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    spec = TOOLS.get(params.name)
    if spec is None:
        raise ValueError(f'Unknown tool: {params.name}')
    client = TandoorAgentClient()
    try:
        payload = await spec.invoke(client, dict(params.arguments or {}))
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return types.CallToolResult(content=[types.TextContent(type='text', text=text)])
    except TandoorAgentClientError as exc:
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type='text', text=str(exc))],
        )


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
    parser = argparse.ArgumentParser(description='Thin MCP adapter for the Tandoor Agent API.')
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-unauthenticated-http', action='store_true')
    args = parser.parse_args(argv)

    # Fail at startup instead of after the first tool call.
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
