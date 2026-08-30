from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from cookbook.agent_api.household import AgentHouseholdInputError, update_meal_plan
from cookbook.views.agent_meal_plans import AgentMealPlanMutableDetailView
from tandoor_mcp.complete import TOOLS


class DummyMealPlan:
    def __init__(self):
        now = timezone.now()
        self.updated_at = now
        self.meal_type = SimpleNamespace(id=1, time=None)
        self.recipe = None
        self.servings = 2
        self.title = 'Dinner'
        self.note = ''
        self.from_date = now + timedelta(days=1)
        self.to_date = self.from_date
        self.saved = False

    def save(self):
        self.saved = True


def test_update_meal_plan_changes_safe_fields_with_expected_revision():
    plan = DummyMealPlan()
    request = SimpleNamespace(space=SimpleNamespace(id=1), user=SimpleNamespace(id=1))
    payload = {
        'expected_updated_at': plan.updated_at.isoformat(),
        'servings': 3,
        'title': 'Updated dinner',
        'note': 'Move later',
        'from_date': (plan.from_date + timedelta(hours=2)).isoformat(),
        'to_date': (plan.to_date + timedelta(hours=2)).isoformat(),
    }
    updated = update_meal_plan(request, plan, payload)
    assert updated is plan
    assert plan.servings == 3
    assert plan.title == 'Updated dinner'
    assert plan.note == 'Move later'
    assert plan.saved is True


def test_update_meal_plan_rejects_stale_revision():
    plan = DummyMealPlan()
    request = SimpleNamespace(space=SimpleNamespace(id=1), user=SimpleNamespace(id=1))
    with pytest.raises(AgentHouseholdInputError, match='changed since it was read'):
        update_meal_plan(request, plan, {'expected_updated_at': '2000-01-01T00:00:00+00:00', 'servings': 4})
    assert plan.saved is False


def test_meal_plan_update_is_exposed_as_semantic_mcp_tool():
    spec = TOOLS['meal_plan_update']
    assert spec.method == 'PATCH'
    assert spec.path == '/api/agent/meal-plans/{meal_plan_id}/'
    assert spec.mutation is True
    assert spec.path_args == ('meal_plan_id',)
    assert 'expected_updated_at' in spec.schema['required']


def test_meal_plan_detail_supports_patch_and_confirmed_delete():
    assert callable(getattr(AgentMealPlanMutableDetailView, 'patch', None))
    assert callable(getattr(AgentMealPlanMutableDetailView, 'delete', None))
