"""
MCP server: Cisco docs via BDB script job (Mykola_Cisco_Docs).

- Duo OAuth client_credentials token is fetched fresh on each tool invocation (each docs query).
- HTTP clients must send header ``MCP_REQUEST_HEADERS`` matching the env var of the same name
  when that env var is set (AWS App Runner shared secret pattern).

Secrets come only from environment (see aliases below, including App Runner names
``client_id`` / ``client_secret``). Never commit real values.
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import time
import uuid
from typing import Any

import requests
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Stable name so CloudWatch Logs Insights can filter `@message like /cisco_ai_docs_mcp/`
# regardless of whether the app is run as `python mcp_server.py` (__main__) or as a module.
logger = logging.getLogger("cisco_ai_docs_mcp")

_DEFAULT_TOKEN_URL = (
    "https://sso-dbbfec7f.sso.duosecurity.com/oauth/DID1LHEMWQZDEGZ7FAXX/token"
)
_DEFAULT_SCRIPT_URL = "https://scripts.cisco.com/api/v2/jobs/Mykola_Cisco_Docs"

_HTTP_TIMEOUT_SEC = 120
_MAX_ERROR_BODY_CHARS = 8000
_MAX_QUERY_SNIPPET_CHARS = 96


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id_bdb: str = Field(
        validation_alias=AliasChoices(
            "CLIENT_ID_BDB",
            "CLIENT_ID",
            "client_id",
        ),
    )
    client_secret_bdb: str = Field(
        validation_alias=AliasChoices(
            "CLIENT_SECRET_BDB",
            "CLIENT_SECRET",
            "client_secret",
        ),
    )

    bdb_token_url: str = Field(default=_DEFAULT_TOKEN_URL, validation_alias="BDB_TOKEN_URL")
    cisco_script_job_url: str = Field(
        default=_DEFAULT_SCRIPT_URL,
        validation_alias="CISCO_SCRIPT_JOB_URL",
    )

    #: When set, every HTTP request must include header ``MCP_REQUEST_HEADERS`` with this exact value.
    mcp_request_headers: str | None = Field(
        default=None,
        validation_alias="MCP_REQUEST_HEADERS",
    )

    http_ssl_verify: bool = Field(default=True, validation_alias="HTTP_SSL_VERIFY")
    ssl_ca_bundle: str | None = Field(default=None, validation_alias="SSL_CA_BUNDLE")

    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8080, validation_alias="PORT")
    mcp_path: str = Field(default="/mcp", validation_alias="MCP_PATH")

    #: Logging: DEBUG, INFO, WARNING, ERROR
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    #: If true, log a short sanitized prefix of each docs query (no secrets).
    log_query_snippet: bool = Field(default=True, validation_alias="LOG_QUERY_SNIPPET")

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
    """Stderr logging for App Runner / CloudWatch (common convention); levels from LOG_LEVEL."""
    level_name = (settings.log_level or "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            )
        )
        root.addHandler(handler)

    # Third-party noise reduction unless DEBUG
    logging.getLogger("urllib3").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("mcp").setLevel(level)


def _sanitize_one_line(s: str, max_len: int = 400) -> str:
    """Strip control chars / newlines for safe single-line logs (log injection defense)."""
    if not s:
        return ""
    out = s.replace("\r", " ").replace("\n", " ").strip()
    if len(out) > max_len:
        return out[:max_len] + "..."
    return out


def _tool_result(payload: object) -> ToolResult:
    """
    WxCC activity-service validates ``structuredContent.result``. A plain dict return is not
    always wired the same as ``CallToolResult.structuredContent`` on all MCP clients.
    Returning ``ToolResult`` sets both **content** (text) and **structured_content** explicitly.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return ToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content={"result": body},
    )


# Advertised in tools/list so WxCC validation matches runtime output.
CISCO_DOCS_QUERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "JSON-encoded docs payload or error object from Cisco script job.",
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


def fetch_client_credentials_token(settings: Settings, *, correlation_id: str) -> str:
    """OAuth 2.0 client_credentials — new access token for this request."""
    logger.info(
        "oauth_token_begin correlation_id=%s token_url=%s",
        correlation_id,
        _sanitize_one_line(settings.bdb_token_url, 300),
    )
    t0 = time.perf_counter()
    data = {
        "grant_type": "client_credentials",
        "client_id": settings.client_id_bdb,
        "client_secret": settings.client_secret_bdb,
    }
    try:
        r = requests.post(
            settings.bdb_token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT_SEC,
            verify=_requests_verify(settings),
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "oauth_token_http correlation_id=%s status=%s elapsed_ms=%s",
            correlation_id,
            r.status_code,
            elapsed_ms,
        )
        r.raise_for_status()
        payload = r.json()
        token = payload.get("access_token")
        if not token or not isinstance(token, str):
            logger.error(
                "oauth_token_missing_field correlation_id=%s keys=%s",
                correlation_id,
                list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
            )
            raise RuntimeError("Token response missing access_token")
        logger.info(
            "oauth_token_ok correlation_id=%s elapsed_ms=%s access_token_chars=%s",
            correlation_id,
            elapsed_ms,
            len(token),
        )
        return token
    except requests.HTTPError as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        detail = ""
        if e.response is not None:
            detail = _sanitize_one_line((e.response.text or "")[:500])
        logger.warning(
            "oauth_token_http_error correlation_id=%s status=%s elapsed_ms=%s detail=%s",
            correlation_id,
            e.response.status_code if e.response else None,
            elapsed_ms,
            detail,
        )
        raise
    except requests.RequestException as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning(
            "oauth_token_request_error correlation_id=%s elapsed_ms=%s error=%s",
            correlation_id,
            elapsed_ms,
            _sanitize_one_line(str(e), 300),
        )
        raise


mcp = FastMCP(name="cisco-ai-docs")


@mcp.tool(output_schema=CISCO_DOCS_QUERY_OUTPUT_SCHEMA)
def cisco_docs_query(query: str) -> ToolResult:
    """
    Query Cisco documentation using the Mykola_Cisco_Docs BDB script job.

    Args:
        query: Question or search terms from the MCP client.

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

    try:
        token = fetch_client_credentials_token(settings, correlation_id=correlation_id)
    except requests.HTTPError as e:
        text = ""
        if e.response is not None:
            text = (e.response.text or "")[:_MAX_ERROR_BODY_CHARS]
        sc = getattr(e.response, "status_code", None) if e.response is not None else None
        logger.warning(
            "tool_cisco_docs_query_oauth_failed correlation_id=%s status=%s",
            correlation_id,
            sc,
        )
        return _tool_result(
            {
                "error": "Failed to obtain OAuth token",
                "status_code": sc,
                "detail": text,
            }
        )
    except requests.RequestException as e:
        logger.warning(
            "tool_cisco_docs_query_oauth_transport correlation_id=%s error=%s",
            correlation_id,
            _sanitize_one_line(str(e), 300),
        )
        return _tool_result({"error": "OAuth token request failed", "message": str(e)})
    except RuntimeError as e:
        logger.error(
            "tool_cisco_docs_query_oauth_runtime correlation_id=%s error=%s",
            correlation_id,
            _sanitize_one_line(str(e), 300),
        )
        return _tool_result({"error": str(e)})

    body = {"dev": "true", "input": {"query": q}}

    t_script = time.perf_counter()
    try:
        logger.info(
            "cisco_script_request_begin correlation_id=%s url=%s",
            correlation_id,
            _sanitize_one_line(settings.cisco_script_job_url, 300),
        )
        t0 = time.perf_counter()
        r = requests.post(
            settings.cisco_script_job_url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=_HTTP_TIMEOUT_SEC,
            verify=_requests_verify(settings),
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        logger.info(
            "cisco_script_response correlation_id=%s status=%s elapsed_ms=%s content_type=%s body_chars=%s",
            correlation_id,
            r.status_code,
            elapsed_ms,
            _sanitize_one_line(ct, 80),
            len(r.text or ""),
        )
        r.raise_for_status()
        try:
            logger.info(
                "tool_cisco_docs_query_success correlation_id=%s elapsed_ms=%s result=json",
                correlation_id,
                elapsed_ms,
            )
            return _tool_result(r.json())
        except ValueError:
            logger.info(
                "tool_cisco_docs_query_success correlation_id=%s elapsed_ms=%s result=non_json_text",
                correlation_id,
                elapsed_ms,
            )
            return _tool_result({"raw_text": (r.text or "")[:_MAX_ERROR_BODY_CHARS]})

    except requests.HTTPError as e:
        text = ""
        if e.response is not None:
            text = (e.response.text or "")[:_MAX_ERROR_BODY_CHARS]
        elapsed_ms = int((time.perf_counter() - t_script) * 1000)
        sc = getattr(e.response, "status_code", None) if e.response is not None else None
        logger.warning(
            "cisco_script_http_error correlation_id=%s status=%s elapsed_ms=%s detail_prefix=%s",
            correlation_id,
            sc,
            elapsed_ms,
            _sanitize_one_line(text, 120),
        )
        if sc == 403:
            logger.warning(
                "cisco_script_403_hint correlation_id=%s "
                "upstream may block cloud egress or deny token/job scope; check Cisco scripts access",
                correlation_id,
            )
        return _tool_result(
            {
                "error": "Cisco script job HTTP error",
                "status_code": sc,
                "detail": text,
            }
        )
    except requests.RequestException as e:
        elapsed_ms = int((time.perf_counter() - t_script) * 1000)
        logger.warning(
            "cisco_script_transport_error correlation_id=%s elapsed_ms=%s error=%s",
            correlation_id,
            elapsed_ms,
            _sanitize_one_line(str(e), 300),
        )
        return _tool_result({"error": "Cisco script request failed", "message": str(e)})


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
            "http_request_begin request_id=%s method=%s path=%s client=%s user_agent=%s",
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
        logger.info(
            "http_request_end request_id=%s status=%s elapsed_ms=%s path=%s",
            rid,
            getattr(response, "status_code", "?"),
            elapsed_ms,
            path_qs,
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
                "http_auth_failed request_id=%s path=%s client=%s header_mcp_present=%s",
                rid,
                request.url.path,
                client_host,
                bool(got),
            )
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        logger.debug(
            "http_auth_ok request_id=%s path=%s client=%s",
            rid,
            request.url.path,
            client_host,
        )
        return await call_next(request)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "cisco-ai-docs-mcp"})


def main() -> None:
    # Banner on stderr so container log collectors pick it up (same stream as logging).
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

    #: Outermost middleware last: access log wraps auth + MCP routes.
    middleware_list.append(Middleware(HttpAccessLogMiddleware))

    logger.info(
        "startup service=cisco-ai-docs-mcp host=%s port=%s mcp_path=%s log_level=%s "
        "ssl_verify=%s log_query_snippet=%s",
        settings.host,
        settings.port,
        path,
        settings.log_level,
        settings.http_ssl_verify,
        settings.log_query_snippet,
    )

    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        path=path,
        middleware=middleware_list,
        uvicorn_config={
            # Custom HttpAccessLogMiddleware already logs each request in detail.
            "access_log": False,
            "log_level": (settings.log_level or "info").lower(),
        },
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
