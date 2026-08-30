import hashlib
import json
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from cookbook.models import Food, MealPlan, MealType, Recipe, ShoppingList, ShoppingListEntry, Unit


class AgentHouseholdInputError(ValueError):
    pass


def _decimal(value, field, *, minimum=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AgentHouseholdInputError(f'{field} must be numeric.')
    if minimum is not None and result < Decimal(str(minimum)):
        raise AgentHouseholdInputError(f'{field} must be at least {minimum}.')
    return result


def accessible_recipe_queryset(request):
    return (Recipe.objects
            .filter(space=request.space)
            .filter(Q(private=False) | Q(created_by=request.user) | Q(shared=request.user))
            .distinct())


def accessible_meal_plan_queryset(request):
    return (MealPlan.objects
            .filter(space=request.space)
            .filter(Q(recipe__isnull=True) | Q(recipe__private=False) | Q(recipe__created_by=request.user) | Q(recipe__shared=request.user))
            .distinct())


def shopping_list_payload(obj):
    return {
        'id': obj.id,
        'name': obj.name,
        'description': obj.description or '',
        'color': obj.color,
        'created_at': obj.created_at.isoformat() if obj.created_at else None,
        'updated_at': obj.updated_at.isoformat() if obj.updated_at else None,
    }


def shopping_entry_payload(obj):
    return {
        'id': obj.id,
        'food_id': obj.food_id,
        'food': obj.food.name,
        'amount': float(obj.amount),
        'unit_id': obj.unit_id,
        'unit': obj.unit.name if obj.unit_id else '',
        'checked': obj.checked,
        'shopping_list_ids': list(obj.shopping_lists.values_list('id', flat=True)),
        'created_by_id': obj.created_by_id,
        'created_at': obj.created_at.isoformat() if obj.created_at else None,
        'updated_at': obj.updated_at.isoformat() if obj.updated_at else None,
    }


def meal_type_payload(obj):
    return {
        'id': obj.id,
        'name': obj.name,
        'order': obj.order,
        'color': obj.color,
        'time': obj.time.isoformat() if obj.time else None,
        'default': obj.default,
    }


def meal_plan_revision(obj):
    canonical = {
        'id': obj.id,
        'recipe_id': obj.recipe_id,
        'servings': str(obj.servings),
        'title': obj.title or '',
        'meal_type_id': obj.meal_type_id,
        'note': obj.note or '',
        'from_date': obj.from_date.isoformat(),
        'to_date': obj.to_date.isoformat(),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def meal_plan_payload(obj):
    return {
        'id': obj.id,
        'recipe_id': obj.recipe_id,
        'recipe': obj.recipe.name if obj.recipe_id else '',
        'servings': float(obj.servings),
        'title': obj.title or '',
        'meal_type_id': obj.meal_type_id,
        'meal_type': obj.meal_type.name,
        'note': obj.note or '',
        'from_date': obj.from_date.isoformat(),
        'to_date': obj.to_date.isoformat(),
        'created_by_id': obj.created_by_id,
        'revision': meal_plan_revision(obj),
    }


def _unit(unit_id, space):
    if unit_id in (None, ''):
        return None
    obj = Unit.objects.filter(pk=unit_id, space=space).first()
    if obj is None:
        raise AgentHouseholdInputError('Unit not found in the active space.')
    return obj


def _shopping_lists(ids, space):
    if ids is None:
        return []
    if not isinstance(ids, list) or len(ids) > 25:
        raise AgentHouseholdInputError('shopping_list_ids must be a list with at most 25 IDs.')
    normalized = []
    for value in ids:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError):
            raise AgentHouseholdInputError('shopping_list_ids must contain integer IDs.')
    values = list(ShoppingList.objects.filter(space=space, id__in=normalized))
    if len(values) != len(set(normalized)):
        raise AgentHouseholdInputError('One or more shopping lists were not found in the active space.')
    return values


def create_shopping_list(request, payload):
    name = str(payload.get('name') or '').strip()
    if not name:
        raise AgentHouseholdInputError('name is required.')
    return ShoppingList.objects.create(
        name=name[:32],
        description=str(payload.get('description') or ''),
        color=(str(payload.get('color')).strip()[:7] if payload.get('color') else None),
        space=request.space,
    )


def create_shopping_entry(request, payload):
    food = Food.objects.filter(pk=payload.get('food_id'), space=request.space).first()
    if food is None:
        raise AgentHouseholdInputError('Food not found in the active space.')
    amount = _decimal(payload.get('amount', 1), 'amount', minimum=0)
    unit = _unit(payload.get('unit_id'), request.space)
    lists = _shopping_lists(payload.get('shopping_list_ids'), request.space)
    with transaction.atomic():
        obj = ShoppingListEntry.objects.create(
            food=food,
            unit=unit,
            amount=amount,
            checked=bool(payload.get('checked', False)),
            created_by=request.user,
            space=request.space,
        )
        if lists:
            obj.shopping_lists.set(lists)
    return obj


def update_shopping_entry(request, obj, payload):
    expected = str(payload.get('expected_updated_at') or '').strip()
    if not expected:
        raise AgentHouseholdInputError('expected_updated_at is required.')
    if obj.updated_at.isoformat() != expected:
        raise AgentHouseholdInputError('Shopping entry changed since it was read.')

    if 'amount' in payload:
        obj.amount = _decimal(payload.get('amount'), 'amount', minimum=0)
    if 'unit_id' in payload:
        obj.unit = _unit(payload.get('unit_id'), request.space)
    if 'checked' in payload:
        obj.checked = bool(payload.get('checked'))
    lists = None
    if 'shopping_list_ids' in payload:
        lists = _shopping_lists(payload.get('shopping_list_ids'), request.space)

    with transaction.atomic():
        obj.save()
        if lists is not None:
            obj.shopping_lists.set(lists)
    return obj


def _parse_plan_datetime(value, meal_type, field):
    raw = str(value or '').strip()
    if not raw:
        raise AgentHouseholdInputError(f'{field} is required.')
    dt = parse_datetime(raw)
    if dt is None:
        day = parse_date(raw)
        if day is None:
            raise AgentHouseholdInputError(f'{field} must be an ISO-8601 date or datetime.')
        preferred_time = meal_type.time if meal_type and meal_type.time else time(hour=12)
        dt = datetime.combine(day, preferred_time)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _meal_type(meal_type_id, space):
    obj = MealType.objects.filter(pk=meal_type_id, space=space).first()
    if obj is None:
        raise AgentHouseholdInputError('meal_type_id was not found in the active space.')
    return obj


def _accessible_recipe(request, recipe_id):
    if recipe_id in (None, ''):
        return None
    obj = accessible_recipe_queryset(request).filter(pk=recipe_id).first()
    if obj is None:
        raise AgentHouseholdInputError('recipe_id was not found or is not accessible to the authenticated user.')
    return obj


def create_meal_plan(request, payload):
    meal_type = _meal_type(payload.get('meal_type_id'), request.space)
    recipe = _accessible_recipe(request, payload.get('recipe_id'))
    servings = _decimal(payload.get('servings', 1), 'servings', minimum='0.0001')
    from_date = _parse_plan_datetime(payload.get('from_date'), meal_type, 'from_date')
    to_value = payload.get('to_date') or payload.get('from_date')
    to_date = _parse_plan_datetime(to_value, meal_type, 'to_date')
    if to_date < from_date:
        raise AgentHouseholdInputError('to_date cannot be before from_date.')

    return MealPlan.objects.create(
        recipe=recipe,
        servings=servings,
        title=str(payload.get('title') or '')[:64],
        meal_type=meal_type,
        note=str(payload.get('note') or ''),
        from_date=from_date,
        to_date=to_date,
        created_by=request.user,
        space=request.space,
    )


def update_meal_plan(request, obj, payload):
    expected = str(payload.get('expected_revision') or '').strip()
    if not expected:
        raise AgentHouseholdInputError('expected_revision is required.')
    if meal_plan_revision(obj) != expected:
        raise AgentHouseholdInputError('Meal plan changed since it was read.')

    meal_type = obj.meal_type
    if 'meal_type_id' in payload:
        meal_type = _meal_type(payload.get('meal_type_id'), request.space)
        obj.meal_type = meal_type
    if 'recipe_id' in payload:
        obj.recipe = _accessible_recipe(request, payload.get('recipe_id'))
    if 'servings' in payload:
        obj.servings = _decimal(payload.get('servings'), 'servings', minimum='0.0001')
    if 'title' in payload:
        obj.title = str(payload.get('title') or '')[:64]
    if 'note' in payload:
        obj.note = str(payload.get('note') or '')
    if 'from_date' in payload:
        obj.from_date = _parse_plan_datetime(payload.get('from_date'), meal_type, 'from_date')
    if 'to_date' in payload:
        obj.to_date = _parse_plan_datetime(payload.get('to_date'), meal_type, 'to_date')
    if obj.to_date < obj.from_date:
        raise AgentHouseholdInputError('to_date cannot be before from_date.')
    obj.save()
    return obj
