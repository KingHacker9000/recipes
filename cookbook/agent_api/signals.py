from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver
from django_scopes import scopes_disabled

from cookbook.agent_api.models import FoodNutritionProfile
from cookbook.agent_api.native_properties import sync_recipe_native_nutrition_properties
from cookbook.models import Ingredient, Recipe, Step


def _sync_recipe_ids(recipe_ids):
    recipe_ids = tuple(sorted({int(pk) for pk in recipe_ids if pk}))
    if not recipe_ids:
        return

    def run():
        with scopes_disabled():
            recipes = (Recipe.objects
                       .filter(pk__in=recipe_ids)
                       .select_related('space')
                       .prefetch_related('steps__ingredients__food', 'steps__ingredients__unit'))
            for recipe in recipes:
                sync_recipe_native_nutrition_properties(recipe, recipe.space)

    transaction.on_commit(run)


def _recipe_ids_for_step(step):
    return list(Recipe.objects.filter(steps=step).values_list('id', flat=True))


def _recipe_ids_for_ingredient(ingredient):
    return list(Recipe.objects.filter(steps__ingredients=ingredient).values_list('id', flat=True).distinct())


@receiver(post_save, sender=Recipe, dispatch_uid='agent-native-properties-recipe-save')
def sync_recipe_after_save(sender, instance, raw=False, **kwargs):
    if not raw:
        _sync_recipe_ids([instance.pk])


@receiver(post_save, sender=Ingredient, dispatch_uid='agent-native-properties-ingredient-save')
def sync_recipe_after_ingredient_save(sender, instance, raw=False, **kwargs):
    if not raw:
        _sync_recipe_ids(_recipe_ids_for_ingredient(instance))


@receiver(
    m2m_changed,
    sender=Recipe.steps.through,
    dispatch_uid='agent-native-properties-recipe-steps',
)
def sync_recipe_after_step_relation(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if isinstance(instance, Recipe):
        _sync_recipe_ids([instance.pk])
    elif isinstance(instance, Step):
        _sync_recipe_ids(_recipe_ids_for_step(instance))


@receiver(
    m2m_changed,
    sender=Step.ingredients.through,
    dispatch_uid='agent-native-properties-step-ingredients',
)
def sync_recipe_after_ingredient_relation(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if isinstance(instance, Step):
        _sync_recipe_ids(_recipe_ids_for_step(instance))
    elif isinstance(instance, Ingredient):
        _sync_recipe_ids(_recipe_ids_for_ingredient(instance))


@receiver(post_save, sender=FoodNutritionProfile, dispatch_uid='agent-native-properties-profile-save')
@receiver(post_delete, sender=FoodNutritionProfile, dispatch_uid='agent-native-properties-profile-delete')
def sync_recipes_after_profile_change(sender, instance, **kwargs):
    food_id = instance.food_id
    space_id = instance.space_id

    def run():
        with scopes_disabled():
            recipe_ids = list(
                Recipe.objects
                .filter(space_id=space_id, steps__ingredients__food_id=food_id)
                .values_list('id', flat=True)
                .distinct()
            )
            recipes = (Recipe.objects
                       .filter(pk__in=recipe_ids)
                       .select_related('space')
                       .prefetch_related('steps__ingredients__food', 'steps__ingredients__unit'))
            for recipe in recipes:
                sync_recipe_native_nutrition_properties(recipe, recipe.space)

    transaction.on_commit(run)
