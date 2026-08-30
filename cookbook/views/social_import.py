import json
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from recipe_scrapers import scrape_html
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.helper import recipe_url_import as recipe_import_helper
from cookbook.helper.permission_helper import CustomIsUser, CustomTokenHasReadWriteScope
from cookbook.models import AiProvider
from cookbook.serializer import RecipeSerializer
from cookbook.social_import.acquisition import SocialImportError, canonicalize_url, identify_platform
from cookbook.social_import.models import SocialImportJob
from cookbook.social_import.service import normalize_extraction


ACTIVE_IMPORT_STATUSES = (
    SocialImportJob.STATUS_QUEUED,
    SocialImportJob.STATUS_ACQUIRING,
    SocialImportJob.STATUS_EXTRACTING,
    SocialImportJob.STATUS_READY,
)


def social_share_entry(request):
    """Route installed-PWA social shares into the inbox without breaking normal URL imports."""
    shared_url = str(request.GET.get('url') or request.GET.get('text') or '').strip()
    if shared_url:
        try:
            identify_platform(shared_url)
            return HttpResponseRedirect('/recipe/social-inbox?' + urlencode({'url': shared_url}))
        except SocialImportError:
            pass
    return render(request, 'frontend/tandoor.html', {})


class SocialImportJobSerializer(serializers.ModelSerializer):
    recipe_id = serializers.IntegerField(source='recipe.id', read_only=True)
    ai_provider_id = serializers.IntegerField(source='ai_provider.id', read_only=True)

    class Meta:
        model = SocialImportJob
        fields = (
            'id', 'source_url', 'canonical_url', 'platform', 'external_id', 'creator',
            'caption', 'thumbnail_url', 'transcript', 'status', 'extraction',
            'confidence', 'error', 'retry_count', 'ai_provider_id', 'recipe_id',
            'created_at', 'updated_at',
        )
        read_only_fields = fields


class SocialImportCollectionView(APIView):
    permission_classes = [CustomIsUser & CustomTokenHasReadWriteScope]

    def get(self, request):
        queryset = (SocialImportJob.objects
                    .filter(space=request.space, created_by=request.user)
                    .select_related('ai_provider', 'recipe')
                    .order_by('-created_at')[:100])
        return Response(SocialImportJobSerializer(queryset, many=True).data)

    def post(self, request):
        source_url = str(request.data.get('source_url') or request.data.get('url') or '').strip()
        if not source_url:
            return Response({'error': True, 'msg': 'source_url is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            platform = identify_platform(source_url)
            canonical = canonicalize_url(source_url)
        except SocialImportError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        provider = None
        provider_id = request.data.get('ai_provider_id')
        if provider_id not in (None, ''):
            try:
                provider = AiProvider.objects.filter(pk=int(provider_id)).first()
            except (TypeError, ValueError):
                provider = None
            if provider is None or provider.space_id not in (None, request.space.id):
                return Response({'error': True, 'msg': 'AI provider not found.'}, status=status.HTTP_404_NOT_FOUND)

        existing = (SocialImportJob.objects
                    .filter(
                        canonical_url=canonical,
                        created_by=request.user,
                        space=request.space,
                        status__in=ACTIVE_IMPORT_STATUSES,
                    )
                    .order_by('-created_at')
                    .first())
        if existing:
            return Response(SocialImportJobSerializer(existing).data, status=status.HTTP_200_OK)

        job = SocialImportJob.objects.create(
            source_url=source_url,
            canonical_url=canonical,
            platform=platform,
            ai_provider=provider,
            created_by=request.user,
            space=request.space,
        )
        return Response(SocialImportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class SocialImportDetailView(APIView):
    permission_classes = [CustomIsUser & CustomTokenHasReadWriteScope]

    def get_object(self, request, pk):
        return (SocialImportJob.objects
                .filter(pk=pk, space=request.space, created_by=request.user)
                .select_related('ai_provider', 'recipe')
                .first())

    def get(self, request, pk):
        job = self.get_object(request, pk)
        if not job:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(SocialImportJobSerializer(job).data)


class SocialImportRetryView(SocialImportDetailView):
    def post(self, request, pk):
        job = self.get_object(request, pk)
        if not job:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if job.status == SocialImportJob.STATUS_SAVED:
            return Response({'error': True, 'msg': 'Saved jobs cannot be retried.'}, status=status.HTTP_409_CONFLICT)
        job.status = SocialImportJob.STATUS_QUEUED
        job.error = ''
        job.retry_count += 1
        job.save(update_fields=['status', 'error', 'retry_count', 'updated_at'])
        return Response(SocialImportJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


def _schema_recipe(extraction):
    ingredients = []
    for item in extraction.get('ingredients') or []:
        pieces = []
        if item.get('quantity') is not None:
            pieces.append(str(item['quantity']))
        if item.get('unit'):
            pieces.append(item['unit'])
        pieces.append(item['food'])
        if item.get('preparation'):
            pieces.append(f"({item['preparation']})")
        ingredients.append(' '.join(piece for piece in pieces if piece).strip())

    instructions = [{'@type': 'HowToStep', 'text': step['text']} for step in extraction.get('steps') or []]
    payload = {
        '@context': 'https://schema.org',
        '@type': 'Recipe',
        'name': extraction.get('title') or 'Imported social recipe',
        'description': extraction.get('description') or '',
        'recipeIngredient': ingredients,
        'recipeInstructions': instructions,
    }
    if extraction.get('servings'):
        payload['recipeYield'] = str(extraction['servings'])
    prep = extraction.get('prep_time_minutes')
    cook = extraction.get('cook_time_minutes')
    if isinstance(prep, int) and prep >= 0:
        payload['prepTime'] = f'PT{prep}M'
    if isinstance(cook, int) and cook >= 0:
        payload['cookTime'] = f'PT{cook}M'
    return payload


class SocialImportSaveView(SocialImportDetailView):
    def post(self, request, pk):
        job = self.get_object(request, pk)
        if not job:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if job.recipe_id:
            return Response({'recipe_id': job.recipe_id, 'job': SocialImportJobSerializer(job).data})
        if job.status not in (SocialImportJob.STATUS_READY, SocialImportJob.STATUS_FAILED):
            return Response({'error': True, 'msg': 'The import is not ready for review.'}, status=status.HTTP_409_CONFLICT)

        extraction = request.data.get('extraction', job.extraction)
        try:
            extraction = normalize_extraction(extraction)
        except SocialImportError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if not extraction['ingredients'] and not extraction['steps']:
            return Response(
                {'error': True, 'msg': 'Add at least one ingredient or step before saving.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job.status = SocialImportJob.STATUS_SAVING
        job.extraction = extraction
        job.save(update_fields=['status', 'extraction', 'updated_at'])
        try:
            payload = _schema_recipe(extraction)
            html = "<script type='application/ld+json'>" + json.dumps(payload) + '</script>'
            scraper = scrape_html(html=html, org_url=job.canonical_url or job.source_url, supported_only=False)
            recipe_data = recipe_import_helper.get_from_scraper(scraper, request)
            recipe_data['source_url'] = job.canonical_url or job.source_url

            serializer = RecipeSerializer(data=recipe_data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            recipe = serializer.save()

            job.recipe = recipe
            job.status = SocialImportJob.STATUS_SAVED
            job.error = ''
            job.save(update_fields=['recipe', 'status', 'error', 'updated_at'])
            return Response({'recipe_id': recipe.id, 'job': SocialImportJobSerializer(job).data}, status=status.HTTP_201_CREATED)
        except Exception as exc:
            job.status = SocialImportJob.STATUS_READY
            job.error = f'Save failed: {exc}'[:5000]
            job.save(update_fields=['status', 'error', 'updated_at'])
            raise
