from decimal import Decimal, InvalidOperation

from django.db import transaction

from cookbook.agent_api.pantry import AgentPantryInputError, entry_payload, inventory_queryset
from cookbook.models import Food, InventoryEntry, InventoryLocation, InventoryLog, Unit


def _decimal(value, field):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AgentPantryInputError(f'{field} must be numeric.')


def _resolve_entry(request, payload):
    if payload.get('entry_id') not in (None, ''):
        entry = inventory_queryset(request.space).filter(pk=payload.get('entry_id')).first()
        if entry is None:
            raise AgentPantryInputError('Inventory entry was not found in the active space.')
        return entry

    food = Food.objects.filter(pk=payload.get('food_id'), space=request.space).first()
    location = InventoryLocation.objects.filter(pk=payload.get('location_id'), space=request.space).first()
    if food is None or location is None:
        raise AgentPantryInputError('food_id and location_id must reference objects in the active space.')
    unit = None
    if payload.get('unit_id') not in (None, ''):
        unit = Unit.objects.filter(pk=payload.get('unit_id'), space=request.space).first()
        if unit is None:
            raise AgentPantryInputError('unit_id was not found in the active space.')

    queryset = inventory_queryset(request.space).filter(food=food, inventory_location=location)
    queryset = queryset.filter(unit=unit) if unit else queryset.filter(unit__isnull=True)
    matches = list(queryset.order_by('id')[:2])
    if len(matches) > 1:
        raise AgentPantryInputError('Multiple matching inventory entries exist; entry_id is required.')
    if matches:
        return matches[0]
    return InventoryEntry(
        food=food,
        inventory_location=location,
        unit=unit,
        amount=0,
        created_by=request.user,
        space=request.space,
    )


def adjust_inventory(request, payload):
    """Apply an explicit inventory delta such as 'used two eggs'."""
    delta = _decimal(payload.get('delta'), 'delta')
    if delta == 0:
        raise AgentPantryInputError('delta cannot be zero.')

    with transaction.atomic():
        seed = _resolve_entry(request, payload)
        if seed.pk:
            entry = inventory_queryset(request.space).select_for_update().get(pk=seed.pk)
            expected = str(payload.get('expected_updated_at') or '').strip()
            if not expected:
                raise AgentPantryInputError('expected_updated_at is required when adjusting an existing entry.')
            if not entry.updated_at or entry.updated_at.isoformat() != expected:
                raise AgentPantryInputError('Inventory entry changed since it was read.')
        else:
            if delta < 0:
                raise AgentPantryInputError('Cannot remove inventory from an entry that does not exist.')
            entry = seed

        old_amount = Decimal(str(entry.amount or 0))
        new_amount = old_amount + delta
        if new_amount < 0:
            raise AgentPantryInputError('Inventory adjustment would make the amount negative.')

        if not entry.pk:
            entry.save()
        entry.amount = new_amount
        if 'note' in payload:
            entry.note = str(payload.get('note') or '')[:256]
        entry.save()

        booking_type = InventoryLog.B_ADD if delta > 0 else InventoryLog.B_REMOVE
        InventoryLog.objects.create(
            entry=entry,
            booking_type=booking_type,
            old_amount=old_amount,
            new_amount=new_amount,
            old_inventory_location=entry.inventory_location,
            new_inventory_location=entry.inventory_location,
            note=str(payload.get('reason') or payload.get('note') or 'Agent API inventory adjustment')[:256],
            space=request.space,
        )
    return entry_payload(entry)
