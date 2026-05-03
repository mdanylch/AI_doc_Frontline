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

import requests
from fastmcp import FastMCP
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_URL = (
    "https://sso-dbbfec7f.sso.duosecurity.com/oauth/DID1LHEMWQZDEGZ7FAXX/token"
)
_DEFAULT_SCRIPT_URL = "https://scripts.cisco.com/api/v2/jobs/Mykola_Cisco_Docs"

_HTTP_TIMEOUT_SEC = 120
_MAX_ERROR_BODY_CHARS = 8000


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


def _requests_verify(settings: Settings) -> bool | str:
    if not settings.http_ssl_verify:
        return False
    if settings.ssl_ca_bundle:
        return settings.ssl_ca_bundle
    return True


def fetch_client_credentials_token(settings: Settings) -> str:
    """OAuth 2.0 client_credentials — new access token for this request."""
    data = {
        "grant_type": "client_credentials",
        "client_id": settings.client_id_bdb,
        "client_secret": settings.client_secret_bdb,
    }
    r = requests.post(
        settings.bdb_token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_HTTP_TIMEOUT_SEC,
        verify=_requests_verify(settings),
    )
    r.raise_for_status()
    payload = r.json()
    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise RuntimeError("Token response missing access_token")
    return token


mcp = FastMCP(name="cisco-ai-docs")


@mcp.tool()
def cisco_docs_query(query: str) -> str:
    """
    Query Cisco documentation using the Mykola_Cisco_Docs BDB script job.

    Args:
        query: Question or search terms from the MCP client.

    Returns:
        JSON string with the script API response body or structured error information.
    """
    settings = get_settings()
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query must not be empty"})

    try:
        token = fetch_client_credentials_token(settings)
    except requests.HTTPError as e:
        text = ""
        if e.response is not None:
            text = (e.response.text or "")[:_MAX_ERROR_BODY_CHARS]
        logger.warning("Duo token HTTP error: %s", e.response.status_code if e.response else "?")
        return json.dumps(
            {
                "error": "Failed to obtain OAuth token",
                "status_code": e.response.status_code if e.response else None,
                "detail": text,
            }
        )
    except requests.RequestException as e:
        logger.warning("Duo token request failed: %s", e)
        return json.dumps({"error": "OAuth token request failed", "message": str(e)})
    except RuntimeError as e:
        return json.dumps({"error": str(e)})

    body = {"dev": "true", "input": {"query": q}}

    try:
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
        r.raise_for_status()
        try:
            return json.dumps(r.json())
        except ValueError:
            return json.dumps({"raw_text": (r.text or "")[:_MAX_ERROR_BODY_CHARS]})

    except requests.HTTPError as e:
        text = ""
        if e.response is not None:
            text = (e.response.text or "")[:_MAX_ERROR_BODY_CHARS]
        logger.warning("Cisco script HTTP error: %s", e.response.status_code if e.response else "?")
        return json.dumps(
            {
                "error": "Cisco script job HTTP error",
                "status_code": e.response.status_code if e.response else None,
                "detail": text,
            }
        )
    except requests.RequestException as e:
        logger.warning("Cisco script request failed: %s", e)
        return json.dumps({"error": "Cisco script request failed", "message": str(e)})


class MCPRequestHeadersAuthMiddleware(BaseHTTPMiddleware):
    """Require header ``MCP_REQUEST_HEADERS`` to match configured secret (timing-safe)."""

    def __init__(self, app, *, expected: str):
        super().__init__(app)
        self._expected = expected

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path.rstrip("/") or ""
        if path == "/health":
            return await call_next(request)
        got = request.headers.get("MCP_REQUEST_HEADERS") or ""
        if not hmac.compare_digest(got.encode("utf-8"), self._expected.encode("utf-8")):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "cisco-ai-docs-mcp"})


def main() -> None:
    settings = get_settings()
    middleware_list: list[Middleware] = []
    exp = (settings.mcp_request_headers or "").strip()
    if exp:
        middleware_list.append(
            Middleware(MCPRequestHeadersAuthMiddleware, expected=exp),
        )
        logger.info("HTTP authentication enabled (header MCP_REQUEST_HEADERS)")
    else:
        logger.warning(
            "MCP_REQUEST_HEADERS is not set — HTTP requests are not authenticated. "
            "Configure it on AWS App Runner for production."
        )

    path = settings.mcp_path.strip() or "/mcp"
    if not path.startswith("/"):
        path = "/" + path

    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        path=path,
        middleware=middleware_list or None,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
