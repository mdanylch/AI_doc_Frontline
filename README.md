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
| `LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, … — controls verbosity |
| `LOG_QUERY_SNIPPET` | No | If `true` (default), logs a short sanitized prefix of each docs query for troubleshooting |
| `PYTHONUNBUFFERED` | Recommended | Set to **`1`** so logs reach CloudWatch promptly (`apprunner.yaml` / `run.sh` set this) |

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

**`start.sh`** installs dependencies into **`/app/vendor`** (build phase). AWS Fusion’s runtime image only copies the **`/app`** tree from the build stage, so global `pip install` during build does not appear in the container that runs your app—vendor installs avoid that. **`run.sh`** sets **`PYTHONPATH`** to include `/app/vendor`, then starts `python3 mcp_server.py` with **`PORT`** (App Runner sets **8080**).

OAuth env vars on App Runner may be lowercase **`client_id`** and **`client_secret`**; those names are supported.

If **Configuration source** is **API**, you can use **`apprunner.yaml`** in the repo (same commands as above).

### Application logs in App Runner (CloudWatch)

App Runner sends **stdout and stderr** from your process to CloudWatch. This is **not** the same stream as **deployment** or **service** logs.

| What you see | Where it lives |
|----------------|----------------|
| Build output (`[Build]`, pip, errors) | **Deployment** logs / Service log group — stream names like `deployment/…` |
| App Runner platform messages | **Service** log group — stream `events` |
| **`print()`, Python `logging`, uvicorn** | **Application** log group — streams like `instance/…` |

**In the console:** open your service → **Logs** tab → section **Application logs** (not only *Deployment logs*).  
**In CloudWatch:** log group name pattern:

`/aws/apprunner/<service-name>/<service-id>/application`

Official detail: [Viewing App Runner logs in CloudWatch](https://docs.aws.amazon.com/apprunner/latest/dg/monitor-cwl.html).

**If application logs are empty or delayed:**

1. Set **`PYTHONUNBUFFERED=1`** for the runtime (this repo sets it in **`apprunner.yaml`** and **`run.sh`**). If you use **Configure all settings here** instead of the YAML file, add that variable manually in **Runtime environment variables**.
2. Confirm the container stays healthy (TCP health check passes) — a crashing process may produce little or no application output.
3. Open **CloudWatch → Log groups** and select the **`…/application`** group, not only **`…/service`**.

### Container image (Dockerfile)

Build from the **Dockerfile** instead if you want a single image definition; set **port** **8080** (or match `PORT`).

MCP clients must send header `MCP_REQUEST_HEADERS: <same value as env>` when that env var is set.

## Security notes

- Do not commit real credentials; use App Runner secrets or Parameter Store.
- `MCP_REQUEST_HEADERS` uses a timing-safe compare when enabled.
