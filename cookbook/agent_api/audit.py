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


def find_idempotent_replay(request):
    context = agent_request_context(request)
    if not context['idempotency_key']:
        return None
    return (AgentAuditEvent.objects
            .filter(
                space=request.space,
                created_by=request.user,
                client_id=context['client_id'],
                idempotency_key=context['idempotency_key'],
                success=True,
            )
            .order_by('-created_at')
            .first())


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
    return AgentAuditEvent.objects.create(
        client_id=context['client_id'],
        action=action,
        target_type=str(target_type or '')[:128],
        target_id=str(target_id or '')[:128],
        request_id=context['request_id'],
        idempotency_key=context['idempotency_key'],
        before=before or {},
        after=after or {},
        metadata=metadata or {},
        response=response or {},
        success=bool(success),
        error=str(error or '')[:5000],
        created_by=request.user,
        space=request.space,
    )
