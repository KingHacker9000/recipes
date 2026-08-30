from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from rest_framework.exceptions import ValidationError

from cookbook.agent_api.models import RecipeVariantLink
from cookbook.agent_api.nutrition import ALL_FIELDS, evaluate_draft
from cookbook.models import Food, Recipe, Unit
from cookbook.serializer import RecipeSerializer


RECIPE_FIELDS = (
    'name', 'description', 'working_time', 'waiting_time', 'source_url',
    'internal', 'show_ingredient_overview', 'servings', 'servings_text',
    'diameter', 'diameter_text', 'private',
)
STEP_FIELDS = (
    'name', 'instruction', 'time', 'order', 'show_as_header',
    'show_ingredients_table',
)
INGREDIENT_FIELDS = (
    'amount', 'note', 'order', 'is_header', 'no_amount', 'original_text',
)

MACRO_CONSTRAINTS = {
    'calories_max': ('calories', 'max'),
    'calories_min': ('calories', 'min'),
    'protein_min_g': ('protein_g', 'min'),
    'protein_max_g': ('protein_g', 'max'),
    'carbohydrate_max_g': ('carbohydrate_g', 'max'),
    'carbohydrate_min_g': ('carbohydrate_g', 'min'),
    'fat_max_g': ('fat_g', 'max'),
    'fat_min_g': ('fat_g', 'min'),
    'fiber_min_g': ('fiber_g', 'min'),
    'fiber_max_g': ('fiber_g', 'max'),
}


class AgentRecipeInputError(ValueError):
    pass


def _decimal(value, field):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AgentRecipeInputError(f'{field} must be numeric.')


def _food_ref(item, space):
    food_id = item.get('food_id')
    food_name = str(item.get('food_name') or '').strip()
    if food_id not in (None, ''):
        food = Food.objects.filter(pk=food_id, space=space).first()
        if food is None:
            raise AgentRecipeInputError(f'Food {food_id} was not found in the active space.')
        return {'id': food.id, 'name': food.name}
    if food_name:
        existing = Food.objects.filter(name__iexact=food_name, space=space).first()
        if existing:
            return {'id': existing.id, 'name': existing.name}
        return {'name': food_name[:512]}
    return None


def _unit_ref(item, space):
    unit_id = item.get('unit_id')
    unit_name = str(item.get('unit_name') or item.get('unit') or '').strip()
    if unit_id not in (None, ''):
        unit = Unit.objects.filter(pk=unit_id, space=space).first()
        if unit is None:
            raise AgentRecipeInputError(f'Unit {unit_id} was not found in the active space.')
        return {'id': unit.id, 'name': unit.name}
    if unit_name:
        existing = Unit.objects.filter(name__iexact=unit_name, space=space).first()
        if existing:
            return {'id': existing.id, 'name': existing.name}
        return {'name': unit_name[:64]}
    return None


def _existing_nested_ids(recipe):
    step_ids = set(recipe.steps.values_list('id', flat=True)) if recipe else set()
    ingredient_ids = set()
    if recipe:
        for step in recipe.steps.all():
            ingredient_ids.update(step.ingredients.values_list('id', flat=True))
    return step_ids, ingredient_ids


def normalize_recipe_input(payload, space, instance=None, partial=False):
    """Convert a small agent-friendly recipe schema into Tandoor's native serializer schema.

    The agent never gets a generic nested-model escape hatch. References are
    resolved inside the active space before the native serializer sees them.
    """
    if not isinstance(payload, dict):
        raise AgentRecipeInputError('recipe must be an object.')

    data = {}
    for field in RECIPE_FIELDS:
        if field in payload:
            data[field] = payload[field]

    if not partial and not str(data.get('name') or '').strip():
        raise AgentRecipeInputError('name is required.')
    if 'name' in data:
        data['name'] = str(data['name']).strip()[:512]
        if not data['name']:
            raise AgentRecipeInputError('name cannot be blank.')

    if 'servings' in data:
        servings = _decimal(data['servings'], 'servings')
        if servings is None or servings <= 0:
            raise AgentRecipeInputError('servings must be greater than zero.')
        data['servings'] = servings

    if 'keywords' in payload:
        keywords = payload.get('keywords') or []
        if not isinstance(keywords, list) or len(keywords) > 100:
            raise AgentRecipeInputError('keywords must be a list with at most 100 entries.')
        normalized_keywords = []
        for keyword in keywords:
            name = str(keyword.get('name') if isinstance(keyword, dict) else keyword).strip()
            if name:
                normalized_keywords.append({'name': name[:128]})
        data['keywords'] = normalized_keywords

    if 'steps' not in payload:
        if not partial:
            raise AgentRecipeInputError('steps is required.')
        return data

    steps = payload.get('steps')
    if not isinstance(steps, list) or len(steps) > 100:
        raise AgentRecipeInputError('steps must be a list with at most 100 entries.')

    current_step_ids, current_ingredient_ids = _existing_nested_ids(instance)
    normalized_steps = []
    total_ingredients = 0
    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise AgentRecipeInputError(f'steps[{step_index}] must be an object.')
        normalized_step = {field: step[field] for field in STEP_FIELDS if field in step}
        if instance and step.get('id') not in (None, ''):
            step_id = int(step['id'])
            if step_id not in current_step_ids:
                raise AgentRecipeInputError(f'steps[{step_index}].id does not belong to this recipe.')
            normalized_step['id'] = step_id
        elif not instance and step.get('id') not in (None, ''):
            raise AgentRecipeInputError('New recipes cannot reference existing step IDs.')

        ingredients = step.get('ingredients', [])
        if not isinstance(ingredients, list) or len(ingredients) > 500:
            raise AgentRecipeInputError(f'steps[{step_index}].ingredients must contain at most 500 entries.')
        total_ingredients += len(ingredients)
        if total_ingredients > 1000:
            raise AgentRecipeInputError('A recipe can contain at most 1000 ingredients through the Agent API.')

        normalized_ingredients = []
        for ingredient_index, item in enumerate(ingredients):
            if not isinstance(item, dict):
                raise AgentRecipeInputError(f'steps[{step_index}].ingredients[{ingredient_index}] must be an object.')
            normalized_item = {field: item[field] for field in INGREDIENT_FIELDS if field in item}
            if instance and item.get('id') not in (None, ''):
                ingredient_id = int(item['id'])
                if ingredient_id not in current_ingredient_ids:
                    raise AgentRecipeInputError(
                        f'steps[{step_index}].ingredients[{ingredient_index}].id does not belong to this recipe.'
                    )
                normalized_item['id'] = ingredient_id
            elif not instance and item.get('id') not in (None, ''):
                raise AgentRecipeInputError('New recipes cannot reference existing ingredient IDs.')

            if 'amount' in item:
                amount = _decimal(item.get('amount'), f'steps[{step_index}].ingredients[{ingredient_index}].amount')
                if amount is not None and amount < 0:
                    raise AgentRecipeInputError('Ingredient amount cannot be negative.')
                normalized_item['amount'] = amount if amount is not None else Decimal('0')
            else:
                normalized_item['amount'] = Decimal('0')

            normalized_item['food'] = _food_ref(item, space)
            normalized_item['unit'] = _unit_ref(item, space)
            normalized_ingredients.append(normalized_item)

        normalized_step['ingredients'] = normalized_ingredients
        normalized_steps.append(normalized_step)

    data['steps'] = normalized_steps
    return data


def recipe_to_agent_input(recipe, *, name=None):
    """Create a safe semantic payload for cloning an existing recipe."""
    payload = {
        'name': name or recipe.name,
        'description': recipe.description or '',
        'working_time': recipe.working_time,
        'waiting_time': recipe.waiting_time,
        'source_url': recipe.source_url or '',
        'internal': recipe.internal,
        'show_ingredient_overview': recipe.show_ingredient_overview,
        'servings': recipe.servings or 1,
        'servings_text': recipe.servings_text or '',
        'diameter': recipe.diameter,
        'diameter_text': recipe.diameter_text or '',
        'private': recipe.private,
        'keywords': list(recipe.keywords.values_list('name', flat=True)),
        'steps': [],
    }
    for step in recipe.steps.all().order_by('order', 'id'):
        step_data = {
            'name': step.name,
            'instruction': step.instruction,
            'time': step.time,
            'order': step.order,
            'show_as_header': step.show_as_header,
            'show_ingredients_table': step.show_ingredients_table,
            'ingredients': [],
        }
        for ingredient in step.ingredients.all().order_by('order', 'id'):
            step_data['ingredients'].append({
                'food_id': ingredient.food_id,
                'unit_id': ingredient.unit_id,
                'amount': ingredient.amount,
                'note': ingredient.note or '',
                'order': ingredient.order,
                'is_header': ingredient.is_header,
                'no_amount': ingredient.no_amount,
                'original_text': ingredient.original_text or '',
            })
        payload['steps'].append(step_data)
    return payload


def save_recipe_from_agent(request, payload, *, instance=None, partial=False):
    normalized = normalize_recipe_input(payload, request.space, instance=instance, partial=partial)
    serializer = RecipeSerializer(
        instance=instance,
        data=normalized,
        partial=partial,
        context={'request': request},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.save()


def draft_items_from_recipe_payload(payload):
    items = []
    for step in payload.get('steps') or []:
        for ingredient in step.get('ingredients') or []:
            items.append({
                'food_id': ingredient.get('food_id'),
                'amount': ingredient.get('amount'),
                'unit': ingredient.get('unit_name') or ingredient.get('unit') or '',
                'unit_id': ingredient.get('unit_id'),
            })
    return items


def _resolve_draft_units(items, space):
    resolved = []
    for item in items:
        value = dict(item)
        if value.get('unit_id') not in (None, ''):
            unit = Unit.objects.filter(pk=value['unit_id'], space=space).first()
            if unit is None:
                raise AgentRecipeInputError(f"Unit {value['unit_id']} was not found in the active space.")
            value['unit'] = unit.name
        resolved.append(value)
    return resolved


def per_serving_from_analysis(analysis, servings):
    servings = _decimal(servings, 'servings')
    if servings is None or servings <= 0:
        raise AgentRecipeInputError('servings must be greater than zero.')
    result = {}
    for field in ALL_FIELDS:
        value = analysis['total'].get(field)
        if value is None:
            result[field] = None
        else:
            result[field] = float((Decimal(str(value)) / servings).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return result


def evaluate_macro_constraints(per_serving, coverage, constraints):
    constraints = constraints or {}
    checks = []
    all_satisfied = True
    all_verifiable = True
    for key, (field, operator) in MACRO_CONSTRAINTS.items():
        if key not in constraints:
            continue
        try:
            target = Decimal(str(constraints[key]))
        except (InvalidOperation, TypeError, ValueError):
            raise AgentRecipeInputError(f'{key} must be numeric.')
        actual = per_serving.get(field)
        field_coverage = Decimal(str((coverage.get('field_coverage') or {}).get(field, 0)))
        verifiable = actual is not None and field_coverage >= Decimal('1')
        if not verifiable:
            satisfied = None
            all_satisfied = False
            all_verifiable = False
        else:
            actual_decimal = Decimal(str(actual))
            satisfied = actual_decimal <= target if operator == 'max' else actual_decimal >= target
            if not satisfied:
                all_satisfied = False
        checks.append({
            'constraint': key,
            'field': field,
            'operator': operator,
            'target': float(target),
            'actual': actual,
            'verifiable': verifiable,
            'satisfied': satisfied,
        })

    non_macro = {key: value for key, value in constraints.items() if key not in MACRO_CONSTRAINTS}
    return {
        'checks': checks,
        'all_satisfied': all_satisfied,
        'all_verifiable': all_verifiable,
        'non_macro_constraints': non_macro,
    }


def evaluate_variant_candidate(parent_recipe, candidate, constraints, space):
    if not isinstance(candidate, dict):
        raise AgentRecipeInputError('candidate must be an object.')
    servings = candidate.get('servings', parent_recipe.servings or 1)
    items = _resolve_draft_units(draft_items_from_recipe_payload(candidate), space)
    analysis = evaluate_draft(items, space)
    per_serving = per_serving_from_analysis(analysis, servings)
    constraint_result = evaluate_macro_constraints(per_serving, analysis['coverage'], constraints)
    return {
        'parent_recipe_id': parent_recipe.id,
        'candidate_name': str(candidate.get('name') or f'{parent_recipe.name} variant'),
        'servings': float(Decimal(str(servings))),
        'nutrition': {
            **analysis,
            'per_serving': per_serving,
        },
        'constraints': constraint_result,
    }


def save_variant_from_agent(request, parent_recipe, candidate, *, constraints=None, variant_type='custom', change_summary=None):
    preview = evaluate_variant_candidate(parent_recipe, candidate, constraints or {}, request.space)
    if preview['constraints']['checks'] and not preview['constraints']['all_satisfied']:
        raise AgentRecipeInputError('Candidate does not satisfy all verifiable macro constraints; save was blocked.')
    if preview['constraints']['checks'] and not preview['constraints']['all_verifiable']:
        raise AgentRecipeInputError('Candidate macro constraints cannot be verified with current nutrition coverage; save was blocked.')

    with transaction.atomic():
        recipe = save_recipe_from_agent(request, candidate, instance=None, partial=False)
        original_analysis = None
        try:
            from cookbook.agent_api.nutrition import analyze_recipe
            original_analysis = analyze_recipe(parent_recipe, request.space)
        except Exception:
            original_analysis = {}
        link = RecipeVariantLink.objects.create(
            recipe=recipe,
            parent_recipe=parent_recipe,
            variant_type=str(variant_type or 'custom')[:128],
            constraints=deepcopy(constraints or {}),
            change_summary=deepcopy(change_summary or []),
            original_macros=(original_analysis or {}).get('per_serving', {}),
            variant_macros=preview['nutrition']['per_serving'],
            created_by=request.user,
            space=request.space,
        )
    return recipe, link, preview
