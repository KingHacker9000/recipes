import hashlib
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from cookbook.agent_api.models import AgentProposal, FoodNutritionProfile
from cookbook.agent_api.nutrition import unit_descriptor
from cookbook.models import Food, InventoryEntry, InventoryLocation, InventoryLog, Unit


class AgentPantryInputError(ValueError):
    pass


def _decimal(value, field, *, minimum=None, maximum=None):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AgentPantryInputError(f'{field} must be numeric.')
    if minimum is not None and result < Decimal(str(minimum)):
        raise AgentPantryInputError(f'{field} must be at least {minimum}.')
    if maximum is not None and result > Decimal(str(maximum)):
        raise AgentPantryInputError(f'{field} must be at most {maximum}.')
    return result


def _number(value):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))


def location_payload(location):
    return {
        'id': location.id,
        'name': location.name,
        'is_freezer': location.is_freezer,
        'household_id': location.household_id,
        'updated_at': location.updated_at.isoformat() if location.updated_at else None,
    }


def entry_payload(entry):
    return {
        'id': entry.id,
        'location': location_payload(entry.inventory_location),
        'sub_location': entry.sub_location or '',
        'code': entry.code or '',
        'food_id': entry.food_id,
        'food': entry.food.name if entry.food_id else '',
        'amount': _number(entry.amount),
        'unit_id': entry.unit_id,
        'unit': entry.unit.name if entry.unit_id else '',
        'expires': entry.expires.isoformat() if entry.expires else None,
        'note': entry.note or '',
        'created_by_id': entry.created_by_id,
        'updated_at': entry.updated_at.isoformat() if entry.updated_at else None,
    }


def inventory_queryset(space):
    return (InventoryEntry.objects
            .filter(space=space)
            .select_related('inventory_location', 'inventory_location__household', 'food', 'unit'))


def _best_density(food, space):
    profile = (FoodNutritionProfile.objects
               .filter(food=food, space=space, grams_per_ml__isnull=False)
               .order_by('-is_default', '-verified', '-confidence', '-updated_at')
               .first())
    if profile is None or profile.grams_per_ml is None or profile.grams_per_ml <= 0:
        return None
    return Decimal(str(profile.grams_per_ml))


def convert_amount(amount, from_unit_name, to_unit_name, *, food=None, space=None):
    """Convert an amount between inventory/recipe units without guessing.

    Mass and volume use the same canonical factors as the nutrition engine.
    Cross mass/volume conversion requires a food-specific density profile.
    Count/custom units only convert when the normalized unit matches exactly.
    """
    amount = _decimal(amount, 'amount')
    source = unit_descriptor(from_unit_name)
    target = unit_descriptor(to_unit_name)

    if source['dimension'] == target['dimension']:
        if source['dimension'] in ('custom', 'count') and source['name'] != target['name']:
            return None, 'unit_mismatch'
        canonical = amount * source['factor']
        return canonical / target['factor'], ''

    if {source['dimension'], target['dimension']} == {'mass', 'volume'} and food is not None and space is not None:
        density = _best_density(food, space)
        if density is None:
            return None, 'density_required'
        if source['dimension'] == 'volume':
            millilitres = amount * source['factor']
            grams = millilitres * density
            return grams / target['factor'], ''
        grams = amount * source['factor']
        millilitres = grams / density
        return millilitres / target['factor'], ''

    return None, 'unit_mismatch'


def _recipe_ingredients(recipe):
    seen = set()
    ingredients = []
    for step in recipe.steps.all().order_by('order', 'id'):
        for ingredient in step.ingredients.all().order_by('order', 'id'):
            if ingredient.id in seen:
                continue
            seen.add(ingredient.id)
            ingredients.append(ingredient)
    return ingredients


def check_recipe_against_pantry(recipe, space):
    entries = list(inventory_queryset(space).filter(amount__gt=0).order_by('expires', 'id'))
    remaining = {entry.id: Decimal(str(entry.amount)) for entry in entries}
    by_food = {}
    for entry in entries:
        if entry.food_id:
            by_food.setdefault(entry.food_id, []).append(entry)

    details = []
    counts = {'complete': 0, 'partial': 0, 'missing': 0, 'unknown': 0, 'ignored': 0}
    considered = 0

    for ingredient in _recipe_ingredients(recipe):
        required = Decimal(str(ingredient.amount or 0))
        required_unit = ingredient.unit.name if ingredient.unit_id else ''
        if ingredient.no_amount or required <= 0:
            counts['ignored'] += 1
            details.append({
                'ingredient_id': ingredient.id,
                'food_id': ingredient.food_id,
                'food': ingredient.food.name if ingredient.food_id else '',
                'required_amount': _number(required),
                'required_unit': required_unit,
                'status': 'ignored',
                'reason': 'no_amount_or_zero_requirement',
            })
            continue
        considered += 1
        if not ingredient.food_id:
            counts['unknown'] += 1
            details.append({
                'ingredient_id': ingredient.id,
                'food_id': None,
                'food': '',
                'required_amount': _number(required),
                'required_unit': required_unit,
                'status': 'unknown',
                'reason': 'ingredient_has_no_food',
            })
            continue

        needed = required
        consumed = []
        convertible_entry_seen = False
        mismatch_reasons = set()
        for entry in by_food.get(ingredient.food_id, []):
            native_remaining = remaining.get(entry.id, Decimal('0'))
            if native_remaining <= 0:
                continue
            entry_unit = entry.unit.name if entry.unit_id else ''
            available_target, reason = convert_amount(
                native_remaining,
                entry_unit,
                required_unit,
                food=ingredient.food,
                space=space,
            )
            if available_target is None:
                mismatch_reasons.add(reason)
                continue
            convertible_entry_seen = True
            used_target = min(needed, available_target)
            used_native, reverse_reason = convert_amount(
                used_target,
                required_unit,
                entry_unit,
                food=ingredient.food,
                space=space,
            )
            if used_native is None:
                mismatch_reasons.add(reverse_reason)
                continue
            remaining[entry.id] = max(Decimal('0'), native_remaining - used_native)
            needed -= used_target
            consumed.append({
                'entry_id': entry.id,
                'location_id': entry.inventory_location_id,
                'location': entry.inventory_location.name,
                'amount_used': _number(used_native),
                'unit': entry_unit,
            })
            if needed <= 0:
                break

        available = required - max(needed, Decimal('0'))
        if needed <= 0:
            state = 'complete'
        elif available > 0:
            state = 'partial'
        elif by_food.get(ingredient.food_id) and not convertible_entry_seen:
            state = 'unknown'
        else:
            state = 'missing'
        counts[state] += 1
        details.append({
            'ingredient_id': ingredient.id,
            'food_id': ingredient.food_id,
            'food': ingredient.food.name,
            'required_amount': _number(required),
            'required_unit': required_unit,
            'available_amount': _number(available),
            'missing_amount': _number(max(needed, Decimal('0'))),
            'status': state,
            'reason': ','.join(sorted(mismatch_reasons)) if state == 'unknown' else '',
            'consumed_from': consumed,
        })

    known = counts['complete'] + counts['partial'] + counts['missing']
    return {
        'recipe_id': recipe.id,
        'recipe_name': recipe.name,
        'can_make': considered > 0 and counts['complete'] == considered,
        'coverage': {
            **counts,
            'considered': considered,
            'complete_fraction': float(Decimal(counts['complete']) / Decimal(considered)) if considered else 0.0,
            'known_fraction': float(Decimal(known) / Decimal(considered)) if considered else 0.0,
        },
        'ingredients': details,
    }


def _resolve_food(observation, space):
    food_id = observation.get('food_id')
    if food_id not in (None, ''):
        food = Food.objects.filter(pk=food_id, space=space).first()
        if food is None:
            raise AgentPantryInputError(f'Food {food_id} was not found in the active space.')
        return food
    name = str(observation.get('food_name') or observation.get('food') or '').strip()
    if not name:
        raise AgentPantryInputError('Each observation requires food_id or food_name.')
    food = Food.objects.filter(name__iexact=name, space=space).first()
    if food is None:
        return None
    return food


def _resolve_unit(observation, space):
    unit_id = observation.get('unit_id')
    if unit_id not in (None, ''):
        unit = Unit.objects.filter(pk=unit_id, space=space).first()
        if unit is None:
            raise AgentPantryInputError(f'Unit {unit_id} was not found in the active space.')
        return unit
    name = str(observation.get('unit_name') or observation.get('unit') or '').strip()
    if not name:
        return None
    return Unit.objects.filter(name__iexact=name, space=space).first()


def _resolve_location(observation, space, default_location_id=None):
    location_id = observation.get('location_id') or default_location_id
    if location_id not in (None, ''):
        location = InventoryLocation.objects.filter(pk=location_id, space=space).first()
        if location is None:
            raise AgentPantryInputError(f'Inventory location {location_id} was not found in the active space.')
        return location
    name = str(observation.get('location_name') or '').strip()
    if not name:
        raise AgentPantryInputError('Each observation requires a location or default_location_id.')
    location = InventoryLocation.objects.filter(name__iexact=name, space=space).first()
    if location is None:
        raise AgentPantryInputError(f'Inventory location {name!r} was not found in the active space.')
    return location


def _inventory_revision(space, location_ids):
    rows = (inventory_queryset(space)
            .filter(inventory_location_id__in=sorted(set(location_ids)))
            .order_by('id')
            .values_list('id', 'amount', 'updated_at', 'food_id', 'unit_id', 'inventory_location_id'))
    raw = '|'.join(
        f'{row[0]}:{row[1]}:{row[2].isoformat() if row[2] else ""}:{row[3]}:{row[4]}:{row[5]}'
        for row in rows
    )
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _matching_entry(space, location, food, unit):
    queryset = inventory_queryset(space).filter(inventory_location=location, food=food)
    if unit is None:
        queryset = queryset.filter(unit__isnull=True)
    else:
        queryset = queryset.filter(unit=unit)
    matches = list(queryset.order_by('id')[:2])
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        return None, True
    return None, False


def create_reconcile_proposal(request, observations, *, mode='augment', default_location_id=None):
    """Create a reviewable inventory diff from vision/agent observations.

    ``augment`` never decreases/removes inventory. ``snapshot`` treats one
    location as a complete visible snapshot and may set unobserved entries to
    zero, therefore apply requires explicit high-impact confirmation.
    """
    if mode not in ('augment', 'snapshot'):
        raise AgentPantryInputError('mode must be augment or snapshot.')
    if not isinstance(observations, list) or not observations or len(observations) > 300:
        raise AgentPantryInputError('observations must contain between 1 and 300 items.')

    actions = []
    resolved_observations = []
    location_ids = set()
    snapshot_location_id = None
    observed_food_ids = set()

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise AgentPantryInputError(f'observations[{index}] must be an object.')
        location = _resolve_location(observation, request.space, default_location_id)
        location_ids.add(location.id)
        if mode == 'snapshot':
            if snapshot_location_id is None:
                snapshot_location_id = location.id
            elif snapshot_location_id != location.id:
                raise AgentPantryInputError('snapshot mode is limited to one inventory location per proposal.')

        food = _resolve_food(observation, request.space)
        unit = _resolve_unit(observation, request.space)
        amount = _decimal(observation.get('amount'), f'observations[{index}].amount', minimum=0)
        confidence = _decimal(observation.get('confidence', 1), f'observations[{index}].confidence', minimum=0, maximum=1)
        expires = None
        if observation.get('expires'):
            expires = parse_date(str(observation.get('expires')))
            if expires is None:
                raise AgentPantryInputError(f'observations[{index}].expires must be YYYY-MM-DD.')

        if food is None:
            actions.append({
                'type': 'unresolved',
                'index': index,
                'food_name': str(observation.get('food_name') or observation.get('food') or ''),
                'location_id': location.id,
                'location': location.name,
                'amount': _number(amount),
                'unit': unit.name if unit else str(observation.get('unit_name') or observation.get('unit') or ''),
                'confidence': _number(confidence),
                'reason': 'food_not_found',
            })
            continue

        observed_food_ids.add(food.id)
        existing, ambiguous = _matching_entry(request.space, location, food, unit)
        if ambiguous:
            actions.append({
                'type': 'unresolved',
                'index': index,
                'food_id': food.id,
                'food': food.name,
                'location_id': location.id,
                'location': location.name,
                'amount': _number(amount),
                'unit_id': unit.id if unit else None,
                'unit': unit.name if unit else '',
                'confidence': _number(confidence),
                'reason': 'multiple_matching_inventory_entries',
            })
            continue

        resolved = {
            'index': index,
            'food_id': food.id,
            'food': food.name,
            'location_id': location.id,
            'location': location.name,
            'amount': _number(amount),
            'unit_id': unit.id if unit else None,
            'unit': unit.name if unit else '',
            'expires': expires.isoformat() if expires else None,
            'note': str(observation.get('note') or '')[:256],
            'confidence': _number(confidence),
        }
        resolved_observations.append(resolved)

        if existing is None:
            if amount > 0:
                actions.append({'type': 'add', 'before': None, 'after': resolved})
            else:
                actions.append({'type': 'no_change', 'before': None, 'after': resolved, 'reason': 'zero_observed_without_existing_entry'})
            continue

        before = entry_payload(existing)
        if mode == 'augment' and amount <= existing.amount:
            actions.append({
                'type': 'no_change',
                'entry_id': existing.id,
                'before': before,
                'after': resolved,
                'reason': 'augment_mode_never_decreases_inventory',
            })
            continue
        if amount == existing.amount and (expires is None or expires == existing.expires):
            actions.append({'type': 'no_change', 'entry_id': existing.id, 'before': before, 'after': resolved, 'reason': 'already_matches'})
            continue
        actions.append({'type': 'update', 'entry_id': existing.id, 'before': before, 'after': resolved})

    if mode == 'snapshot':
        existing_entries = inventory_queryset(request.space).filter(inventory_location_id=snapshot_location_id, amount__gt=0)
        for entry in existing_entries:
            if entry.food_id in observed_food_ids:
                continue
            actions.append({
                'type': 'set_zero',
                'entry_id': entry.id,
                'before': entry_payload(entry),
                'after': {'amount': 0.0},
                'reason': 'not_observed_in_explicit_full_snapshot',
            })

    revision_key = _inventory_revision(request.space, location_ids)
    mutating = sum(1 for action in actions if action['type'] in ('add', 'update', 'set_zero'))
    unresolved = sum(1 for action in actions if action['type'] == 'unresolved')
    preview = {
        'mode': mode,
        'requires_explicit_confirmation': True,
        'high_impact': mode == 'snapshot',
        'summary': {
            'actions': len(actions),
            'mutating_actions': mutating,
            'unresolved_actions': unresolved,
        },
        'actions': actions,
    }
    proposal = AgentProposal.objects.create(
        proposal_type='pantry_reconcile',
        payload={
            'mode': mode,
            'default_location_id': default_location_id,
            'observations': observations,
            'resolved_observations': resolved_observations,
            'location_ids': sorted(location_ids),
        },
        preview=preview,
        revision_key=revision_key,
        expires_at=timezone.now() + timedelta(hours=1),
        created_by=request.user,
        space=request.space,
    )
    return proposal


def proposal_payload(proposal):
    return {
        'proposal_id': str(proposal.proposal_id),
        'proposal_type': proposal.proposal_type,
        'status': proposal.status,
        'preview': proposal.preview,
        'result': proposal.result,
        'expires_at': proposal.expires_at.isoformat() if proposal.expires_at else None,
        'created_at': proposal.created_at.isoformat(),
        'updated_at': proposal.updated_at.isoformat(),
    }


def apply_reconcile_proposal(request, proposal, *, confirmed=False):
    if not confirmed:
        raise AgentPantryInputError('confirmed=true is required to apply an inventory proposal.')
    if proposal.proposal_type != 'pantry_reconcile':
        raise AgentPantryInputError('Unsupported proposal type.')
    if proposal.status != AgentProposal.STATUS_PENDING:
        raise AgentPantryInputError(f'Proposal is already {proposal.status}.')
    if proposal.expires_at and proposal.expires_at <= timezone.now():
        proposal.status = AgentProposal.STATUS_EXPIRED
        proposal.save(update_fields=['status', 'updated_at'])
        raise AgentPantryInputError('Proposal has expired; create a new preview.')

    location_ids = proposal.payload.get('location_ids') or []
    current_revision = _inventory_revision(request.space, location_ids)
    if current_revision != proposal.revision_key:
        raise AgentPantryInputError('Inventory changed since this proposal was created; create a new preview.')

    results = []
    with transaction.atomic():
        proposal = (AgentProposal.objects
                    .select_for_update()
                    .filter(pk=proposal.pk, space=request.space, created_by=request.user)
                    .first())
        if proposal is None or proposal.status != AgentProposal.STATUS_PENDING:
            raise AgentPantryInputError('Proposal is no longer pending.')

        locked_entries = {
            entry.id: entry
            for entry in inventory_queryset(request.space)
            .select_for_update()
            .filter(inventory_location_id__in=location_ids)
        }
        for action in proposal.preview.get('actions') or []:
            action_type = action.get('type')
            if action_type not in ('add', 'update', 'set_zero'):
                continue

            if action_type == 'add':
                after = action['after']
                location = InventoryLocation.objects.get(pk=after['location_id'], space=request.space)
                food = Food.objects.get(pk=after['food_id'], space=request.space)
                unit = Unit.objects.get(pk=after['unit_id'], space=request.space) if after.get('unit_id') else None
                entry = InventoryEntry.objects.create(
                    inventory_location=location,
                    food=food,
                    unit=unit,
                    amount=Decimal(str(after['amount'])),
                    expires=parse_date(after['expires']) if after.get('expires') else None,
                    note=after.get('note') or '',
                    created_by=request.user,
                    space=request.space,
                )
                InventoryLog.objects.create(
                    entry=entry,
                    booking_type=InventoryLog.B_ADD,
                    old_amount=0,
                    new_amount=entry.amount,
                    old_inventory_location=location,
                    new_inventory_location=location,
                    note=f'Agent proposal {proposal.proposal_id}',
                    space=request.space,
                )
                results.append({'type': 'add', 'entry': entry_payload(entry)})
                continue

            entry = locked_entries.get(action.get('entry_id'))
            if entry is None:
                raise AgentPantryInputError(f"Inventory entry {action.get('entry_id')} changed or disappeared; create a new preview.")
            old_amount = entry.amount
            if action_type == 'set_zero':
                new_amount = Decimal('0')
            else:
                after = action['after']
                new_amount = Decimal(str(after['amount']))
                entry.expires = parse_date(after['expires']) if after.get('expires') else entry.expires
                if after.get('note'):
                    entry.note = after['note']
            entry.amount = new_amount
            entry.save()
            booking_type = InventoryLog.B_ADD if new_amount >= old_amount else InventoryLog.B_REMOVE
            InventoryLog.objects.create(
                entry=entry,
                booking_type=booking_type,
                old_amount=old_amount,
                new_amount=new_amount,
                old_inventory_location=entry.inventory_location,
                new_inventory_location=entry.inventory_location,
                note=f'Agent proposal {proposal.proposal_id}',
                space=request.space,
            )
            results.append({'type': action_type, 'entry': entry_payload(entry)})

        proposal.status = AgentProposal.STATUS_APPLIED
        proposal.applied_at = timezone.now()
        proposal.result = {'applied': results, 'applied_count': len(results)}
        proposal.save(update_fields=['status', 'applied_at', 'result', 'updated_at'])
    return proposal
