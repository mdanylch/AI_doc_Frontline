"""
MCP server: Cisco Docs AI (streamable HTTP).

- Calls Cisco Docs AI ``/api/v1/docs/ask`` with a bearer token from the environment
  (``docai_token`` / ``DOC_AI_TOKEN``). No BDB or Duo OAuth.
- Optional HTTP gate: header ``MCP_REQUEST_HEADERS`` must match the env var of the same name.

Secrets: only ``docai_token`` (and optional ``MCP_REQUEST_HEADERS``). Never commit real values.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Self

import requests
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("cisco_ai_docs_mcp")

_DEFAULT_DOCS_AI_URL = "https://docs-ai-ext.cloudapps.cisco.com/api/v1/docs/ask"
_HTTP_TIMEOUT_SEC = 120
_MAX_ERROR_BODY_CHARS = 8000
_MAX_QUERY_SNIPPET_CHARS = 96

VOICE_REPLY_INSTRUCTION = (
    "Reply for spoken voice playback only: give exactly 3 to 4 short sentences. "
    "Use plain language. Do not include URLs, links, document titles in brackets, "
    "bullet lists, table markup, or citation blocks. End with a period when possible.\n\n"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    docai_token: str = Field(default="", validation_alias=AliasChoices("docai_token", "DOC_AI_TOKEN"))

    docs_ai_ask_url: str = Field(
        default=_DEFAULT_DOCS_AI_URL,
        validation_alias=AliasChoices("DOCS_AI_ASK_URL", "docs_ai_ask_url"),
    )

    @model_validator(mode="before")
    @classmethod
    def _merge_docai_token_aliases(cls, data: Any) -> dict[str, Any]:
        merged: dict[str, Any] = dict(data) if isinstance(data, dict) else {}

        def pick(*candidates: str) -> str | None:
            for key in candidates:
                if key in merged:
                    val = merged.get(key)
                    if val is not None and str(val).strip() != "":
                        return str(val).strip()
            for key in candidates:
                val = os.environ.get(key)
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
            upper_map = {k.upper(): v for k, v in os.environ.items()}
            for key in candidates:
                val = upper_map.get(key.upper())
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
            return None

        tok = pick("docai_token", "DOC_AI_TOKEN")
        if tok:
            merged["docai_token"] = tok

        return merged

    @model_validator(mode="after")
    def _docai_token_required(self) -> Self:
        if not (self.docai_token or "").strip():
            raise ValueError(
                "Missing Docs AI token. Set docai_token or DOC_AI_TOKEN "
                "(environment variables or a .env file in this directory)."
            )
        return self

    mcp_request_headers: str | None = Field(
        default=None,
        validation_alias="MCP_REQUEST_HEADERS",
    )

    http_ssl_verify: bool = Field(default=True, validation_alias="HTTP_SSL_VERIFY")
    ssl_ca_bundle: str | None = Field(default=None, validation_alias="SSL_CA_BUNDLE")

    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8080, validation_alias="PORT")
    mcp_path: str = Field(default="/mcp", validation_alias="MCP_PATH")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_query_snippet: bool = Field(default=True, validation_alias="LOG_QUERY_SNIPPET")

    voice_optimized: bool = Field(default=True, validation_alias="VOICE_OPTIMIZED")
    voice_max_sentences: int = Field(default=4, ge=1, le=10, validation_alias="VOICE_MAX_SENTENCES")

    @field_validator("http_ssl_verify", mode="before")
    @classmethod
    def _coerce_http_ssl_verify(cls, v: object) -> bool:
        if v is None or v == "":
            return True
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)


_settings_cache: Settings | None = None


def get_settings() -> Settings:
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = Settings()  # type: ignore[call-arg]
    return _settings_cache


def _configure_logging(settings: Settings) -> None:
    level_name = (settings.log_level or "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"),
        )
        root.addHandler(handler)

    logging.getLogger("urllib3").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("mcp").setLevel(level)


def _sanitize_one_line(s: str, max_len: int = 400) -> str:
    if not s:
        return ""
    out = s.replace("\r", " ").replace("\n", " ").strip()
    if len(out) > max_len:
        return out[:max_len] + "..."
    return out


def _tool_result(payload: object) -> ToolResult:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return ToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content={"result": body},
    )


CISCO_DOCS_QUERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "JSON string: Docs AI answer (voice-sanitized when enabled), confidence, optional fields.",
        },
    },
    "required": ["result"],
}


def _requests_verify(settings: Settings) -> bool | str:
    if not settings.http_ssl_verify:
        return False
    if settings.ssl_ca_bundle:
        return settings.ssl_ca_bundle
    return True


def _http_outcome_from_status(status_code: int | None) -> str:
    if status_code is None:
        return "unknown"
    if status_code < 400:
        return "success"
    if status_code < 500:
        return "client_error"
    return "server_error"


def _strip_markdown_noise(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<https?://[^>\s]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\bwww\.[^\s)]+", "", text)
    text = re.sub(r"`+", "", text)
    lines_out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("sources:") or low.startswith("**sources"):
            break
        if re.match(r"^[\-\*\u2022]\s+", s):
            s = re.sub(r"^[\-\*\u2022]\s+", "", s)
        lines_out.append(s)
    text = " ".join(lines_out)
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _format_answer_for_voice(text: str, max_sentences: int) -> str:
    text = _strip_markdown_noise(text)
    chunks = re.split(r"(?<=[.!?])\s+", text)
    sentences = [c.strip() for c in chunks if c and c.strip()]
    if not sentences:
        return text[:500].strip()
    return " ".join(sentences[:max_sentences]).strip()


def _docs_ai_post(
    settings: Settings,
    *,
    question: str,
    correlation_id: str,
) -> requests.Response:
    url = (settings.docs_ai_ask_url or "").strip() or _DEFAULT_DOCS_AI_URL
    safe_url = _sanitize_one_line(url, 500)
    logger.info(
        "app_event kind=outbound_http phase=start purpose=DocsAI_ask method=POST url=%s correlation_id=%s",
        safe_url,
        correlation_id,
    )
    t0 = time.perf_counter()
    try:
        r = requests.post(
            url,
            json={"question": question},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {(settings.docai_token or '').strip()}",
            },
            timeout=_HTTP_TIMEOUT_SEC,
            verify=_requests_verify(settings),
        )
    except requests.RequestException as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning(
            "app_event kind=outbound_http phase=complete purpose=DocsAI_ask outcome=network_error "
            "elapsed_ms=%s correlation_id=%s error=%s",
            elapsed_ms,
            correlation_id,
            _sanitize_one_line(str(e), 240),
        )
        raise
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    oc = _http_outcome_from_status(r.status_code)
    logger.info(
        "app_event kind=outbound_http phase=complete purpose=DocsAI_ask outcome=%s http_status=%s "
        "http_outcome=%s elapsed_ms=%s correlation_id=%s response_bytes=%s",
        "http_success" if r.ok else "http_non_success",
        r.status_code,
        oc,
        elapsed_ms,
        correlation_id,
        len(r.content or b""),
    )
    return r


mcp = FastMCP(name="cisco-ai-docs")


@mcp.tool(output_schema=CISCO_DOCS_QUERY_OUTPUT_SCHEMA)
def cisco_docs_query(query: str) -> ToolResult:
    """
    Query Cisco documentation via Docs AI (voice-friendly summary by default).

    Args:
        query: Question from the Webex AI Agent / MCP client.

    Returns:
        MCP tool result with ``structuredContent.result`` (JSON string) per WxCC output schema.
    """
    settings = get_settings()
    correlation_id = str(uuid.uuid4())
    q = (query or "").strip()
    snippet = ""
    if settings.log_query_snippet and q:
        snippet = _sanitize_one_line(q, _MAX_QUERY_SNIPPET_CHARS)
    logger.info(
        "tool_cisco_docs_query_begin correlation_id=%s query_len=%s snippet=%s",
        correlation_id,
        len(q),
        snippet if snippet else "(empty)",
    )
    if not q:
        logger.info(
            "tool_cisco_docs_query_reject correlation_id=%s reason=empty_query",
            correlation_id,
        )
        return _tool_result({"error": "query must not be empty"})

    api_question = f"{VOICE_REPLY_INSTRUCTION}{q}" if settings.voice_optimized else q
    max_retries = 3

    for attempt in range(max_retries):
        try:
            r = _docs_ai_post(settings, question=api_question, correlation_id=correlation_id)
            if r.status_code == 429 and attempt < max_retries - 1:
                wait_s = 2**attempt
                logger.warning(
                    "docs_ai_429_retry correlation_id=%s attempt=%s wait_s=%s",
                    correlation_id,
                    attempt + 1,
                    wait_s,
                )
                time.sleep(wait_s)
                continue
            r.raise_for_status()
            try:
                payload = r.json()
            except ValueError:
                return _tool_result(
                    {"error": "Docs AI returned non-JSON", "raw": (r.text or "")[:_MAX_ERROR_BODY_CHARS]}
                )

            raw_answer = payload.get("answer", "")
            if not isinstance(raw_answer, str):
                raw_answer = str(raw_answer) if raw_answer is not None else ""

            confidence = payload.get("confidence", 0.0)
            try:
                confidence_f = float(confidence)
            except (TypeError, ValueError):
                confidence_f = 0.0

            if settings.voice_optimized:
                spoken = _format_answer_for_voice(raw_answer, settings.voice_max_sentences)
                out: dict[str, Any] = {
                    "answer": spoken,
                    "confidence": confidence_f,
                    "voice_optimized": True,
                }
            else:
                out = {
                    "answer": raw_answer,
                    "confidence": confidence_f,
                    "sources": payload.get("sources", []),
                    "voice_optimized": False,
                }

            logger.info(
                "tool_cisco_docs_query_success correlation_id=%s confidence=%s voice=%s",
                correlation_id,
                confidence_f,
                settings.voice_optimized,
            )
            return _tool_result(out)

        except requests.HTTPError as e:
            text = ""
            if e.response is not None:
                text = (e.response.text or "")[:_MAX_ERROR_BODY_CHARS]
            sc = getattr(e.response, "status_code", None) if e.response is not None else None
            logger.warning(
                "tool_cisco_docs_query_http correlation_id=%s status=%s",
                correlation_id,
                sc,
            )
            return _tool_result(
                {
                    "error": "Docs AI HTTP error",
                    "status_code": sc,
                    "detail": text,
                }
            )
        except requests.RequestException as e:
            logger.warning(
                "tool_cisco_docs_query_transport correlation_id=%s error=%s",
                correlation_id,
                _sanitize_one_line(str(e), 300),
            )
            return _tool_result({"error": "Docs AI request failed", "message": str(e)})

    return _tool_result({"error": "Docs AI rate limited; retries exhausted"})


class HttpAccessLogMiddleware(BaseHTTPMiddleware):
    """Log each HTTP request: method, path, client, status, duration (no secrets)."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        rid = (
            request.headers.get("x-request-id")
            or request.headers.get("X-Request-ID")
            or str(uuid.uuid4())
        )
        request.state.request_id = rid
        client_host = request.client.host if request.client else ""
        path_qs = request.url.path
        if request.url.query:
            path_qs = f"{path_qs}?{_sanitize_one_line(request.url.query, 200)}"
        ua = _sanitize_one_line(request.headers.get("user-agent") or "", 160)
        logger.info(
            "app_event kind=inbound_http phase=start request_id=%s method=%s path=%s client=%s user_agent=%s",
            rid,
            request.method,
            path_qs,
            client_host,
            ua if ua else "-",
        )
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.exception(
                "http_request_exception request_id=%s elapsed_ms=%s path=%s",
                rid,
                elapsed_ms,
                path_qs,
            )
            raise
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        raw_status = getattr(response, "status_code", None)
        status_code = int(raw_status) if raw_status is not None else 0
        oc = _http_outcome_from_status(status_code if status_code else None)
        outcome = "success" if raw_status is not None and raw_status < 400 else "failed"
        logger.info(
            "app_event kind=inbound_http phase=complete request_id=%s method=%s path=%s "
            "outcome=%s http_status=%s http_outcome=%s elapsed_ms=%s",
            rid,
            request.method,
            path_qs,
            outcome,
            raw_status if raw_status is not None else "?",
            oc,
            elapsed_ms,
        )
        return response


class MCPRequestHeadersAuthMiddleware(BaseHTTPMiddleware):
    """Require header ``MCP_REQUEST_HEADERS`` to match configured secret (timing-safe)."""

    def __init__(self, app, *, expected: str):
        super().__init__(app)
        self._expected = expected

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path.rstrip("/") or ""
        rid = getattr(request.state, "request_id", None) or "-"
        client_host = request.client.host if request.client else ""
        if path == "/health":
            return await call_next(request)
        got = request.headers.get("MCP_REQUEST_HEADERS") or ""
        if not hmac.compare_digest(got.encode("utf-8"), self._expected.encode("utf-8")):
            logger.warning(
                "app_event kind=inbound_auth outcome=denied request_id=%s path=%s client=%s "
                "header_mcp_present=%s",
                rid,
                request.url.path,
                client_host,
                bool(got),
            )
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        logger.debug(
            "app_event kind=inbound_auth outcome=allowed request_id=%s path=%s client=%s",
            rid,
            request.url.path,
            client_host,
        )
        return await call_next(request)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "cisco-ai-docs-mcp"})


def main() -> None:
    print("[cisco-ai-docs-mcp] process start", file=sys.stderr, flush=True)
    settings = get_settings()
    _configure_logging(settings)

    middleware_list: list[Middleware] = []
    exp = (settings.mcp_request_headers or "").strip()
    if exp:
        middleware_list.append(
            Middleware(MCPRequestHeadersAuthMiddleware, expected=exp),
        )
        logger.info("HTTP authentication enabled (header MCP_REQUEST_HEADERS secret configured)")
    else:
        logger.warning(
            "MCP_REQUEST_HEADERS is not set — HTTP requests are not authenticated. "
            "Configure it on AWS App Runner for production."
        )

    path = settings.mcp_path.strip() or "/mcp"
    if not path.startswith("/"):
        path = "/" + path

    middleware_list.append(Middleware(HttpAccessLogMiddleware))

    logger.info(
        "startup service=cisco-ai-docs-mcp host=%s port=%s mcp_path=%s log_level=%s "
        "ssl_verify=%s log_query_snippet=%s docs_ai_url=%s voice_optimized=%s",
        settings.host,
        settings.port,
        path,
        settings.log_level,
        settings.http_ssl_verify,
        settings.log_query_snippet,
        _sanitize_one_line(settings.docs_ai_ask_url, 120),
        settings.voice_optimized,
    )

    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        path=path,
        middleware=middleware_list,
        uvicorn_config={
            "access_log": False,
            "log_level": (settings.log_level or "info").lower(),
        },
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
