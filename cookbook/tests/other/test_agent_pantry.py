from decimal import Decimal
from types import SimpleNamespace

import pytest

from cookbook.agent_api import pantry


def test_pantry_mass_conversion():
    converted, reason = pantry.convert_amount(1, 'kg', 'g')
    assert reason == ''
    assert converted == Decimal('1000')


def test_pantry_volume_conversion():
    converted, reason = pantry.convert_amount(2, 'cup', 'ml')
    assert reason == ''
    assert converted == Decimal('480')


def test_pantry_count_units_only_match_count():
    converted, reason = pantry.convert_amount(6, 'each', '')
    assert reason == ''
    assert converted == Decimal('6')


def test_pantry_custom_unit_mismatch_is_unknown():
    converted, reason = pantry.convert_amount(2, 'scoop', 'g')
    assert converted is None
    assert reason == 'unit_mismatch'


def test_pantry_mass_volume_conversion_requires_density(monkeypatch):
    food = SimpleNamespace(id=1)
    space = SimpleNamespace(id=1)
    monkeypatch.setattr(pantry, '_best_density', lambda food, space: None)
    converted, reason = pantry.convert_amount(250, 'ml', 'g', food=food, space=space)
    assert converted is None
    assert reason == 'density_required'


def test_pantry_mass_volume_conversion_uses_food_density(monkeypatch):
    food = SimpleNamespace(id=1)
    space = SimpleNamespace(id=1)
    monkeypatch.setattr(pantry, '_best_density', lambda food, space: Decimal('1.04'))
    converted, reason = pantry.convert_amount(250, 'ml', 'g', food=food, space=space)
    assert reason == ''
    assert converted == Decimal('260.00')


def test_pantry_reverse_density_conversion(monkeypatch):
    food = SimpleNamespace(id=1)
    space = SimpleNamespace(id=1)
    monkeypatch.setattr(pantry, '_best_density', lambda food, space: Decimal('1.25'))
    converted, reason = pantry.convert_amount(250, 'g', 'ml', food=food, space=space)
    assert reason == ''
    assert converted == Decimal('2E+2')


@pytest.mark.parametrize('value', ['wat', None, {}])
def test_invalid_amount_is_rejected(value):
    with pytest.raises(pantry.AgentPantryInputError):
        pantry.convert_amount(value, 'g', 'g')
