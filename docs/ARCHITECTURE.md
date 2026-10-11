# OnCall architecture

OnCall is an incident agent built around one rule: production changes only with fresh evidence and an out-of-band human approval. The AI model can investigate. It can't decide or act.

## System architecture

```mermaid
flowchart TB
    subgraph Clients["Clients"]
        direction LR
        Engineer["On-call Engineer<br/><small>Voice · Web · Pager</small>"]
        AlexaDevice["Alexa+ / Any MCP Client<br/><small>OAuth 2.1 Account Linking</small>"]
        Browser["Browser Surfaces<br/><small>Workspace · Alexa+ Simulator · Public Status</small>"]
    end

    subgraph Edge["Public Edge"]
        Caddy["Caddy Reverse Proxy<br/><small>Automatic HTTPS · Only Ports 80/443 Public</small>"]
    end

    subgraph Core["OnCall Trusted Core · Python"]
        direction TB
        subgraph Entry[" "]
            direction LR
            API["Operator API · brain.py<br/><small>Bearer Auth · Rate Limits · Alert Intake · Escalation</small>"]
            MCP["MCP Server · server.py<br/><small>18 Typed Tools · Streamable HTTP · Spec 2025-11-25</small>"]
            OAuth["OAuth 2.1 Server · oauth.py<br/><small>PKCE S256 · RFC 9728 Metadata</small>"]
        end
        Kernel["Safety Kernel · oncall_core.py<br/><small>Evidence Gate · One-time Codes · Execute Once · Verify</small>"]
        subgraph Support[" "]
            direction LR
            Correlator["Deploy Correlator · correlate.py<br/><small>Timing Only · Never Claims Cause</small>"]
            Adapters["Provider Adapters · integrations.py<br/><small>Fail Closed · Unknown Never Means Healthy</small>"]
            DB[("SQLite WAL<br/><small>Incidents · Approvals · Audit · Memory</small>")]
        end
    end

    subgraph Reasoning["Reasoning Layer · Untrusted Output"]
        direction TB
        Agent["Investigator · agent.py<br/><small>Read-only Tools + propose_action Only</small>"]
        Fallback["Deterministic Investigator<br/><small>Same Answer Shape When Bedrock Fails</small>"]
    end

    subgraph AWS["AWS"]
        direction TB
        Bedrock["Amazon Bedrock<br/><small>Nova Pro · Converse Tool Use</small>"]
        Role["IAM Instance Role<br/><small>bedrock:InvokeModel · No Stored Keys</small>"]
    end

    subgraph Observability["Observability"]
        direction LR
        Prom["Prometheus<br/><small>Scrape 15s · 5xx Alert Rule</small>"]
        AM["Alertmanager<br/><small>Bearer-signed Webhook</small>"]
    end

    subgraph Production["Reference Production"]
        direction LR
        Releases["Release Controller<br/><small>Deploy Ledger · Idempotent Rollback Webhook</small>"]
        Payments["Payments Service<br/><small>Checkout API · Metrics Endpoint</small>"]
        Traffic["Traffic Generator<br/><small>Keeps Telemetry Fresh</small>"]
    end

    Pager["Pager · ntfy / Slack<br/><small>Incident Pages · One-time Codes</small>"]

    Engineer --> AlexaDevice
    Engineer --> Browser
    AlexaDevice --> Caddy
    Browser --> Caddy
    Caddy -- "UI and REST" --> API
    Caddy -- "/mcp" --> MCP
    Caddy -- "OAuth" --> OAuth
    API -- "MCP client" --> MCP
    MCP --> Kernel
    API --> Agent
    Agent -- "tool calls" --> MCP
    Agent --> Bedrock
    Agent -.-> Fallback
    Role -.-> Bedrock
    Kernel --> Correlator
    Kernel --> Adapters
    Kernel --> DB
    Kernel -- "pages and codes" --> Pager
    Pager -.-> Engineer
    Adapters -- "PromQL" --> Prom
    Adapters -- "ledger and rollback" --> Releases
    Prom --> AM
    AM -- "/api/alerts" --> API
    Prom -- "scrape" --> Payments
    Releases -- "switch release" --> Payments
    Traffic --> Payments

    style Entry fill:none,stroke:none
    style Support fill:none,stroke:none
```

Every service binds to `127.0.0.1` and runs as its own system user under systemd (`deploy/systemd/`). Caddy is the only public listener.

The operator API never touches incident data directly. It calls the MCP server as an MCP client (`brain.py` uses the MCP SDK's Streamable HTTP client). So the web workspace, the Alexa+ simulator, the Bedrock agent and a real Alexa+ device all go through the same tools and the same safety checks.

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| MCP server | `server.py` | 18 typed MCP tools, bearer and OAuth gate, Pydantic input validation |
| Operator API | `brain.py` | REST API and UI hosting, command routing, alert intake, re-paging and escalation |
| Investigator | `agent.py` | Bedrock Converse tool use with Nova Pro, conversation memory, deterministic fallback |
| Safety kernel | `oncall_core.py` | Incidents, evidence, proposals, approvals, voice codes, execution, verification, reports, audit |
| OAuth server | `oauth.py` | OAuth 2.1 with PKCE for Alexa+ account linking |
| Correlator | `correlate.py` | Lines up deploy times with error spikes and reports timing, never cause |
| Adapters | `integrations.py`, `config.py` | Prometheus, CloudWatch, GitHub, Loki, deploy ledger, rollback webhook, validated settings |
| Reference production | `production/` | Payments service, release controller, traffic generator |
| Sandbox | `target_service/` | Local fault-injection service for running without a cloud account |

## Incident flow

```mermaid
sequenceDiagram
    autonumber
    participant Prom as Prometheus + Alertmanager
    participant API as Operator API
    participant Eng as Engineer via Alexa+
    participant MCP as MCP Server + Safety Kernel
    participant LLM as Bedrock Nova Pro
    participant Pager as Pager
    participant Rel as Release Controller

    Prom->>API: Checkout 5xx above 5% for 1 minute
    API->>MCP: Open incident
    MCP->>Pager: Page primary, re-page every 5 min, escalate after 10
    Eng->>MCP: What's on fire?
    MCP->>LLM: Evidence as untrusted data with read-only tools
    LLM-->>MCP: Tool calls for logs and history
    MCP-->>Eng: Error rate, failed checkouts, suspect release
    Eng->>MCP: Roll back the latest deploy
    MCP->>Prom: Re-read fresh metrics
    alt Health is not degraded
        MCP-->>Eng: Refused, nothing staged
    else Degraded right now
        MCP-->>Eng: Rollback staged for exact commit, expires in 120 s
    end
    Eng->>MCP: Yes
    MCP-->>Eng: Words are not approval, nothing changed
    Eng->>MCP: Send me a code
    MCP->>Pager: 6-digit code, valid 90 s
    Eng->>MCP: Spoken code
    MCP->>MCP: Hash compare, consume once, lock after 3 misses
    MCP->>Rel: POST rollback with Idempotency-Key = action ID
    Rel-->>MCP: rolled_back echo of service, commit, action ID
    Eng->>MCP: Check recovery
    MCP->>Prom: Samples after the rollback only
    alt No full fresh window yet
        MCP-->>Eng: Not verified, wait for fresh traffic
    else Fresh window healthy
        MCP-->>Eng: Recovery verified with N fresh requests
        MCP->>API: Incident resolved, status page updated
    end
```

## State machines

Incident:

```mermaid
stateDiagram-v2
    [*] --> Open: alert fires
    Open --> Acknowledged: engineer acknowledges
    Open --> Open: re-page, escalate to secondary
    Acknowledged --> Verifying: rollback executed
    Open --> Verifying: rollback executed
    Verifying --> Verifying: no fresh healthy window yet
    Verifying --> Resolved: fresh post-rollback traffic healthy
    Resolved --> [*]
```

Rollback approval:

```mermaid
stateDiagram-v2
    [*] --> Pending: propose_action with fresh degraded evidence
    Pending --> Superseded: newer proposal
    Pending --> Cancelled: operator cancels
    Pending --> Expired: 120 s elapsed
    Pending --> Executing: one-time code accepted
    Executing --> RolledBack: webhook echoes exact target
    Executing --> Uncertain: timeout, redirect or mismatch
    Executing --> Rejected: active deploy changed
    RolledBack --> [*]
    Uncertain --> [*]: reconcile manually, never retried
    Rejected --> [*]
    Cancelled --> [*]
    Expired --> [*]
    Superseded --> [*]
```

## Safety guarantees

| Guarantee | How it's enforced |
| --- | --- |
| No action without evidence | `propose_action` re-reads metrics and refuses unless health is `degraded`. Missing, stale or partial telemetry is `unknown`, never `healthy`. |
| The model can't act | Bedrock gets read-only tools plus `propose_action`. Approval and execution tools aren't exposed to it, and tool output is passed as untrusted data. |
| Talking isn't approving | Approval needs a code delivered out of band. Confirmation words are filtered out before any model routing. |
| Codes can't be reused or guessed | Stored as SHA-256 hashes, compared in constant time, consumed under a writer lock, locked after 3 wrong tries, valid 90 seconds. |
| One approval, one target | The approval stores the exact commit. Execution re-checks session, expiry and target, and rejects if the active deploy changed. |
| At most once | Per-deployment execution reservation plus the webhook's durable idempotency key. Uncertain outcomes are consumed, not retried. |
| Recovery is proven | Verification needs a full 2-minute metrics window after the rollback. Pre-rollback traffic counts as zero evidence. |
| Every step is auditable | Each transition writes an audit row, which drives the timeline, the report and the public status page. |

## Data model

```mermaid
flowchart LR
    subgraph Lifecycle["Incident Lifecycle"]
        incidents[("incidents")]
        alerts[("alerts")]
    end
    subgraph Mutation["Approval and Mutation Path"]
        approvals[("approvals")]
        voice[("voice_challenges<br/><small>hashed codes</small>")]
        executions[("executions")]
        locks[("workflow_locks")]
    end
    subgraph Record["Incident Record"]
        audit[("audit")]
        reports[("reports")]
    end
    subgraph Memory["Conversation Memory"]
        threads[("agent_threads")]
        turns[("chat_turns<br/><small>6 exchanges · 24 h</small>")]
    end
    subgraph Linking["Alexa+ Account Linking"]
        oauth[("oauth_clients · codes<br/>tokens · consents<br/><small>hashed tokens</small>")]
    end

    alerts --> incidents
    approvals --> voice
    approvals --> executions
    executions --> audit
    incidents --> audit
    audit --> reports
    threads --> turns
```

SQLite in WAL mode, one database per mode, with a nightly backup installed by `deploy/install.sh`.

## Authentication

| Caller | Credential |
| --- | --- |
| Operator workspace and API | Bearer operator key, constant-time compare |
| MCP clients | Operator key, or an OAuth 2.1 access token whose audience is this server's `/mcp` |
| Alexa+ discovery | `client_credentials` token limited to discovery |
| Alexa+ account link | Authorization code + PKCE `S256` with a static client. The operator approves each link with the operator key. |
| Alertmanager | Dedicated bearer token on `/api/alerts` |
| Deploy ledger and rollback webhook | Separate read and mutation tokens, so a leaked read token can't change production |

Unauthenticated MCP calls get a plain `401` with no `WWW-Authenticate` header, as Alexa+ requires. Rate limits apply per client on every endpoint, including failed key attempts.

## Alexa+ surfaces

- **MCP server (`/mcp`):** what a real Alexa+ add-on connects to. `check_recovery` is non-blocking to fit Alexa's 500 ms tool budget. Manifest template: `deploy/alexa/addon.example.json`.
- **Alexa+ simulator (`/alexa.html`):** a voice and Echo Show style interface that calls the same MCP tools through the operator API. It exists because Alexa+ add-on access isn't open to hackathon participants.

## Integrations

| Purpose | Options | Setting |
| --- | --- | --- |
| Metrics | Prometheus or CloudWatch | `ONCALL_METRICS_SOURCE` |
| Deployments | Release-controller ledger or GitHub Deployments | `ONCALL_DEPLOYS_SOURCE` |
| Logs | Loki (optional) | `ONCALL_LOKI_URL` |
| Rollback | Authenticated webhook (`executor/rollback_executor.py` is a reference) | `ONCALL_ROLLBACK_URL` |
| Paging | ntfy, Slack or JSON webhook, plus an optional secondary | `ONCALL_NOTIFY_URL` |
| Reasoning | Amazon Bedrock Nova Pro, or deterministic | `ONCALL_BEDROCK_MODEL_ID` |

Mutations are off by default (`ONCALL_MUTATION_ENABLED=false`). Every option is documented in `.env.example`.

## Deployment

```mermaid
flowchart LR
    Provision["provision.sh<br/><small>IAM Role · Security Group · EC2 · Elastic IP</small>"] --> Ship["ship.sh<br/><small>Copy Code · Run Installer · Health Check</small>"]
    Ship --> Install["install.sh<br/><small>Caddy · Checksum-verified Prometheus and Alertmanager<br/>Per-service Users · Generated Secrets · Firewall · Backups</small>"]
    Install --> Live["Running Stack<br/><small>systemd · 127.0.0.1 Only · HTTPS Edge</small>"]
    Budget["budget.sh<br/><small>Alerts at 50 / 80 / 100%</small>"] -.-> Live
    Teardown["teardown.sh<br/><small>Remove Everything</small>"] -.-> Live
```

Secrets are generated on the host into `/etc/oncall/*.env` and never printed. No AWS keys are stored on the server.

## Limitations

- Single operator. It isn't multi-tenant identity.
- SQLite on one host. Scaling out and reconciling interrupted actions need more work.
- A real Alexa+ account link and certification are not yet verified, because the toolkit is partner-only.
