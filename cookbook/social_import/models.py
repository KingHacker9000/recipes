from django.contrib.auth.models import User
from django.db import models
from django_scopes import ScopedManager


class SocialImportJob(models.Model):
    # 0243 shipped with a BigAutoField while this legacy app still defaults
    # implicit primary keys differently. Keep the model state explicit so
    # makemigrations does not try to shrink/alter the live primary key later.
    id = models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')

    PLATFORM_TIKTOK = 'tiktok'
    PLATFORM_INSTAGRAM = 'instagram'
    PLATFORM_YOUTUBE = 'youtube'

    PLATFORMS = (
        (PLATFORM_TIKTOK, 'TikTok'),
        (PLATFORM_INSTAGRAM, 'Instagram'),
        (PLATFORM_YOUTUBE, 'YouTube'),
    )

    STATUS_QUEUED = 'queued'
    STATUS_ACQUIRING = 'acquiring'
    STATUS_EXTRACTING = 'extracting'
    STATUS_READY = 'ready'
    STATUS_SAVING = 'saving'
    STATUS_SAVED = 'saved'
    STATUS_FAILED = 'failed'

    STATUSES = (
        (STATUS_QUEUED, 'Queued'),
        (STATUS_ACQUIRING, 'Acquiring media'),
        (STATUS_EXTRACTING, 'Extracting recipe'),
        (STATUS_READY, 'Ready for review'),
        (STATUS_SAVING, 'Saving'),
        (STATUS_SAVED, 'Saved'),
        (STATUS_FAILED, 'Failed'),
    )

    source_url = models.URLField(max_length=2048)
    canonical_url = models.URLField(max_length=2048, blank=True, default='')
    platform = models.CharField(max_length=32, choices=PLATFORMS)
    external_id = models.CharField(max_length=256, blank=True, default='')
    creator = models.CharField(max_length=256, blank=True, default='')
    caption = models.TextField(blank=True, default='')
    thumbnail_url = models.URLField(max_length=2048, blank=True, default='')
    transcript = models.TextField(blank=True, default='')
    status = models.CharField(max_length=32, choices=STATUSES, default=STATUS_QUEUED)
    extraction = models.JSONField(default=dict, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    error = models.TextField(blank=True, default='')
    retry_count = models.PositiveIntegerField(default=0)

    ai_provider = models.ForeignKey('cookbook.AiProvider', on_delete=models.SET_NULL, null=True, blank=True)
    recipe = models.ForeignKey('cookbook.Recipe', on_delete=models.SET_NULL, null=True, blank=True, related_name='social_import_jobs')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    space = models.ForeignKey('cookbook.Space', on_delete=models.CASCADE)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScopedManager(space='space')

    class Meta:
        ordering = ('-created_at',)
        indexes = (
            models.Index(fields=['space', 'status', 'created_at'], name='cookbook_so_space_i_8564c8_idx'),
            models.Index(fields=['created_by', 'status', 'created_at'], name='cookbook_so_created_9541ff_idx'),
        )

    def __str__(self):
        return f'{self.platform}:{self.pk}:{self.status}'
