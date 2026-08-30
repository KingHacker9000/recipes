from tandoor_mcp import server as _server


_server.TOOLS['meal_plan_update'] = _server.ToolSpec(
    'Update meal plan',
    'Update an accessible meal plan using the deterministic revision returned by meal_plans.',
    'PATCH',
    '/api/agent/meal-plans/{meal_plan_id}/',
    _server.obj({
        'meal_plan_id': _server.integer('Meal plan ID.'),
        'expected_revision': _server.string('Revision returned by meal_plans.'),
        'recipe_id': _server.integer('Optional accessible recipe ID; omit to leave unchanged.'),
        'meal_type_id': _server.integer('Optional meal type ID.'),
        'servings': _server.number('Optional planned servings.'),
        'title': _server.string('Optional title.'),
        'note': _server.string('Optional note.'),
        'from_date': _server.string('Optional ISO date/datetime.'),
        'to_date': _server.string('Optional ISO date/datetime.'),
        **_server.IDEMPOTENCY,
    }, ['meal_plan_id', 'expected_revision']),
    True,
    ('meal_plan_id',),
)

_server.TOOLS['recipe_image_get'] = _server.ToolSpec(
    'Get recipe image',
    'Read the current native Tandoor recipe image and optimistic recipe revision.',
    'GET',
    '/api/agent/recipes/{recipe_id}/image/',
    _server.obj({
        'recipe_id': _server.integer('Recipe ID.'),
    }, ['recipe_id']),
    path_args=('recipe_id',),
)

_server.TOOLS['recipe_image_upload'] = _server.ToolSpec(
    'Upload recipe image',
    'Set a native Tandoor recipe image from validated base64 JPEG, PNG or WEBP data. Maximum decoded size is 8 MiB.',
    'POST',
    '/api/agent/recipes/{recipe_id}/image/',
    _server.obj({
        'recipe_id': _server.integer('Recipe ID.'),
        'expected_updated_at': _server.string('Current recipe revision returned by recipe_get or recipe_image_get.'),
        'image_base64': _server.string('Base64 image bytes or a base64 data URI. JPEG, PNG and WEBP only.'),
        'content_type': _server.string('Optional MIME type matching the decoded image, e.g. image/jpeg.'),
        **_server.IDEMPOTENCY,
    }, ['recipe_id', 'expected_updated_at', 'image_base64']),
    True,
    ('recipe_id',),
)


TOOLS = _server.TOOLS
ToolSpec = _server.ToolSpec
build_server = _server.build_server
handle_call_tool = _server.handle_call_tool
handle_list_tools = _server.handle_list_tools
invoke_tool = _server.invoke_tool
main = _server.main
