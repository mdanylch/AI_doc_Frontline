# Cisco AI Docs MCP (Frontline)

FastMCP server (**streamable HTTP**) that:

1. Obtains a **new Duo OAuth access token** (`client_credentials`) on **each** `cisco_docs_query` tool call.
2. Calls the Cisco BDB script job **`Mykola_Cisco_Docs`** with `Authorization: Bearer <token>` and body  
   `{"dev":"true","input":{"query":"<your query>"}}`.
3. Optionally requires HTTP header **`MCP_REQUEST_HEADERS`** to match the environment variable of the same name (same pattern as other App Runner MCP deployments).

## Tool

| Name | Description |
|------|-------------|
| `cisco_docs_query` | Pass natural-language `query`; returns JSON from the script API. |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| OAuth client id | Yes | `CLIENT_ID_BDB`, `CLIENT_ID`, or **`client_id`** (App Runner) |
| OAuth client secret | Yes | `CLIENT_SECRET_BDB`, `CLIENT_SECRET`, or **`client_secret`** (App Runner) |
| `MCP_REQUEST_HEADERS` | Recommended (prod) | Shared secret; clients must send this value in the **HTTP header** `MCP_REQUEST_HEADERS` |
| `BDB_TOKEN_URL` | No | Defaults to Duo token endpoint in code |
| `CISCO_SCRIPT_JOB_URL` | No | Defaults to `https://scripts.cisco.com/api/v2/jobs/Mykola_Cisco_Docs` |
| `PORT` | No | Default `8080` (App Runner sets this) |
| `HTTP_SSL_VERIFY` / `SSL_CA_BUNDLE` | No | Corporate TLS overrides |

Copy `env.example` to `.env` for local runs (never commit `.env`).

## Run locally

```bash
pip install -r requirements.txt
set CLIENT_ID_BDB=...
set CLIENT_SECRET_BDB=...
set MCP_REQUEST_HEADERS=your-shared-secret   # optional for local testing
python mcp_server.py
```

Health: `GET http://localhost:8080/health`  
MCP endpoint: `http://localhost:8080/mcp` (streamable HTTP)

## AWS App Runner

### Managed Python 3.11 — matches Address_MCP style

| Setting | Value |
|--------|--------|
| **Build command** | `sh start.sh` |
| **Start command** | `sh run.sh` |
| **Port** | `8080` |

**`start.sh`** installs dependencies only (build phase). **`run.sh`** starts `python3 mcp_server.py` with **`PORT`** (App Runner sets **8080**).

OAuth env vars on App Runner may be lowercase **`client_id`** and **`client_secret`**; those names are supported.

If **Configuration source** is **API**, you can use **`apprunner.yaml`** in the repo (same commands as above).

### Container image (Dockerfile)

Build from the **Dockerfile** instead if you want a single image definition; set **port** **8080** (or match `PORT`).

MCP clients must send header `MCP_REQUEST_HEADERS: <same value as env>` when that env var is set.

## Security notes

- Do not commit real credentials; use App Runner secrets or Parameter Store.
- `MCP_REQUEST_HEADERS` uses a timing-safe compare when enabled.
