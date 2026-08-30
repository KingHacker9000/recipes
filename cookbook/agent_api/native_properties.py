from decimal import Decimal, ROUND_HALF_UP

from cookbook.agent_api.nutrition import ALL_FIELDS, analyze_recipe
from cookbook.models import Property, PropertyType, Recipe


MANAGED_PROPERTY_PREFIX = 'agent-recipe-nutrition:'

NATIVE_NUTRITION_FIELDS = {
    'calories': {
        'name': 'Calories',
        'unit': 'kcal',
        'slug': 'agent-nutrition-calories',
        'order': 10,
    },
    'protein_g': {
        'name': 'Protein',
        'unit': 'g',
        'slug': 'agent-nutrition-protein',
        'order': 20,
    },
    'carbohydrate_g': {
        'name': 'Carbohydrates',
        'unit': 'g',
        'slug': 'agent-nutrition-carbohydrates',
        'order': 30,
    },
    'fat_g': {
        'name': 'Fat',
        'unit': 'g',
        'slug': 'agent-nutrition-fat',
        'order': 40,
    },
    'fiber_g': {
        'name': 'Fiber',
        'unit': 'g',
        'slug': 'agent-nutrition-fiber',
        'order': 50,
    },
    'sugar_g': {
        'name': 'Sugar',
        'unit': 'g',
        'slug': 'agent-nutrition-sugar',
        'order': 60,
    },
    'sodium_mg': {
        'name': 'Sodium',
        'unit': 'mg',
        'slug': 'agent-nutrition-sodium',
        'order': 70,
    },
}


def _marker(recipe):
    return f'{MANAGED_PROPERTY_PREFIX}{recipe.id}'


def _amount(value):
    if value is None:
        return None
    # Property.property_amount is DecimalField(..., decimal_places=3).
    return Decimal(str(value)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def _property_type(space, spec):
    """Resolve a native property type without taking ownership of user data.

    We prefer the stable Agent slug. If a user already has the conventional
    property name, reuse it rather than creating a duplicate. The individual
    Property row carries the Agent marker, so the bridge can still safely
    distinguish its own values from manually maintained properties.
    """
    obj = PropertyType.objects.filter(space=space, open_data_slug=spec['slug']).first()
    if obj is not None:
        return obj

    obj = PropertyType.objects.filter(space=space, name__iexact=spec['name']).first()
    if obj is not None:
        return obj

    return PropertyType.objects.create(
        name=spec['name'],
        unit=spec['unit'],
        description='Per-serving deterministic nutrition synced by the Tandoor Agent API.',
        order=spec['order'],
        category=PropertyType.NUTRITION,
        open_data_slug=spec['slug'],
        space=space,
    )


def sync_recipe_native_nutrition_properties(recipe, space=None):
    """Mirror fully-verifiable Agent nutrition into Recipe.properties.

    Only fields with complete field-level coverage are surfaced. Agent-created
    Property rows are tagged with a recipe-specific marker so stale bridge data
    can be removed without touching unrelated/manual recipe properties.
    """
    space = space or recipe.space
    analysis = analyze_recipe(recipe, space)
    marker = _marker(recipe)
    field_coverage = (analysis.get('coverage') or {}).get('field_coverage') or {}
    per_serving = analysis.get('per_serving') or {}

    kept_property_ids = set()
    synced = {}

    for field in ALL_FIELDS:
        spec = NATIVE_NUTRITION_FIELDS[field]
        value = per_serving.get(field)
        coverage = Decimal(str(field_coverage.get(field) or 0))
        if value is None or coverage < Decimal('1'):
            continue

        property_type = _property_type(space, spec)
        property_amount = _amount(value)
        prop, created = Property.objects.get_or_create(
            space=space,
            property_type=property_type,
            open_data_food_slug=marker,
            defaults={'property_amount': property_amount},
        )
        if not created and prop.property_amount != property_amount:
            prop.property_amount = property_amount
            prop.save(update_fields=['property_amount'])
        recipe.properties.add(prop)
        kept_property_ids.add(prop.id)
        synced[field] = float(property_amount)

    stale = Property.objects.filter(space=space, open_data_food_slug=marker)
    if kept_property_ids:
        stale = stale.exclude(pk__in=kept_property_ids)
    stale_ids = list(stale.values_list('id', flat=True))
    if stale_ids:
        recipe.properties.remove(*stale_ids)
        Property.objects.filter(pk__in=stale_ids).delete()

    return {
        'recipe_id': recipe.id,
        'synced': synced,
        'coverage': analysis.get('coverage') or {},
    }


def sync_recipes_for_food(food, space=None):
    """Refresh native recipe properties after a food nutrition profile changes."""
    space = space or food.space
    recipes = (Recipe.objects
               .filter(space=space, steps__ingredients__food=food)
               .distinct()
               .prefetch_related('steps__ingredients__food', 'steps__ingredients__unit'))
    results = []
    for recipe in recipes:
        results.append(sync_recipe_native_nutrition_properties(recipe, space))
    return results
