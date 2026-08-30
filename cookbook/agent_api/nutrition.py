from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from cookbook.agent_api.models import FoodNutritionProfile
from cookbook.models import Food


CORE_FIELDS = ('calories', 'protein_g', 'carbohydrate_g', 'fat_g')
ALL_FIELDS = CORE_FIELDS + ('fiber_g', 'sugar_g', 'sodium_mg')

# Canonical amount is grams for mass and millilitres for volume.
UNIT_ALIASES = {
    'mg': ('mass', Decimal('0.001')),
    'milligram': ('mass', Decimal('0.001')),
    'milligrams': ('mass', Decimal('0.001')),
    'g': ('mass', Decimal('1')),
    'gram': ('mass', Decimal('1')),
    'grams': ('mass', Decimal('1')),
    'kg': ('mass', Decimal('1000')),
    'kilogram': ('mass', Decimal('1000')),
    'kilograms': ('mass', Decimal('1000')),
    'oz': ('mass', Decimal('28.349523125')),
    'ounce': ('mass', Decimal('28.349523125')),
    'ounces': ('mass', Decimal('28.349523125')),
    'lb': ('mass', Decimal('453.59237')),
    'lbs': ('mass', Decimal('453.59237')),
    'pound': ('mass', Decimal('453.59237')),
    'pounds': ('mass', Decimal('453.59237')),
    'ml': ('volume', Decimal('1')),
    'milliliter': ('volume', Decimal('1')),
    'milliliters': ('volume', Decimal('1')),
    'millilitre': ('volume', Decimal('1')),
    'millilitres': ('volume', Decimal('1')),
    'l': ('volume', Decimal('1000')),
    'liter': ('volume', Decimal('1000')),
    'liters': ('volume', Decimal('1000')),
    'litre': ('volume', Decimal('1000')),
    'litres': ('volume', Decimal('1000')),
    'tsp': ('volume', Decimal('4.92892159375')),
    'teaspoon': ('volume', Decimal('4.92892159375')),
    'teaspoons': ('volume', Decimal('4.92892159375')),
    'tbsp': ('volume', Decimal('14.78676478125')),
    'tablespoon': ('volume', Decimal('14.78676478125')),
    'tablespoons': ('volume', Decimal('14.78676478125')),
    'cup': ('volume', Decimal('240')),
    'cups': ('volume', Decimal('240')),
}

COUNT_ALIASES = {
    '': 'each',
    'each': 'each',
    'ea': 'each',
    'item': 'each',
    'items': 'each',
    'piece': 'each',
    'pieces': 'each',
    'whole': 'each',
}


def as_decimal(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize_unit_name(value):
    value = str(value or '').strip().lower().replace('.', '')
    value = ' '.join(value.split())
    return COUNT_ALIASES.get(value, value)


def unit_descriptor(value):
    normalized = normalize_unit_name(value)
    if normalized == 'each':
        return {'name': 'each', 'dimension': 'count', 'factor': Decimal('1')}
    if normalized in UNIT_ALIASES:
        dimension, factor = UNIT_ALIASES[normalized]
        return {'name': normalized, 'dimension': dimension, 'factor': factor}
    return {'name': normalized, 'dimension': 'custom', 'factor': Decimal('1')}


def ratio_for_profile(amount, unit_name, profile):
    """Return number of nutrition-profile basis portions represented by amount."""
    amount = as_decimal(amount)
    basis_amount = as_decimal(profile.basis_amount)
    if amount is None or amount <= 0:
        return None, 'missing_amount'
    if basis_amount is None or basis_amount <= 0:
        return None, 'invalid_profile_basis'

    ingredient_unit = unit_descriptor(unit_name)
    profile_unit = unit_descriptor(profile.basis_unit)

    if ingredient_unit['dimension'] == profile_unit['dimension']:
        if ingredient_unit['dimension'] == 'custom' and ingredient_unit['name'] != profile_unit['name']:
            return None, 'unit_mismatch'
        canonical_amount = amount * ingredient_unit['factor']
        canonical_basis = basis_amount * profile_unit['factor']
        return canonical_amount / canonical_basis, ''

    density = as_decimal(profile.grams_per_ml)
    if density is not None and density > 0:
        if ingredient_unit['dimension'] == 'volume' and profile_unit['dimension'] == 'mass':
            ml = amount * ingredient_unit['factor']
            grams = ml * density
            basis_grams = basis_amount * profile_unit['factor']
            return grams / basis_grams, ''
        if ingredient_unit['dimension'] == 'mass' and profile_unit['dimension'] == 'volume':
            grams = amount * ingredient_unit['factor']
            ml = grams / density
            basis_ml = basis_amount * profile_unit['factor']
            return ml / basis_ml, ''

    return None, 'unit_mismatch'


def profile_dict(profile):
    if not profile:
        return None
    return {
        'id': profile.id,
        'food_id': profile.food_id,
        'label': profile.label,
        'brand': profile.brand,
        'barcode': profile.barcode,
        'basis_amount': float(profile.basis_amount),
        'basis_unit': profile.basis_unit,
        'source_type': profile.source_type,
        'source_reference': profile.source_reference,
        'confidence': float(profile.confidence),
        'verified': profile.verified,
        'is_default': profile.is_default,
    }


def _ordered_profiles(food, space):
    return (FoodNutritionProfile.objects
            .filter(food=food, space=space)
            .order_by('-is_default', '-verified', '-confidence', '-updated_at'))


def calculate_food_amount(food, amount, unit_name, space):
    """Calculate nutrient contribution for a single food amount.

    A profile is only selected if its basis can be deterministically converted
    to the ingredient unit. This prevents a high-confidence but incompatible
    profile (for example 100 g) from being silently applied to an unknown
    'scoop' amount.
    """
    failures = []
    for profile in _ordered_profiles(food, space):
        ratio, reason = ratio_for_profile(amount, unit_name, profile)
        if ratio is None:
            failures.append({'profile_id': profile.id, 'reason': reason})
            continue

        nutrients = {}
        for field in ALL_FIELDS:
            value = getattr(profile, field)
            nutrients[field] = None if value is None else value * ratio
        return {
            'matched': True,
            'ratio': ratio,
            'profile': profile,
            'nutrients': nutrients,
            'reason': '',
            'profile_failures': failures,
        }

    return {
        'matched': False,
        'ratio': None,
        'profile': None,
        'nutrients': {field: None for field in ALL_FIELDS},
        'reason': 'no_compatible_nutrition_profile' if failures else 'no_nutrition_profile',
        'profile_failures': failures,
    }


def _round(value, places='0.01'):
    if value is None:
        return None
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _serialize_nutrients(values):
    return {field: _round(values.get(field)) for field in ALL_FIELDS}


def _recipe_ingredients(recipe):
    seen = set()
    result = []
    for step in recipe.steps.all().order_by('order', 'id'):
        for ingredient in step.ingredients.all().order_by('id'):
            if ingredient.id in seen:
                continue
            seen.add(ingredient.id)
            result.append(ingredient)
    return result


def analyze_recipe(recipe, space):
    ingredients = _recipe_ingredients(recipe)
    totals = {field: Decimal('0') for field in ALL_FIELDS}
    field_matches = {field: 0 for field in ALL_FIELDS}
    matched_profiles = 0
    confidence_sum = Decimal('0')
    details = []
    warnings = []

    considered = 0
    for ingredient in ingredients:
        amount = as_decimal(ingredient.amount)
        if not ingredient.food_id or amount is None or amount <= 0:
            details.append({
                'ingredient_id': ingredient.id,
                'food_id': ingredient.food_id,
                'food': ingredient.food.name if ingredient.food_id else '',
                'amount': _round(amount) if amount is not None else None,
                'unit': ingredient.unit.name if ingredient.unit_id else '',
                'matched': False,
                'reason': 'missing_food_or_amount',
                'profile': None,
                'nutrients': {field: None for field in ALL_FIELDS},
            })
            continue

        considered += 1
        unit_name = ingredient.unit.name if ingredient.unit_id else ''
        calc = calculate_food_amount(ingredient.food, amount, unit_name, space)
        if calc['matched']:
            matched_profiles += 1
            confidence_sum += calc['profile'].confidence
            for field, value in calc['nutrients'].items():
                if value is not None:
                    totals[field] += value
                    field_matches[field] += 1
        else:
            warnings.append({
                'ingredient_id': ingredient.id,
                'food': ingredient.food.name,
                'reason': calc['reason'],
            })

        details.append({
            'ingredient_id': ingredient.id,
            'food_id': ingredient.food_id,
            'food': ingredient.food.name,
            'amount': _round(amount),
            'unit': unit_name,
            'note': ingredient.note or '',
            'matched': calc['matched'],
            'reason': calc['reason'],
            'profile': profile_dict(calc['profile']),
            'nutrients': _serialize_nutrients(calc['nutrients']),
        })

    servings = recipe.servings if recipe.servings and recipe.servings > 0 else 1
    servings_decimal = Decimal(str(servings))
    per_serving = {field: totals[field] / servings_decimal for field in ALL_FIELDS}
    ingredient_coverage = Decimal(matched_profiles) / Decimal(considered) if considered else Decimal('0')
    confidence = confidence_sum / Decimal(matched_profiles) if matched_profiles else Decimal('0')

    field_coverage = {
        field: _round(Decimal(field_matches[field]) / Decimal(considered), '0.0001') if considered else 0.0
        for field in ALL_FIELDS
    }

    return {
        'recipe_id': recipe.id,
        'recipe_name': recipe.name,
        'servings': servings,
        'total': _serialize_nutrients(totals),
        'per_serving': _serialize_nutrients(per_serving),
        'coverage': {
            'ingredients_considered': considered,
            'ingredients_matched': matched_profiles,
            'ingredient_coverage': _round(ingredient_coverage, '0.0001'),
            'field_coverage': field_coverage,
            'confidence': _round(confidence, '0.0001'),
            'complete_core_macros': all(field_matches[field] == considered and considered > 0 for field in CORE_FIELDS),
        },
        'ingredients': details,
        'warnings': warnings,
    }


def scale_nutrition_analysis(analysis, factor, target_servings):
    factor = as_decimal(factor)
    total = {}
    for field, value in analysis['total'].items():
        total[field] = None if value is None else _round(as_decimal(value) * factor)

    target = as_decimal(target_servings)
    per_serving = {}
    for field, value in total.items():
        if value is None or target is None or target <= 0:
            per_serving[field] = None
        else:
            per_serving[field] = _round(as_decimal(value) / target)

    scaled = dict(analysis)
    scaled['servings'] = float(target) if target is not None else target_servings
    scaled['total'] = total
    scaled['per_serving'] = per_serving
    return scaled


def scale_recipe_preview(recipe, target_servings, space):
    target = as_decimal(target_servings)
    if target is None or target <= 0:
        raise ValueError('target_servings must be greater than zero.')
    current = Decimal(str(recipe.servings if recipe.servings and recipe.servings > 0 else 1))
    factor = target / current

    scaled_ingredients = []
    for ingredient in _recipe_ingredients(recipe):
        amount = as_decimal(ingredient.amount)
        scaled_ingredients.append({
            'ingredient_id': ingredient.id,
            'food_id': ingredient.food_id,
            'food': ingredient.food.name if ingredient.food_id else '',
            'original_amount': _round(amount) if amount is not None else None,
            'scaled_amount': _round(amount * factor) if amount is not None else None,
            'unit': ingredient.unit.name if ingredient.unit_id else '',
            'note': ingredient.note or '',
        })

    analysis = analyze_recipe(recipe, space)
    return {
        'recipe_id': recipe.id,
        'recipe_name': recipe.name,
        'mode': 'exact',
        'current_servings': float(current),
        'target_servings': float(target),
        'scale_factor': _round(factor, '0.000001'),
        'ingredients': scaled_ingredients,
        'nutrition': scale_nutrition_analysis(analysis, factor, target),
        'note': 'Exact scaling only. Practical culinary adjustments are intentionally left to the agent before a variant is saved.',
    }


def evaluate_draft(items, space):
    """Evaluate an unsaved ingredient draft generated by an agent."""
    totals = {field: Decimal('0') for field in ALL_FIELDS}
    field_matches = {field: 0 for field in ALL_FIELDS}
    details = []
    considered = 0
    matched = 0
    confidence_sum = Decimal('0')

    for index, item in enumerate(items or []):
        food_id = item.get('food_id')
        amount = as_decimal(item.get('amount'))
        unit_name = str(item.get('unit') or '')
        food = Food.objects.filter(pk=food_id, space=space).first() if food_id else None
        if food is None or amount is None or amount <= 0:
            details.append({
                'index': index,
                'food_id': food_id,
                'amount': _round(amount) if amount is not None else None,
                'unit': unit_name,
                'matched': False,
                'reason': 'food_not_found_or_invalid_amount',
            })
            continue

        considered += 1
        calc = calculate_food_amount(food, amount, unit_name, space)
        if calc['matched']:
            matched += 1
            confidence_sum += calc['profile'].confidence
            for field, value in calc['nutrients'].items():
                if value is not None:
                    totals[field] += value
                    field_matches[field] += 1

        details.append({
            'index': index,
            'food_id': food.id,
            'food': food.name,
            'amount': _round(amount),
            'unit': unit_name,
            'matched': calc['matched'],
            'reason': calc['reason'],
            'profile': profile_dict(calc['profile']),
            'nutrients': _serialize_nutrients(calc['nutrients']),
        })

    coverage = Decimal(matched) / Decimal(considered) if considered else Decimal('0')
    confidence = confidence_sum / Decimal(matched) if matched else Decimal('0')
    return {
        'total': _serialize_nutrients(totals),
        'coverage': {
            'ingredients_considered': considered,
            'ingredients_matched': matched,
            'ingredient_coverage': _round(coverage, '0.0001'),
            'confidence': _round(confidence, '0.0001'),
            'field_coverage': {
                field: _round(Decimal(field_matches[field]) / Decimal(considered), '0.0001') if considered else 0.0
                for field in ALL_FIELDS
            },
            'complete_core_macros': all(field_matches[field] == considered and considered > 0 for field in CORE_FIELDS),
        },
        'ingredients': details,
    }
