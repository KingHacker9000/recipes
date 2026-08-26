import asyncio
import base64
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from PIL import Image
from django.test import override_settings

from cookbook.helper import ai_runtime
from cookbook.helper.ai_helper import can_perform_ai_request
from cookbook.helper.ai_runtime_worker import (
    _claude_prompt_stream,
    _coerce_json,
    _materialize_attachments,
    _token_usage,
)
from cookbook.serializer import AiProviderSerializer


@pytest.mark.parametrize(
    ('model', 'runtime', 'runtime_model'),
    [
        ('gpt-5-mini', 'litellm', 'gpt-5-mini'),
        ('openai/gpt-5', 'litellm', 'openai/gpt-5'),
        ('codex/default', 'codex', None),
        ('codex/gpt-5.3-codex', 'codex', 'gpt-5.3-codex'),
        ('claude-code/default', 'claude-code', None),
        ('claude-code/claude-sonnet-4-6', 'claude-code', 'claude-sonnet-4-6'),
    ],
)
def test_runtime_prefix_detection(model, runtime, runtime_model):
    assert ai_runtime.provider_runtime(model) == runtime
    assert ai_runtime.runtime_model(model) == runtime_model


@override_settings(SECRET_KEY='subscription-ai-test-secret')
def test_claude_setup_token_is_encrypted_at_rest():
    token = 'sk-ant-oat01-test-token'
    stored = ai_runtime.encrypt_subscription_token(token)

    assert stored.startswith(ai_runtime.TOKEN_PREFIX)
    assert token not in stored
    assert ai_runtime.decrypt_subscription_token(stored) == token
    assert ai_runtime.subscription_token_present(stored)


@override_settings(SECRET_KEY='subscription-ai-test-secret')
def test_claude_serializer_encrypts_token_and_forces_subscription_settings():
    serializer = AiProviderSerializer(data={
        'name': 'Claude subscription',
        'description': '',
        'api_key': 'claude-setup-token',
        'model_name': 'claude-code/default',
        'url': 'https://should-be-removed.invalid',
        'log_credit_cost': True,
        'space': None,
    })

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data['api_key'].startswith(ai_runtime.TOKEN_PREFIX)
    assert ai_runtime.decrypt_subscription_token(serializer.validated_data['api_key']) == 'claude-setup-token'
    assert serializer.validated_data['url'] is None
    assert serializer.validated_data['log_credit_cost'] is False


def test_codex_serializer_never_accepts_api_credentials():
    serializer = AiProviderSerializer(data={
        'name': 'Codex subscription',
        'description': '',
        'api_key': 'must-not-survive',
        'model_name': 'codex/default',
        'url': 'https://should-be-removed.invalid',
        'log_credit_cost': True,
        'space': None,
    })

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data['api_key'] == ''
    assert serializer.validated_data['url'] is None
    assert serializer.validated_data['log_credit_cost'] is False


def test_subscription_provider_bypasses_credit_balance_but_not_ai_kill_switch():
    provider = SimpleNamespace(model_name='codex/default')
    exhausted_space = SimpleNamespace(ai_enabled=True, ai_credits_monthly=0, ai_credits_balance=0)
    disabled_space = SimpleNamespace(ai_enabled=False, ai_credits_monthly=999, ai_credits_balance=999)

    assert can_perform_ai_request(exhausted_space, provider) is True
    assert can_perform_ai_request(disabled_space, provider) is False


def _tiny_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (2, 2), 'white').save(buffer, format='PNG')
    return buffer.getvalue()


def _tiny_pdf_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), 'white').save(buffer, format='PDF')
    return buffer.getvalue()


def _data_url(mime_type: str, raw: bytes) -> str:
    return f'data:{mime_type};base64,{base64.b64encode(raw).decode("ascii")}'


def test_subscription_runtime_normalizes_text_image_and_pdf_payloads():
    prompt, attachments = ai_runtime._normalize_messages([{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'Read this recipe'},
            {'type': 'image_url', 'image_url': _data_url('image/png', _tiny_png_bytes())},
            {'type': 'image_url', 'image_url': _data_url('application/pdf', _tiny_pdf_bytes())},
        ],
    }])

    assert 'Read this recipe' in prompt
    assert '[ATTACHMENT 1: image/png]' in prompt
    assert '[ATTACHMENT 2: application/pdf]' in prompt
    assert [item['mime_type'] for item in attachments] == ['image/png', 'application/pdf']
    assert all(item['data'] for item in attachments)


def test_subscription_runtime_rejects_remote_attachment_urls():
    with pytest.raises(ai_runtime.AiRuntimeBadRequest, match='inline data URLs'):
        ai_runtime._normalize_messages([{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Read this'},
                {'type': 'image_url', 'image_url': 'https://example.com/recipe.png'},
            ],
        }])


def test_worker_materializes_image_and_rasterizes_pdf():
    payload = {
        'attachments': [
            {
                'mime_type': 'image/png',
                'data': base64.b64encode(_tiny_png_bytes()).decode('ascii'),
            },
            {
                'mime_type': 'application/pdf',
                'data': base64.b64encode(_tiny_pdf_bytes()).decode('ascii'),
            },
        ]
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        prepared = _materialize_attachments(payload, temp_dir)

        assert len(prepared) >= 2
        assert prepared[0]['mime_type'] == 'image/png'
        assert prepared[0]['source'] == 'image'
        assert any(item['source'] == 'pdf_page' for item in prepared)
        assert all(item['mime_type'] == 'image/png' for item in prepared)
        assert all(item['data'] for item in prepared)
        assert all(Path(item['path']).is_file() for item in prepared)
        assert not list(Path(temp_dir).glob('*.pdf'))


def test_claude_stream_contains_multimodal_content():
    prepared = [{
        'mime_type': 'image/png',
        'data': base64.b64encode(_tiny_png_bytes()).decode('ascii'),
        'path': '/tmp/not-used-by-claude.png',
        'source': 'image',
    }]

    async def collect():
        return [message async for message in _claude_prompt_stream('Read this', prepared)]

    messages = asyncio.run(collect())
    content = messages[0]['message']['content']

    assert content[0] == {'type': 'text', 'text': 'Read this'}
    assert content[1]['type'] == 'image'
    assert content[1]['source']['type'] == 'base64'
    assert content[1]['source']['media_type'] == 'image/png'


def test_codex_completion_uses_isolated_worker_contract_with_attachment(monkeypatch):
    seen = {}

    monkeypatch.setattr(ai_runtime, 'codex_connected', lambda: True)
    monkeypatch.setattr(litellm, 'callbacks', [])

    def fake_worker(payload, runtime, credential=None, timeout=None):
        seen.update(payload=payload, runtime=runtime, credential=credential)
        return {
            'ok': True,
            'data': {'name': 'Pasta'},
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7},
        }

    monkeypatch.setattr(ai_runtime, '_invoke_worker', fake_worker)
    response = ai_runtime.completion(
        model='codex/default',
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Return a recipe JSON object'},
                {'type': 'image_url', 'image_url': _data_url('image/png', _tiny_png_bytes())},
            ],
        }],
        response_format={'type': 'json_object'},
    )

    assert seen['runtime'] == 'codex'
    assert seen['credential'] is None
    assert seen['payload']['schema'] == {'type': 'object', 'additionalProperties': True}
    assert seen['payload']['attachments'][0]['mime_type'] == 'image/png'
    assert json.loads(response.choices[0].message.content) == {'name': 'Pasta'}
    assert response['usage'] == {'prompt_tokens': 11, 'completion_tokens': 7}


@override_settings(SECRET_KEY='subscription-ai-test-secret')
def test_claude_completion_decrypts_token_only_for_worker(monkeypatch):
    token = 'private-claude-token'
    encrypted = ai_runtime.encrypt_subscription_token(token)
    seen = {}
    monkeypatch.setattr(litellm, 'callbacks', [])

    def fake_worker(payload, runtime, credential=None, timeout=None):
        seen.update(payload=payload, runtime=runtime, credential=credential)
        return {'ok': True, 'data': {'ok': True}, 'usage': {}}

    monkeypatch.setattr(ai_runtime, '_invoke_worker', fake_worker)
    response = ai_runtime.completion(
        api_key=encrypted,
        model='claude-code/default',
        messages=[{'role': 'user', 'content': 'Return JSON'}],
        response_format={'type': 'json_object'},
    )

    assert seen['runtime'] == 'claude-code'
    assert seen['credential'] == token
    assert encrypted not in seen['payload']['prompt']
    assert json.loads(response.choices[0].message.content) == {'ok': True}


def test_worker_json_and_usage_normalizers():
    assert _coerce_json('prefix {"ok": true} suffix') == {'ok': True}
    assert _token_usage({'input_tokens': 9, 'output_tokens': 4}) == {
        'prompt_tokens': 9,
        'completion_tokens': 4,
    }
