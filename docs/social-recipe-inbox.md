# Social Recipe Inbox

The Social Recipe Inbox is the Phase 1 social-media recipe importer for this fork.

## Supported sources

- TikTok
- Instagram Reels/posts
- YouTube Shorts / YouTube links

The API rejects other hosts before `yt-dlp` is invoked.

## API

Authenticated users can use:

- `POST /api/social-import/` with `{"source_url": "...", "ai_provider_id": 1}` (`ai_provider_id` is optional)
- `GET /api/social-import/`
- `GET /api/social-import/<id>/`
- `POST /api/social-import/<id>/retry/`
- `POST /api/social-import/<id>/save/`

Creating a job returns `202` immediately. It does not download a video or call AI in the web request.

The save endpoint accepts an optional edited `extraction` object. This is the review step: imports are never silently written into the cookbook.

## Worker

Run one unprivileged worker:

```sh
python manage.py process_social_imports
```

For smoke tests:

```sh
python manage.py process_social_imports --once
```

The worker should run as the same non-root application identity, with no Docker socket and no additional privileges. Concurrency should remain one initially.

The worker needs `yt-dlp`; video keyframes additionally use `ffmpeg`. The Docker image installs both. Temporary downloads live under the normal OS temporary directory and are deleted after every job. To keep video work off a small root filesystem, set `TMPDIR` for the worker to a directory on the external data drive.

## Processing

Each job moves through:

`queued -> acquiring -> extracting -> ready -> saving -> saved`

Failures become `failed` and retain a bounded error message for review/retry.

Acquisition uses `yt-dlp --no-config` for metadata/subtitles and optionally a bounded video download (100 MB maximum) for up to eight `ffmpeg` keyframes. The AI receives caption + transcript + those keyframes and must return JSON with field-level confidence/provenance. Unknown quantities remain unknown rather than being invented.

The configured Tandoor AI provider is reused. If no explicit provider is attached to the job, the space default provider is used.

## PWA sharing

Tandoor already exposes an installed-PWA Web Share Target at `/recipe/import`. That remains the OS-level entry point. The social inbox API is the durable processing backend and can also be used directly by a paste-URL UI.

## Out of scope for Phase 1

Nutrition resolution, grocery-label scanning, recipe personalization, calorie/protein variants, and automatic high-confidence saving belong to later phases.
