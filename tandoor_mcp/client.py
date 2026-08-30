import os
import uuid

import httpx


class TandoorAgentClientError(RuntimeError):
    pass


class TandoorAgentClient:
    def __init__(self, base_url=None, token=None, *, timeout=20):
        self.base_url = str(base_url or os.environ.get('TANDOOR_BASE_URL') or '').strip().rstrip('/')
        self.token = str(token or os.environ.get('TANDOOR_API_TOKEN') or '').strip()
        if not self.base_url:
            raise TandoorAgentClientError('TANDOOR_BASE_URL is required.')
        if not self.token:
            raise TandoorAgentClientError('TANDOOR_API_TOKEN is required.')
        if not self.base_url.startswith(('http://', 'https://')):
            raise TandoorAgentClientError('TANDOOR_BASE_URL must use http or https.')
        self.timeout = timeout

    async def request(self, method, path, *, params=None, json=None, mutation=False, idempotency_key=None):
        # Paths are supplied only by the fixed tool registry in server.py.
        if not path.startswith('/api/agent/') or '://' in path or '..' in path:
            raise TandoorAgentClientError('Refusing non-Agent-API path.')
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/json',
            'X-Agent-Client': 'tandoor-mcp',
            'X-Request-ID': str(uuid.uuid4()),
        }
        if mutation:
            headers['Idempotency-Key'] = str(idempotency_key or uuid.uuid4())
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            try:
                response = await client.request(
                    method.upper(),
                    f'{self.base_url}{path}',
                    params=params,
                    json=json,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise TandoorAgentClientError(f'Tandoor Agent API request failed: {exc.__class__.__name__}.')
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {'raw': response.text[:2000]}
        if response.status_code >= 400:
            detail = payload.get('msg') if isinstance(payload, dict) else None
            raise TandoorAgentClientError(f'Tandoor Agent API HTTP {response.status_code}: {detail or payload}')
        return payload
