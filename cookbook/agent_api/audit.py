import hashlib
import json

from django.db import IntegrityError
from django.utils import timezone

from cookbook.agent_api.models import AgentAuditEvent


def agent_request_context(request):
    """Return bounded client/request metadata supplied by an agent adapter."""
    client_id = str(request.headers.get('X-Agent-Client') or 'tandoor-agent-api').strip()[:128]
    request_id = str(request.headers.get('X-Request-ID') or '').strip()[:128]
    idempotency_key = str(request.headers.get('Idempotency-Key') or '').strip()[:256]
    return {
        'client_id': client_id or 'tandoor-agent-api',
        'request_id': request_id,
        'idempotency_key': idempotency_key,
    }


def idempotency_fingerprint(request, action):
    """Fingerprint the intended mutation without including transport retry metadata."""
    canonical = {
        'action': str(action or ''),
        'method': str(getattr(request, 'method', '') or '').upper(),
        'path': str(getattr(request, 'path', '') or ''),
        'payload': getattr(request, 'data', {}) or {},
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def find_idempotency_event(request):
    """Return the event currently reserving this client's idempotency key."""
    context = agent_request_context(request)
    if not context['idempotency_key']:
        return None
    return (AgentAuditEvent.objects
            .filter(
                space=request.space,
                created_by=request.user,
                client_id=context['client_id'],
                idempotency_key=context['idempotency_key'],
            )
            .order_by('-created_at')
            .first())


def find_idempotent_replay(request, *, action=None):
    event = find_idempotency_event(request)
    if event is None or not event.success:
        return None
    if action is not None:
        if event.action != action:
            return None
        stored_fingerprint = (event.metadata or {}).get('idempotency_fingerprint')
        if stored_fingerprint and stored_fingerprint != idempotency_fingerprint(request, action):
            return None
    return event


def _release_failed_idempotency_reservation(request, context, action, fingerprint):
    """Release a failed attempt so the same operation can be retried safely.

    AgentAuditEvent historically made the idempotency key unique for both
    successful and failed audit rows, while replay lookup intentionally ignored
    failed rows. That combination permanently trapped a key after a failed
    attempt: a later valid retry reached the write, then the audit insert raised
    IntegrityError and rolled the mutation back. Preserve the failed audit row,
    but move its key into metadata before reusing the key for the retry.
    """
    existing = (AgentAuditEvent.objects
                .filter(
                    space=request.space,
                    created_by=request.user,
                    client_id=context['client_id'],
                    idempotency_key=context['idempotency_key'],
                )
                .order_by('-created_at')
                .first())
    if existing is None:
        return

    if existing.success:
        raise IntegrityError('Idempotency-Key is already committed by a successful mutation.')
    if existing.action != action:
        raise IntegrityError(
            f'Idempotency-Key belongs to failed action {existing.action!r}, not {action!r}.'
        )

    existing_fingerprint = (existing.metadata or {}).get('idempotency_fingerprint')
    if existing_fingerprint and existing_fingerprint != fingerprint:
        raise IntegrityError('Idempotency-Key was already used for a different request payload.')

    released_metadata = dict(existing.metadata or {})
    released_metadata.update({
        'released_idempotency_key': context['idempotency_key'],
        'released_for_retry_at': timezone.now().isoformat(),
    })
    existing.idempotency_key = ''
    existing.metadata = released_metadata
    existing.save(update_fields=['idempotency_key', 'metadata'])


def record_agent_event(
    request,
    *,
    action,
    target_type='',
    target_id='',
    before=None,
    after=None,
    metadata=None,
    response=None,
    success=True,
    error='',
):
    context = agent_request_context(request)
    event_metadata = dict(metadata or {})

    if context['idempotency_key']:
        fingerprint = idempotency_fingerprint(request, action)
        event_metadata.setdefault('idempotency_fingerprint', fingerprint)
        _release_failed_idempotency_reservation(
            request,
            context,
            action,
            fingerprint,
        )

    return AgentAuditEvent.objects.create(
        client_id=context['client_id'],
        action=action,
        target_type=str(target_type or '')[:128],
        target_id=str(target_id or '')[:128],
        request_id=context['request_id'],
        idempotency_key=context['idempotency_key'],
        before=before or {},
        after=after or {},
        metadata=event_metadata,
        response=response or {},
        success=bool(success),
        error=str(error or '')[:5000],
        created_by=request.user,
        space=request.space,
    )
