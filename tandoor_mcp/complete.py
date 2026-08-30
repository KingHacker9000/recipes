from tandoor_mcp import server as _server


_server.TOOLS['meal_plan_update'] = _server.ToolSpec(
    'Update meal plan',
    'Update an accessible meal plan using its optimistic updated_at revision.',
    'PATCH',
    '/api/agent/meal-plans/{meal_plan_id}/',
    _server.obj({
        'meal_plan_id': _server.integer('Meal plan ID.'),
        'expected_updated_at': _server.string('Revision returned by meal_plans.'),
        'recipe_id': _server.integer('Optional accessible recipe ID; omit to leave unchanged.'),
        'meal_type_id': _server.integer('Optional meal type ID.'),
        'servings': _server.number('Optional planned servings.'),
        'title': _server.string('Optional title.'),
        'note': _server.string('Optional note.'),
        'from_date': _server.string('Optional ISO date/datetime.'),
        'to_date': _server.string('Optional ISO date/datetime.'),
        **_server.IDEMPOTENCY,
    }, ['meal_plan_id', 'expected_updated_at']),
    True,
    ('meal_plan_id',),
)


TOOLS = _server.TOOLS
ToolSpec = _server.ToolSpec
build_server = _server.build_server
handle_call_tool = _server.handle_call_tool
handle_list_tools = _server.handle_list_tools
invoke_tool = _server.invoke_tool
main = _server.main
