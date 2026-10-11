<p align="center">
  <img src="docs/images/oncall-banner.jpg" alt="OnCall: voice incident response with proven recovery" width="100%">
</p>

# OnCall

**A voice incident agent for the engineer who gets paged. It finds the bad release, and it can't touch production without your one-time code.**

OnCall is a self-hosted MCP server (spec `2025-11-25`, Streamable HTTP) with an operator workspace and an Alexa+ experience. A monitoring alert opens the incident and pages you. You ask "what's on fire?" and OnCall reads metrics, deployments and logs, sizes the customer impact, and tells you which release lines up with the error spike. Ask it to roll back and it stages that exact release, then sends a one-time code to your pager. Saying "yes" changes nothing. Only the code, spoken back within 90 seconds, approves that one action. OnCall then watches fresh traffic and closes the incident only when recovery is proven.

Track: **Alexa+** (self-hosted MCP server, plus a simulated Alexa+ experience). Mini challenge: **AWS Builder** (Bedrock, EC2, IAM, Budgets).

## Why it's different

- **Approval you can't fake by talking.** A rollback is bound to one exact release, expires in 120 seconds, runs once, and needs a 6-digit code sent to your pager. "Yes", "approve" and "do it" are never approvals. Codes are stored hashed and lock after 3 wrong tries.
- **No rollback without evidence.** The server refuses to stage a rollback unless fresh metrics show the service degraded right now. The check sits in one place, so it holds for the web UI, the Bedrock agent and every MCP client, Alexa+ included. Unknown health is never treated as healthy.
- **Recovery is earned.** Verification ignores metrics from before the rollback and waits for a fresh window. A resolved alert never closes an incident by itself.
- **It remembers.** Conversations persist across restarts (last 6 exchanges per session, 24 h), so "what about the deploy before that?" works.

## How it works

```text
Alexa+ / any MCP client ──OAuth 2.1──┐
Operator workspace (web, voice) ─────┼─▶ OnCall: MCP server (/mcp) + operator API
                                     │     ├─ Bedrock Nova Pro: tool-using investigator (read-only tools)
                                     │     ├─ deploy-vs-spike correlator, blast radius, runbooks, history
                                     │     └─ safety kernel: propose ─▶ paged code ─▶ execute once ─▶ verify
Prometheus ─▶ Alertmanager ─▶ /api/alerts (opens incident, pages the operator)
Release controller (deploy ledger + rollback webhook) ◀── payments service ◀── traffic
```

The reference deployment runs on one EC2 `t4g.micro` behind Caddy with automatic HTTPS. Design details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Investigation.** OnCall fetches error rates, deployments, blast radius and the runbook in parallel, then lets Amazon Nova Pro (Bedrock Converse with tool use) read logs or history if it needs them. The model gets read-only tools plus `propose_action`. It can't approve or execute anything. If Bedrock is unavailable, a deterministic investigator gives the same structure of answer.

**MCP tools (18).** Read-only: `get_error_rates`, `list_deploys`, `search_logs`, `get_blast_radius`, `get_runbook`, `similar_incidents`, `incident_briefing`, `get_repo_status`. Workflow: `diagnose_incident`, `acknowledge_incident`, `propose_action`, `request_voice_approval`, `approve_with_voice_code`, `execute_action`, `check_recovery` (non-blocking, for Alexa's 500 ms budget), `verify_recovery`, `generate_report`, and guarded `rollback_deploy`.

**Alexa+ authentication** follows the [MCP Toolkit contract](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-authentication.html): Protected Resource Metadata (RFC 9728), authorization server metadata with PKCE `S256`, a `client_credentials` service token limited to discovery, then authorization code + PKCE account linking with a static client. Unauthenticated calls get a plain `401`. A manifest template is in [`deploy/alexa/addon.example.json`](deploy/alexa/addon.example.json).

## Surfaces

| Surface | Path | Access |
| --- | --- | --- |
| Operator workspace | `/` | Operator key |
| Alexa+ simulator | `/alexa.html` | Operator key |
| Public status page | `/status` | Public, read-only |
| MCP server | `/mcp` | OAuth 2.1 or operator key as bearer |

Judges: the live URL, operator key and pager link are in the private testing instructions on the submission.

## Run it locally

Requires Python 3.12+ (tested on 3.14) and Node 22. The sandbox needs no cloud account.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python.exe
.venv/bin/python scripts/run_demo.py
```

Open http://127.0.0.1:8200. The sandbox runs an isolated fault-injection service and never touches a real deployment. To use Bedrock, set `ONCALL_BEDROCK_MODEL_ID=us.amazon.nova-pro-v1:0` and AWS credentials. Without them the deterministic investigator answers.

To preview the full production stack on your machine (payments service, release controller, Prometheus, Alertmanager), put the official Prometheus 3.15.0 and Alertmanager 0.34.1 releases in `tools/`, then run `python scripts/preview.py`.

To rebuild the bundled UI components: `npm ci && npm run build:ui`.

## Deploy to AWS

One EC2 `t4g.micro` (Ubuntu 24.04 arm64) runs the whole stack. Bedrock access uses an instance role scoped to `bedrock:InvokeModel` on Nova Pro, so no AWS keys are stored on the server.

```bash
./deploy/aws/provision.sh                    # IAM role, security group (SSH from your IP only), instance, Elastic IP
./deploy/aws/ship.sh                         # copy code, run deploy/install.sh, health-check everything
./deploy/aws/budget.sh you@example.com 20    # email alerts at 50/80/100% of a monthly budget
./deploy/aws/teardown.sh                     # remove everything
```

The installer adds swap, installs Caddy, downloads Prometheus and Alertmanager with SHA-256 verification, creates one system user per service, generates every secret into `/etc/oncall/*.env` (never printed), validates configs, enables a deny-by-default firewall, and installs nightly SQLite backups. Re-running it upgrades in place and keeps secrets and data.

Operating it: `sudo ./deploy/healthcheck.sh` checks every service. Logs: `journalctl -u oncall -u payments -u oncall-releases -f`.

### Connect a real Alexa+ account

Requires Alexa+ for Builders access and the Alexa AI CLI, currently limited to select partners.

1. Copy `deploy/alexa/addon.example.json` to `deploy/alexa/addon.json` and replace `YOUR_DOMAIN` with your host.
2. Run `alexa-ai new mcp` and `alexa-ai deploy` from `deploy/alexa/`, answering **Y** to account linking.
3. Get the static client: `sudo cat /etc/oncall/alexa-client.env`.
4. Run `alexa-ai configure-account-linking --addon-id <id> --stage development --client-id <client-id>`. The CLI prints Alexa's redirect URIs.
5. Add every redirect URI to `/etc/oncall/oncall.env` as `ONCALL_OAUTH_REDIRECT_URIS=...`, then `sudo systemctl restart oncall`.
6. Run `alexa-ai deploy` again, since Alexa caches auth metadata at deploy time.

## Connect your own systems

Set `ONCALL_MODE=live` in `.env` (see [`.env.example`](.env.example)). OnCall reads deployments from GitHub or a release-controller ledger, metrics from Prometheus or CloudWatch, and logs from Loki (optional). Rollbacks go to an authenticated webhook you provide:

```json
POST {"service":"payments","target_commit":"<sha>","action_id":"<approved-action-id>"}
Idempotency-Key: <action_id>
→ 200 {"status":"rolled_back","service":"payments","target_commit":"<sha>","action_id":"<same-id>"}
```

Any other response, timeout or redirect is treated as unknown and never retried blindly. Mutations are off by default (`ONCALL_MUTATION_ENABLED=false`). A reference executor is in [`executor/rollback_executor.py`](executor/rollback_executor.py).

## Security and limitations

- Every service binds to `127.0.0.1`. Only 80/443 are public, and SSH is limited to one admin IP.
- The operator API needs a bearer key. MCP accepts that key or an OAuth token whose audience is this server's `/mcp`. Codes and tokens are stored only as SHA-256 hashes.
- Per-client rate limits on every endpoint, including failed keys (20 per 5 minutes).
- Single operator. Every linked Alexa account maps to that one operator. It isn't multi-tenant identity.
- SQLite on one host. Scaling out and reconciling interrupted actions need more work.
- Not yet verified: a real Alexa+ account link and certification. The toolkit is partner-only, so this entry uses the simulated Alexa+ experience alongside the real MCP server.

## Project layout

| Path | What it is |
| --- | --- |
| `server.py` | MCP server: typed tools, OAuth bearer gate |
| `brain.py` | Operator API, command routing, serves the UI |
| `agent.py` | Bedrock Nova investigator and chat, conversation memory |
| `oncall_core.py` | Safety kernel: incidents, evidence, approvals, voice codes, verification, reports |
| `oauth.py` | OAuth 2.1 authorization server for Alexa+ account linking |
| `correlate.py` | Deploy-vs-spike timing correlation (never claims causation) |
| `integrations.py`, `config.py` | GitHub, Prometheus, CloudWatch, Loki, rollback adapters; validated settings |
| `production/` | Payments service, release controller, traffic generator |
| `target_service/` | Sandbox fault-injection service |
| `executor/` | Reference rollback webhook for your own hosts |
| `runbooks/` | Runbooks the investigator reads |
| `app/` | Workspace, Alexa+ simulator, status page |
| `deploy/` | AWS provisioning, installer, Caddy, Prometheus, Alertmanager, systemd units |

## License

[MIT](LICENSE). Bundled UI components are listed in [app/vendor/THIRD_PARTY_NOTICES.md](app/vendor/THIRD_PARTY_NOTICES.md).
