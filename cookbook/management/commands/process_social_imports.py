import time

from django.core.management.base import BaseCommand

from cookbook.social_import.service import process_next_job


class Command(BaseCommand):
    help = 'Process queued Social Recipe Inbox jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one job and exit.')
        parser.add_argument('--poll-seconds', type=float, default=5.0, help='Idle poll interval (default: 5 seconds).')

    def handle(self, *args, **options):
        once = options['once']
        poll = max(1.0, options['poll_seconds'])
        while True:
            job = process_next_job()
            if job:
                self.stdout.write(f'social-import {job.pk}: {job.status}')
            if once:
                return
            if not job:
                time.sleep(poll)
