"""AI runtime dispatch for Tandoor.

LiteLLM remains the default and is passed through unchanged. Model names with
``codex/`` or ``claude-code/`` prefixes are executed by a deliberately isolated
worker process so subscription credentials never need to become API keys and the
agent runtimes do not inherit Tandoor's database/application environment.

The subscription runtimes intentionally accept the same message shape Tandoor
already sends to LiteLLM, including inline image/PDF data URLs. Attachments are
validated here, then materialized only inside a private one-shot worker directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from litellm import BadRequestError as LiteLLMBadRequest
from litellm.exceptions import Timeout as LiteLLMTimeout


RUNTIME_LITELLM = "litellm"
RUNTIME_CODEX = "codex"
RUNTIME_CLAUDE = "claude-code"
SUBSCRIPTION_RUNTIMES = {RUNTIME_CODEX, RUNTIME_CLAUDE}
TOKEN_PREFIX = "tandoor-ai:v1:"
DEFAULT_TIMEOUT_SECONDS = 180
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 40 * 1024 * 1024


class AiRuntimeError(RuntimeError):
    pass


class AiRuntimeBadRequest(AiRuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AiRuntimeTimeout(AiRuntimeError):
    pass


def provider_runtime(provider_or_model: Any) -> str:
    model = getattr(provider_or_model, "model_name", provider_or_model) or ""
    model = str(model)
    if model.startswith("codex/"):
        return RUNTIME_CODEX
    if model.startswith("claude-code/"):
        return RUNTIME_CLAUDE
    return RUNTIME_LITELLM


def is_subscription_provider(provider_or_model: Any) -> bool:
    return provider_runtime(provider_or_model) in SUBSCRIPTION_RUNTIMES


def runtime_model(provider_or_model: Any) -> str | None:
    model = str(getattr(provider_or_model, "model_name", provider_or_model) or "")
    runtime = provider_runtime(model)
    if runtime == RUNTIME_LITELLM:
        return model or None
    value = model.split("/", 1)[1].strip() if "/" in model else ""
    return None if value in ("", "default") else value


def _fernet() -> Fernet:
    secret = str(settings.SECRET_KEY).encode("utf-8")
    digest = hashlib.sha256(b"tandoor-subscription-ai-v1\0" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_subscription_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if token.startswith(TOKEN_PREFIX):
        return token
    return TOKEN_PREFIX + _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_subscription_token(value: str | None) -> str | None:
    value = str(value or "")
    if not value:
        return None
    if not value.startswith(TOKEN_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(TOKEN_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def subscription_token_present(value: str | None) -> bool:
    return bool(decrypt_subscription_token(value))


def runtime_data_dir() -> Path:
    return Path(os.environ.get("AI_RUNTIME_DATA_DIR", "/var/lib/tandoor-ai"))


def codex_home() -> Path:
    return runtime_data_dir() / "codex"


def codex_auth_file() -> Path:
    return codex_home() / "auth.json"


_CODEX_CONFIG = """# Managed by Tandoor Recipes. Keep this runtime isolated from the host.
cli_auth_credentials_store = "file"
allow_login_shell = false
approval_policy = "never"
web_search = "disabled"
default_permissions = "tandoor"

[features]
auth_elicitation = false
shell_tool = false
unified_exec = false
shell_snapshot = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
in_app_browser = false
computer_use = false
apps = false
plugins = false
plugin_sharing = false
remote_plugin = false
multi_agent = false
skill_search = false
skill_mcp_dependency_install = false
workspace_dependencies = false
image_generation = false
hooks = false
code_mode_host = false
tool_suggest = false

[agents]
enabled = false

[permissions.tandoor.filesystem]
":root" = "deny"
":minimal" = "read"

[permissions.tandoor.filesystem.":workspace_roots"]
"." = "read"

[permissions.tandoor.network]
enabled = false
"""


def _agent_ids() -> tuple[int, int] | None:
    if os.name != "posix" or os.geteuid() != 0:
        return None
    try:
        info = pwd.getpwnam(os.environ.get("AI_RUNTIME_USER", "aiagent"))
        return info.pw_uid, info.pw_gid
    except KeyError:
        return None


def _ensure_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:
        pass
    ids = _agent_ids()
    if ids:
        try:
            os.chown(path, *ids)
        except OSError:
            pass
    return path


def ensure_runtime_layout() -> None:
    root = _ensure_dir(runtime_data_dir())
    _ensure_dir(root / "jobs")
    home = _ensure_dir(codex_home())
    config = home / "config.toml"
    if not config.exists() or config.read_text(encoding="utf-8") != _CODEX_CONFIG:
        tmp = config.with_suffix(".tmp")
        tmp.write_text(_CODEX_CONFIG, encoding="utf-8")
        os.replace(tmp, config)
    try:
        config.chmod(0o600)
    except OSError:
        pass
    ids = _agent_ids()
    if ids:
        try:
            os.chown(config, *ids)
        except OSError:
            pass


def codex_connected() -> bool:
    ensure_runtime_layout()
    try:
        return codex_auth_file().is_file() and codex_auth_file().stat().st_size > 0
    except OSError:
        return False


def _decode_data_url(value: Any) -> dict[str, str]:
    """Validate a LiteLLM-style inline attachment and return a compact worker payload."""
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.startswith("data:"):
        raise AiRuntimeBadRequest(
            "Codex/Claude attachment inputs must be inline data URLs; remote URLs are not fetched by the agent runtime."
        )
    try:
        header, encoded = value.split(",", 1)
        metadata = header[5:].split(";")
        mime_type = (metadata[0] or "application/octet-stream").lower()
        if "base64" not in metadata[1:]:
            raise ValueError("attachment is not base64 encoded")
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise AiRuntimeBadRequest("The AI attachment is not a valid base64 data URL.") from exc

    if not (mime_type.startswith("image/") or mime_type == "application/pdf"):
        raise AiRuntimeBadRequest(
            f"Unsupported AI attachment type: {mime_type}. Codex/Claude currently accept images and PDFs."
        )
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AiRuntimeBadRequest(
            f"AI attachment is too large ({len(raw)} bytes); limit is {MAX_ATTACHMENT_BYTES} bytes per file."
        )
    return {"mime_type": mime_type, "data": encoded, "size": str(len(raw))}


def _normalize_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Flatten roles/text while preserving multimodal attachments for the isolated worker."""
    parts: list[str] = []
    attachments: list[dict[str, str]] = []
    total_bytes = 0

    for message in messages or []:
        role = str(message.get("role", "user")).upper()
        content = message.get("content", "")
        chunks: list[str] = []

        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type in {"text", "input_text"}:
                    chunks.append(str(item.get("text", "")))
                elif item_type in {"image_url", "input_image", "image"}:
                    source = item.get("image_url", item.get("url", item.get("source")))
                    attachment = _decode_data_url(source)
                    total_bytes += int(attachment["size"])
                    attachment["index"] = str(len(attachments) + 1)
                    attachments.append(attachment)
                    chunks.append(f"[ATTACHMENT {attachment['index']}: {attachment['mime_type']}]")
                elif item_type in {"file", "input_file"}:
                    source = item.get("file_data", item.get("data", item.get("url")))
                    attachment = _decode_data_url(source)
                    total_bytes += int(attachment["size"])
                    attachment["index"] = str(len(attachments) + 1)
                    attachments.append(attachment)
                    chunks.append(f"[ATTACHMENT {attachment['index']}: {attachment['mime_type']}]")

        parts.append(f"[{role}]\n" + "\n".join(chunks))

    if len(attachments) > MAX_ATTACHMENTS:
        raise AiRuntimeBadRequest(f"Too many AI attachments ({len(attachments)}); limit is {MAX_ATTACHMENTS}.")
    if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        raise AiRuntimeBadRequest(
            f"AI attachments are too large in total ({total_bytes} bytes); limit is {MAX_TOTAL_ATTACHMENT_BYTES} bytes."
        )

    return "\n\n".join(parts).strip(), attachments


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Backward-compatible text-only view used by older tests/callers."""
    return _normalize_messages(messages)[0]


def _json_schema(response_format: Any) -> dict[str, Any]:
    if isinstance(response_format, dict):
        if response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema", {}).get("schema") or response_format.get("schema")
            if isinstance(schema, dict):
                return schema
    return {"type": "object", "additionalProperties": True}


def _worker_base_env(job_dir: str) -> dict[str, str]:
    ensure_runtime_layout()
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": job_dir,
        "TMPDIR": job_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "AI_RUNTIME_DATA_DIR": str(runtime_data_dir()),
        "AI_RUNTIME_MAX_PDF_PAGES": os.environ.get("AI_RUNTIME_MAX_PDF_PAGES", "20"),
        "CODEX_HOME": str(codex_home()),
        "CLAUDE_CONFIG_DIR": os.path.join(job_dir, ".claude"),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "USE_BUILTIN_RIPGREP": "0",
        "CLAUDE_CLI_PATH": os.environ.get("CLAUDE_CLI_PATH", "/usr/local/bin/claude"),
    }


def _make_job_dir() -> str:
    ensure_runtime_layout()
    path = tempfile.mkdtemp(prefix="job-", dir=runtime_data_dir() / "jobs")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    ids = _agent_ids()
    if ids:
        try:
            os.chown(path, *ids)
        except OSError:
            pass
    return path


def _worker_command() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("ai_runtime_worker.py"))]


def _subprocess_identity_kwargs() -> dict[str, Any]:
    ids = _agent_ids()
    if not ids:
        return {}
    uid, gid = ids
    return {"user": uid, "group": gid, "extra_groups": []}


def _invoke_worker(payload: dict[str, Any], runtime: str, credential: str | None = None,
                   timeout: int | None = None) -> dict[str, Any]:
    job_dir = _make_job_dir()
    env = _worker_base_env(job_dir)
    if runtime == RUNTIME_CLAUDE and credential:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
    timeout = timeout or int(os.environ.get("AI_RUNTIME_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    try:
        completed = subprocess.run(
            _worker_command(),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=job_dir,
            env=env,
            timeout=timeout,
            check=False,
            **_subprocess_identity_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AiRuntimeTimeout("The subscription AI runtime timed out.") from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "AI runtime failed").strip()
        raise AiRuntimeBadRequest(detail[-4000:])
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AiRuntimeBadRequest("AI runtime returned an invalid response.") from exc
    if not result.get("ok"):
        raise AiRuntimeBadRequest(str(result.get("error") or "AI runtime failed"))
    return result


class AiRuntimeResponse(dict):
    """Small LiteLLM-shaped response used by the existing Tandoor endpoints."""

    def __init__(self, content: str, usage: dict[str, int] | None = None):
        usage = usage or {"prompt_tokens": 0, "completion_tokens": 0}
        super().__init__(usage=usage)
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


def _log_subscription_result(response: AiRuntimeResponse, started: datetime, ended: datetime) -> None:
    for callback in list(getattr(litellm, "callbacks", []) or []):
        create_log = getattr(callback, "create_ai_log", None)
        if callable(create_log):
            create_log({"response_cost": 0}, response, started, ended)


def completion(**kwargs: Any):
    model = kwargs.get("model", "")
    runtime = provider_runtime(model)
    if runtime == RUNTIME_LITELLM:
        try:
            return litellm.completion(**kwargs)
        except LiteLLMTimeout as exc:
            raise AiRuntimeTimeout(str(exc)) from exc
        except LiteLLMBadRequest as exc:
            raise AiRuntimeBadRequest(getattr(exc, "message", str(exc))) from exc

    prompt, attachments = _normalize_messages(kwargs.get("messages") or [])
    if not prompt and not attachments:
        raise AiRuntimeBadRequest("The AI request contained no prompt or attachment.")

    credential = None
    if runtime == RUNTIME_CLAUDE:
        credential = decrypt_subscription_token(kwargs.get("api_key"))
        if not credential:
            raise AiRuntimeBadRequest("This Claude Code provider does not have a valid setup token.")
    elif runtime == RUNTIME_CODEX and not codex_connected():
        raise AiRuntimeBadRequest("Codex is not signed in. Open the provider and sign in with ChatGPT first.")

    payload = {
        "action": "complete",
        "runtime": runtime,
        "model": runtime_model(model),
        "prompt": prompt,
        "attachments": attachments,
        "schema": _json_schema(kwargs.get("response_format")),
    }
    started = datetime.now().astimezone()
    result = _invoke_worker(payload, runtime, credential=credential)
    ended = datetime.now().astimezone()
    response = AiRuntimeResponse(
        json.dumps(result.get("data"), ensure_ascii=False),
        usage={
            "prompt_tokens": int((result.get("usage") or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((result.get("usage") or {}).get("completion_tokens") or 0),
        },
    )
    _log_subscription_result(response, started, ended)
    return response


def provider_runtime_status(provider, login_id: str | None = None) -> dict[str, Any]:
    runtime = provider_runtime(provider)
    result: dict[str, Any] = {"runtime": runtime, "connected": True}
    if runtime == RUNTIME_CODEX:
        result["connected"] = codex_connected()
        if login_id:
            state = runtime_data_dir() / "logins" / f"{login_id}.json"
            try:
                login = json.loads(state.read_text(encoding="utf-8"))
                result["login"] = {
                    k: login.get(k)
                    for k in ("id", "status", "verification_url", "user_code", "error")
                    if login.get(k) is not None
                }
            except (OSError, json.JSONDecodeError):
                result["login"] = {"id": login_id, "status": "starting"}
    elif runtime == RUNTIME_CLAUDE:
        result["connected"] = subscription_token_present(provider.api_key)
    return result


def start_codex_device_login() -> dict[str, Any]:
    ensure_runtime_layout()
    login_id = uuid.uuid4().hex
    login_dir = _ensure_dir(runtime_data_dir() / "logins")
    state_file = login_dir / f"{login_id}.json"
    job_dir = _make_job_dir()
    env = _worker_base_env(job_dir)
    payload = {
        "action": "codex_login",
        "login_id": login_id,
        "status_file": str(state_file),
        "job_dir": job_dir,
    }
    proc = subprocess.Popen(
        _worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=job_dir,
        env=env,
        start_new_session=True,
        **_subprocess_identity_kwargs(),
    )
    if proc.stdin is None:
        raise AiRuntimeError("Unable to start Codex login worker.")
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return {
                k: state.get(k)
                for k in ("id", "status", "verification_url", "user_code", "error")
                if state.get(k) is not None
            }
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
    return {"id": login_id, "status": "starting"}


def codex_logout() -> None:
    ensure_runtime_layout()
    try:
        codex_auth_file().unlink()
    except FileNotFoundError:
        pass


def test_provider_runtime(provider) -> dict[str, Any]:
    response = completion(
        api_key=provider.api_key,
        model=provider.model_name,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "Return only this JSON object: {\"ok\": true}"}],
        }],
        **({"api_base": provider.url} if provider_runtime(provider) == RUNTIME_LITELLM and provider.url else {}),
    )
    parsed = json.loads(response.choices[0].message.content)
    return {"ok": parsed.get("ok") is True}
