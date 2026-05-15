# Cisco AI Docs MCP (Frontline)

FastMCP server (**streamable HTTP**) that:

1. On each **`cisco_docs_query`** tool call, **POST**s to Cisco **Docs AI**  
   `https://docs-ai-ext.cloudapps.cisco.com/api/v1/docs/ask` with  
   `Authorization: Bearer <docai_token>` and JSON body `{"question":"<query>"}`.
2. **Voice mode (default):** prepends a short instruction so answers stay in **3–4 sentences**, then strips URLs and citation-style noise from the model reply before returning it to the client.
3. Optionally requires HTTP header **`MCP_REQUEST_HEADERS`** to match the environment variable of the same name (same pattern as other App Runner MCP deployments).

## Tool

| Name | Description |
|------|-------------|
| `cisco_docs_query` | Pass natural-language `query`; returns JSON with **`answer`**, **`confidence`**, and **`voice_optimized`** (set `VOICE_OPTIMIZED=false` for raw answer + `sources`). |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| **`docai_token`** (or `DOC_AI_TOKEN`) | Yes | Bearer token for Docs AI (`Authorization: Bearer …`) |
| `DOCS_AI_ASK_URL` | No | Default `https://docs-ai-ext.cloudapps.cisco.com/api/v1/docs/ask` |
| `MCP_REQUEST_HEADERS` | Recommended (prod) | Shared secret; clients must send this value in the **HTTP header** `MCP_REQUEST_HEADERS` |
| `VOICE_OPTIMIZED` | No | Default `true` — voice prompt + post-processed answer |
| `VOICE_MAX_SENTENCES` | No | Default `4` — cap on sentences when `VOICE_OPTIMIZED=true` |
| `PORT` | No | Default `8080` (App Runner sets this) |
| `HTTP_SSL_VERIFY` / `SSL_CA_BUNDLE` | No | Corporate TLS overrides |
| `LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, … — controls verbosity |
| `LOG_QUERY_SNIPPET` | No | If `true` (default), logs a short sanitized prefix of each docs query for troubleshooting |
| `PYTHONUNBUFFERED` | Recommended | Set to **`1`** so logs reach CloudWatch promptly (`apprunner.yaml` / `run.sh` set this) |

Copy `env.example` to `.env` for local runs (never commit `.env`).

## Run locally

```bash
pip install -r requirements.txt
set docai_token=your-docs-ai-bearer-token
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

Configure **`docai_token`** (and optionally **`MCP_REQUEST_HEADERS`**) in App Runner **environment variables** or secrets.

If **Configuration source** is **API**, you can use **`apprunner.yaml`** in the repo (same commands as above).

### WxCC / Webex tool output shape

The activity-service validates **`structuredContent.result`** against the tool **output schema**. This server returns FastMCP **`ToolResult`** with explicit **`structured_content={"result": "<JSON string>"}`** and **`output_schema`** on `cisco_docs_query`, so the MCP **`CallToolResult`** carries structured output the same way WxCC expects.

**Why Address_MCP (`address_book`) can look “simpler”:** that tool returns a **plain string**. Your VA integration often **does not attach a strict output schema** to that MCP tool, so WxCC only checks text content. The docs integration is typically configured with a schema that **requires `result`**, so this server must populate structured content explicitly.

**Functional difference:** Address MCP calls **WxCC APIs** with a bearer token from the agent. This MCP calls **Cisco Docs AI** (`docs-ai-ext.cloudapps.cisco.com`) using **`docai_token`**. HTTP **`401`/`403`** from Docs AI usually mean an invalid or expired bearer token or entitlement on the Docs AI side.

### Application logs in App Runner and CloudWatch

Messages such as `POST /mcp`, `CallToolRequest`, and `INFO: … uvicorn` are **runtime application logs**. They are stored in CloudWatch under the **`application`** log group for your service — **not** in streams whose names start with **`deployment/`** (those are build/deploy logs).

| Log group name ends with | What you get |
|--------------------------|--------------|
| **`…/service`** | Platform + **deployment/build** output (`deployment/…` streams, `[Build]` lines) |
| **`…/application`** | **Running container**: uvicorn, MCP SDK, logger **`cisco_ai_docs_mcp`** |

Official reference: [Viewing App Runner logs in CloudWatch](https://docs.aws.amazon.com/apprunner/latest/dg/monitor-cwl.html).

**App Runner console:** your service → **Logs** → section **Application logs** → open stream **`instance/…`**. Use **View in CloudWatch** when available.

**CloudWatch console:** **Logs** → **Log groups** → **`/aws/apprunner/<service-name>/<service-id>/application`** (correct **Region**).

**CloudWatch Logs Insights** (search across streams): **Logs** → **Logs Insights** → select the **`application`** log group:

```sql
fields @timestamp, @message
| filter @message like /POST \/mcp|CallToolRequest|cisco_ai_docs_mcp/
| sort @timestamp desc
| limit 200
```

**If `application` appears empty:** set **`PYTHONUNBUFFERED=1`** at runtime (see **`apprunner.yaml`**), confirm the service is **Running**, and verify you opened **`…/application`** rather than only **`…/service`**.

### Container image (Dockerfile)

Build from the **Dockerfile** instead if you want a single image definition; set **port** **8080** (or match `PORT`).

MCP clients must send header `MCP_REQUEST_HEADERS: <same value as env>` when that env var is set.

## Security notes

- Do not commit real credentials; use App Runner secrets or Parameter Store for **`docai_token`**.
- `MCP_REQUEST_HEADERS` uses a timing-safe compare when enabled.
