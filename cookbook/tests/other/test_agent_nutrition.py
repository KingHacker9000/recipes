from decimal import Decimal
from types import SimpleNamespace

import pytest

from cookbook.agent_api import nutrition


def profile(**overrides):
    values = {
        'id': 1,
        'food_id': 10,
        'basis_amount': Decimal('100'),
        'basis_unit': 'g',
        'grams_per_ml': None,
        'calories': Decimal('165'),
        'protein_g': Decimal('31'),
        'carbohydrate_g': Decimal('0'),
        'fat_g': Decimal('3.6'),
        'fiber_g': None,
        'sugar_g': None,
        'sodium_mg': Decimal('74'),
        'confidence': Decimal('0.95'),
        'label': 'Chicken breast',
        'brand': '',
        'barcode': '',
        'source_type': 'reference',
        'source_reference': 'test',
        'verified': False,
        'is_default': True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ('amount', 'unit', 'expected'),
    [
        (250, 'g', Decimal('2.5')),
        (1, 'kg', Decimal('10')),
        (8, 'oz', Decimal('2.26796185')),
        (1, 'lb', Decimal('4.5359237')),
    ],
)
def test_mass_units_convert_to_profile_basis(amount, unit, expected):
    ratio, reason = nutrition.ratio_for_profile(amount, unit, profile())
    assert reason == ''
    assert ratio.quantize(Decimal('0.00000001')) == expected.quantize(Decimal('0.00000001'))


def test_volume_profile_conversion():
    milk = profile(basis_amount=Decimal('250'), basis_unit='ml')
    ratio, reason = nutrition.ratio_for_profile(500, 'ml', milk)
    assert reason == ''
    assert ratio == Decimal('2')


def test_density_allows_volume_to_mass_conversion():
    yogurt = profile(basis_amount=Decimal('100'), basis_unit='g', grams_per_ml=Decimal('1.05'))
    ratio, reason = nutrition.ratio_for_profile(200, 'ml', yogurt)
    assert reason == ''
    assert ratio == Decimal('2.10')


def test_incompatible_dimension_is_not_guessed():
    ratio, reason = nutrition.ratio_for_profile(2, 'scoop', profile())
    assert ratio is None
    assert reason == 'unit_mismatch'


def test_custom_units_require_exact_match():
    scoop_profile = profile(basis_amount=Decimal('1'), basis_unit='scoop')
    ratio, reason = nutrition.ratio_for_profile(2, 'scoops', scoop_profile)
    assert ratio is None
    assert reason == 'unit_mismatch'

    ratio, reason = nutrition.ratio_for_profile(2, 'scoop', scoop_profile)
    assert reason == ''
    assert ratio == Decimal('2')


def test_blank_unit_can_match_each_profile():
    egg = profile(basis_amount=Decimal('1'), basis_unit='each')
    ratio, reason = nutrition.ratio_for_profile(3, '', egg)
    assert reason == ''
    assert ratio == Decimal('3')


def test_zero_or_unknown_amount_is_not_calculated():
    assert nutrition.ratio_for_profile(0, 'g', profile()) == (None, 'missing_amount')
    assert nutrition.ratio_for_profile('some', 'g', profile()) == (None, 'missing_amount')


def test_calculate_food_amount_uses_compatible_profile(monkeypatch):
    incompatible = profile(id=1, basis_unit='each', is_default=True, verified=True, confidence=Decimal('1'))
    compatible = profile(id=2, basis_unit='g', calories=Decimal('100'), protein_g=Decimal('20'))
    monkeypatch.setattr(nutrition, '_ordered_profiles', lambda food, space: [incompatible, compatible])

    result = nutrition.calculate_food_amount(SimpleNamespace(id=10), 150, 'g', SimpleNamespace(id=1))
    assert result['matched'] is True
    assert result['profile'].id == 2
    assert result['nutrients']['calories'] == Decimal('150')
    assert result['nutrients']['protein_g'] == Decimal('30')
    assert result['profile_failures'][0]['profile_id'] == 1


def test_calculate_food_amount_preserves_unknown_nutrients(monkeypatch):
    partial = profile(calories=Decimal('100'), protein_g=None, carbohydrate_g=None, fat_g=None)
    monkeypatch.setattr(nutrition, '_ordered_profiles', lambda food, space: [partial])

    result = nutrition.calculate_food_amount(SimpleNamespace(id=10), 50, 'g', SimpleNamespace(id=1))
    assert result['nutrients']['calories'] == Decimal('50')
    assert result['nutrients']['protein_g'] is None
    assert result['nutrients']['carbohydrate_g'] is None
    assert result['nutrients']['fat_g'] is None


def test_unit_normalization_is_conservative():
    assert nutrition.normalize_unit_name(' Grams ') == 'grams'
    assert nutrition.normalize_unit_name('pieces') == 'each'
    assert nutrition.normalize_unit_name('scoops') == 'scoops'
