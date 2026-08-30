from django.core.management.base import BaseCommand, CommandError
from django_scopes import scopes_disabled

from cookbook.agent_api.native_properties import sync_recipe_native_nutrition_properties
from cookbook.models import Recipe, Space


class Command(BaseCommand):
    help = 'Sync deterministic Agent nutrition into native Tandoor recipe Properties.'

    def add_arguments(self, parser):
        parser.add_argument('--space-id', type=int, required=True)
        parser.add_argument('--recipe-id', type=int)

    def handle(self, *args, **options):
        with scopes_disabled():
            space = Space.objects.filter(pk=options['space_id']).first()
            if space is None:
                raise CommandError(f"Space {options['space_id']} does not exist.")

            recipes = Recipe.objects.filter(space=space).order_by('id')
            if options.get('recipe_id'):
                recipes = recipes.filter(pk=options['recipe_id'])

            count = 0
            property_count = 0
            for recipe in recipes.prefetch_related('steps__ingredients__food', 'steps__ingredients__unit'):
                result = sync_recipe_native_nutrition_properties(recipe, space)
                count += 1
                property_count += len(result['synced'])
                self.stdout.write(
                    f"recipe={recipe.id} name={recipe.name!r} synced={len(result['synced'])}"
                )

            self.stdout.write(self.style.SUCCESS(
                f'Synced {property_count} native nutrition properties across {count} recipes.'
            ))
