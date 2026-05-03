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
| `CLIENT_ID_BDB` | Yes | Duo OAuth client id |
| `CLIENT_SECRET_BDB` | Yes | Duo OAuth client secret |
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

### Managed Python 3.11 (Fusion) — source repo

The Fusion builder runs **`sh start.sh` during the image build**. The repo includes **`start.sh`** (dependency install only) so that step succeeds. The process that listens on the port must be set separately:

- Prefer **`apprunner.yaml`** in this repo (`run.command: python3 mcp_server.py`, port **8080**).
- If you use **Configure all settings here** and **ignore** `apprunner.yaml`, set **Start command** to `python3 mcp_server.py` (not only `sh start.sh`, or the container would install deps and exit).

Environment variables / secrets: `CLIENT_ID_BDB`, `CLIENT_SECRET_BDB`, `MCP_REQUEST_HEADERS`.

### Container image (Dockerfile)

Build from the **Dockerfile** instead if you want a single image definition; set **port** **8080** (or match `PORT`).

MCP clients must send header `MCP_REQUEST_HEADERS: <same value as env>` when that env var is set.

## Security notes

- Do not commit real credentials; use App Runner secrets or Parameter Store.
- `MCP_REQUEST_HEADERS` uses a timing-safe compare when enabled.
