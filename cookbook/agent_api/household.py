from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
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


def create_meal_plan(request, payload):
    meal_type = MealType.objects.filter(pk=payload.get('meal_type_id'), space=request.space).first()
    if meal_type is None:
        raise AgentHouseholdInputError('meal_type_id was not found in the active space.')

    recipe = None
    if payload.get('recipe_id') not in (None, ''):
        recipe = Recipe.objects.filter(pk=payload.get('recipe_id'), space=request.space).first()
        if recipe is None:
            raise AgentHouseholdInputError('recipe_id was not found in the active space.')

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
