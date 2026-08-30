import base64
import io
from decimal import Decimal

import pytest
from django_scopes import scopes_disabled
from PIL import Image

from cookbook.agent_api import native_properties
from cookbook.agent_api.images import AgentRecipeImageError, normalize_recipe_image, recipe_image_payload
from cookbook.models import Property, PropertyType
from tandoor_mcp.complete import TOOLS


def complete_analysis(**per_serving):
    defaults = {
        'calories': 465.5,
        'protein_g': 12.25,
        'carbohydrate_g': 60.5,
        'fat_g': 18.0,
        'fiber_g': 4.5,
        'sugar_g': 20.0,
        'sodium_mg': 310.0,
    }
    defaults.update(per_serving)
    return {
        'per_serving': defaults,
        'coverage': {
            'ingredient_coverage': 1.0,
            'field_coverage': {field: 1.0 for field in native_properties.NATIVE_NUTRITION_FIELDS},
            'complete_core_macros': True,
        },
    }


def test_native_recipe_properties_mirror_agent_per_serving_nutrition_and_preserve_manual_property(
    monkeypatch,
    space_1,
    recipe_1_s1,
):
    with scopes_disabled():
        manual_type = PropertyType.objects.create(
            name='My Rating',
            unit='points',
            category=PropertyType.OTHER,
            space=space_1,
        )
        manual = Property.objects.create(
            property_type=manual_type,
            property_amount=Decimal('9'),
            space=space_1,
        )
        recipe_1_s1.properties.add(manual)

        monkeypatch.setattr(native_properties, 'analyze_recipe', lambda recipe, space: complete_analysis())
        result = native_properties.sync_recipe_native_nutrition_properties(recipe_1_s1, space_1)

        assert result['synced']['calories'] == 465.5
        marker = f'{native_properties.MANAGED_PROPERTY_PREFIX}{recipe_1_s1.id}'
        managed = {
            prop.property_type.name: prop.property_amount
            for prop in Property.objects.filter(space=space_1, open_data_food_slug=marker).select_related('property_type')
        }
        assert managed['Calories'] == Decimal('465.5000')
        assert managed['Protein'] == Decimal('12.2500')
        assert managed['Sodium'] == Decimal('310.0000')
        assert recipe_1_s1.properties.filter(pk=manual.pk).exists()


def test_native_property_sync_removes_only_stale_agent_fields(monkeypatch, space_1, recipe_1_s1):
    with scopes_disabled():
        monkeypatch.setattr(native_properties, 'analyze_recipe', lambda recipe, space: complete_analysis())
        native_properties.sync_recipe_native_nutrition_properties(recipe_1_s1, space_1)

        incomplete = complete_analysis()
        incomplete['coverage']['field_coverage']['sugar_g'] = 0.5
        monkeypatch.setattr(native_properties, 'analyze_recipe', lambda recipe, space: incomplete)
        native_properties.sync_recipe_native_nutrition_properties(recipe_1_s1, space_1)

        marker = f'{native_properties.MANAGED_PROPERTY_PREFIX}{recipe_1_s1.id}'
        names = set(
            Property.objects
            .filter(space=space_1, open_data_food_slug=marker)
            .values_list('property_type__name', flat=True)
        )
        assert 'Sugar' not in names
        assert 'Calories' in names
        assert 'Protein' in names


def _png_base64():
    image = Image.new('RGB', (8, 6), (120, 40, 200))
    output = io.BytesIO()
    image.save(output, 'PNG')
    return base64.b64encode(output.getvalue()).decode('ascii')


def test_recipe_image_normalization_accepts_png_and_strips_to_canonical_payload():
    result = normalize_recipe_image(_png_base64(), content_type='image/png')
    assert result['content_type'] == 'image/png'
    assert result['width'] == 8
    assert result['height'] == 6
    assert result['filename'].endswith('.png')
    with Image.open(io.BytesIO(result['content'].read())) as image:
        assert image.format == 'PNG'
        assert image.size == (8, 6)


def test_recipe_image_normalization_rejects_invalid_or_mismatched_payload():
    with pytest.raises(AgentRecipeImageError, match='valid base64'):
        normalize_recipe_image('this is not base64')
    with pytest.raises(AgentRecipeImageError, match='content_type does not match'):
        normalize_recipe_image(_png_base64(), content_type='image/jpeg')


def test_recipe_image_payload_reports_native_tandoor_image_state(recipe_1_s1):
    payload = recipe_image_payload(recipe_1_s1)
    assert payload['recipe_id'] == recipe_1_s1.id
    assert 'updated_at' in payload
    assert payload['image'] is None or set(payload['image']) == {'name', 'url'}


def test_mcp_registry_exposes_recipe_image_read_and_upload():
    assert 'recipe_image_get' in TOOLS
    assert 'recipe_image_upload' in TOOLS
    upload = TOOLS['recipe_image_upload']
    assert upload.method == 'POST'
    assert upload.path == '/api/agent/recipes/{recipe_id}/image/'
    assert upload.mutation is True
