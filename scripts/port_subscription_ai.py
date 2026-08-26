from __future__ import annotations

import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one occurrence, found {count}")
    return text.replace(old, new, 1)


# Python dependencies ---------------------------------------------------------
path = "requirements.txt"
text = read(path)
if "openai-codex==0.147.0" not in text:
    text = replace_once(
        text,
        "litellm==1.89.3\n",
        "litellm==1.89.3\nopenai-codex==0.147.0\nclaude-agent-sdk==0.2.144\n",
        "requirements AI dependencies",
    )
write(path, text)


# Docker runtime --------------------------------------------------------------
path = "Dockerfile"
text = read(path)
text = replace_once(
    text,
    "openldap git libgcc libstdc++ nginx tini envsubst nodejs npm",
    "openldap git libgcc libstdc++ nginx tini envsubst nodejs npm ripgrep",
    "Docker Alpine runtime packages",
)
anchor = "#Print all logs without buffering it.\n"
block = """# Dedicated unprivileged identity and persistent credential root for subscription AI runtimes.\nRUN addgroup -S aiagent && adduser -S -D -H -G aiagent -h /var/lib/tandoor-ai aiagent && \\\n    install -d -m 0700 -o aiagent -g aiagent /var/lib/tandoor-ai\n\n# Claude's Python SDK is installed with the Python requirements. On Alpine we use\n# the official native/musl Claude Code npm package explicitly as its CLI runtime.\nRUN npm install -g @anthropic-ai/claude-code@2.1.238 && claude --version\n\nENV AI_RUNTIME_DATA_DIR=/var/lib/tandoor-ai \\\n    CLAUDE_CLI_PATH=/usr/local/bin/claude\n\n"""
if block not in text:
    text = replace_once(text, anchor, block + anchor, "Docker AI runtime block")
write(path, text)


# Serializer: runtime-aware validation + encrypted Claude setup token ----------
path = "cookbook/serializer.py"
text = read(path)
import_anchor = "from cookbook.helper.ai_helper import get_monthly_token_usage\n"
import_new = import_anchor + "from cookbook.helper.ai_runtime import (RUNTIME_CLAUDE, RUNTIME_CODEX, encrypt_subscription_token,\n                                                provider_runtime, subscription_token_present)\n"
if "encrypt_subscription_token" not in text:
    text = replace_once(text, import_anchor, import_new, "serializer runtime import")
text = replace_once(
    text,
    "class AiProviderSerializer(serializers.ModelSerializer):\n    api_key = serializers.CharField(required=False, write_only=True)\n",
    "class AiProviderSerializer(serializers.ModelSerializer):\n    api_key = serializers.CharField(required=False, write_only=True, allow_blank=True)\n",
    "serializer API key field",
)
meta_anchor = "    class Meta:\n        model = AiProvider\n        fields = ('id', 'name', 'description', 'api_key', 'model_name', 'url', 'log_credit_cost', 'space', 'created_at', 'updated_at')\n"
validate_block = """    def validate(self, attrs):\n        model_name = attrs.get('model_name', getattr(self.instance, 'model_name', ''))\n        runtime = provider_runtime(model_name)\n\n        if runtime == RUNTIME_CODEX:\n            # Codex owns its refreshable ChatGPT credential in the isolated runtime\n            # directory. Never duplicate it into the database.\n            attrs['api_key'] = ''\n            attrs['url'] = None\n            attrs['log_credit_cost'] = False\n\n        elif runtime == RUNTIME_CLAUDE:\n            token = (attrs.get('api_key') or '').strip()\n            existing_is_claude = self.instance is not None and provider_runtime(self.instance.model_name) == RUNTIME_CLAUDE\n            if token:\n                attrs['api_key'] = encrypt_subscription_token(token)\n            elif existing_is_claude and subscription_token_present(self.instance.api_key):\n                attrs.pop('api_key', None)\n            else:\n                raise ValidationError(_('A Claude setup token is required. Generate one with `claude setup-token`.'))\n            attrs['url'] = None\n            attrs['log_credit_cost'] = False\n\n        return super().validate(attrs)\n\n"""
if validate_block not in text:
    text = replace_once(text, meta_anchor, validate_block + meta_anchor, "serializer validation")
write(path, text)


# Runtime dispatcher bugfix: launch device-login worker without waiting --------
path = "cookbook/helper/ai_runtime.py"
text = read(path)
old = """    subprocess.Popen(\n        _worker_command(),\n        stdin=subprocess.PIPE,\n        stdout=subprocess.DEVNULL,\n        stderr=subprocess.DEVNULL,\n        text=True,\n        cwd=job_dir,\n        env=env,\n        start_new_session=True,\n        **_subprocess_identity_kwargs(),\n    ).communicate(json.dumps(payload), timeout=1)\n\n    # The worker writes the device code immediately, then keeps waiting detached.\n"""
new = """    proc = subprocess.Popen(\n        _worker_command(),\n        stdin=subprocess.PIPE,\n        stdout=subprocess.DEVNULL,\n        stderr=subprocess.DEVNULL,\n        text=True,\n        cwd=job_dir,\n        env=env,\n        start_new_session=True,\n        **_subprocess_identity_kwargs(),\n    )\n    if proc.stdin is None:\n        raise AiRuntimeError('Unable to start Codex login worker.')\n    proc.stdin.write(json.dumps(payload))\n    proc.stdin.close()\n\n    # The worker writes the device code immediately, then keeps waiting detached.\n"""
if old in text:
    text = text.replace(old, new, 1)
write(path, text)


# API: replace direct LiteLLM call with dispatcher -----------------------------
path = "cookbook/views/api.py"
text = read(path)
text = replace_once(text, "from litellm import completion, BadRequestError\n", "", "remove direct LiteLLM completion")
text = replace_once(text, "from litellm.exceptions import Timeout as LitellmTimeout\n", "", "remove direct LiteLLM timeout")
ai_import_anchor = "from cookbook.helper.ai_helper import can_perform_ai_request, AiCallbackHandler\n"
ai_import_new = ai_import_anchor + "from cookbook.helper.ai_runtime import (AiRuntimeBadRequest as BadRequestError, AiRuntimeTimeout as LitellmTimeout,\n                                               RUNTIME_CODEX, codex_logout, completion, provider_runtime,\n                                               provider_runtime_status, start_codex_device_login, test_provider_runtime)\n"
if "provider_runtime_status" not in text:
    text = replace_once(text, ai_import_anchor, ai_import_new, "API runtime import")

# Move provider resolution in front of the credit check for all four endpoints.
pattern = re.compile(
    r"(?P<check>            if not can_perform_ai_request\(request\.space\):\n.*?\n                return Response\([^\n]+\)\n\n)"
    r"(?P<provider>            ai_provider = AiProvider\.objects\.filter\([^\n]+\)\.first\(\)\n)",
    re.DOTALL,
)

def move_provider(match: re.Match[str]) -> str:
    check = match.group("check").replace(
        "can_perform_ai_request(request.space)",
        "can_perform_ai_request(request.space, ai_provider)",
        1,
    )
    provider = match.group("provider")
    missing = (
        "            if ai_provider is None:\n"
        "                return Response({'error': True, 'msg': _('AI provider not found.')}, status=status.HTTP_404_NOT_FOUND)\n\n"
    )
    return provider + "\n" + missing + check

text, moved = pattern.subn(move_provider, text)
if moved != 4:
    raise RuntimeError(f"API provider/credit reordering: expected 4 endpoints, changed {moved}")

# Add runtime auth/test actions to the existing provider viewset.
class_anchor = """class AiProviderViewSet(LoggingMixin, viewsets.ModelViewSet, DeleteRelationMixing):\n    queryset = AiProvider.objects\n    serializer_class = AiProviderSerializer\n    permission_classes = [CustomAiProviderPermission & CustomTokenHasReadWriteScope]\n    pagination_class = DefaultPagination\n"""
actions = """
\n    @decorators.action(detail=True, methods=['GET'])\n    def runtime_status(self, request, pk=None):\n        provider = self.get_object()\n        login_id = request.query_params.get('login_id')\n        return Response(provider_runtime_status(provider, login_id=login_id))\n\n    @decorators.action(detail=True, methods=['POST'])\n    def codex_login(self, request, pk=None):\n        provider = self.get_object()\n        if provider_runtime(provider) != RUNTIME_CODEX:\n            return Response({'error': 'This provider does not use the Codex runtime.'}, status=status.HTTP_400_BAD_REQUEST)\n        try:\n            return Response(start_codex_device_login())\n        except Exception as err:\n            traceback.print_exc()\n            return Response({'error': str(err)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)\n\n    @decorators.action(detail=True, methods=['POST'])\n    def codex_logout(self, request, pk=None):\n        provider = self.get_object()\n        if provider_runtime(provider) != RUNTIME_CODEX:\n            return Response({'error': 'This provider does not use the Codex runtime.'}, status=status.HTTP_400_BAD_REQUEST)\n        codex_logout()\n        return Response({'ok': True})\n\n    @decorators.action(detail=True, methods=['POST'])\n    def runtime_test(self, request, pk=None):\n        provider = self.get_object()\n        try:\n            return Response(test_provider_runtime(provider))\n        except (BadRequestError, LitellmTimeout) as err:\n            return Response({'error': getattr(err, 'message', str(err))}, status=status.HTTP_400_BAD_REQUEST)\n        except Exception as err:\n            traceback.print_exc()\n            return Response({'error': str(err)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)\n"""
if "def runtime_status(self, request" not in text:
    text = replace_once(text, class_anchor, class_anchor + actions, "AI provider runtime actions")
write(path, text)

print("subscription AI patch applied")
