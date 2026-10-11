# Friction log

Written by the builder of OnCall during the Amazon Developer Challenge (Aug 31 – Oct 23, 2026). Each entry gives what we tried, what happened, the impact, and a suggestion. Dates are 2026.

## Alexa+ Add-ons / MCP Toolkit

**1. Onboarding sends you to tools you can't use.**
- Tried: the "Set Up Your Development Environment" page says to run `npm install -g @alexa-ai/cli`.
- Happened: the package returns 404 on the public npm registry. Other participants confirmed this on the discussion board (Sep 15 and Sep 17).
- Impact: about half a day spent before learning that the toolkit and web simulator are limited to select partners.
- Suggestion: put a short "availability" box at the top of the quickstart, and a hackathon note pointing to the simulated-experience path.

**2. The Add-ons console says "Coming soon" with no explanation.**
- Tried: opened Alexa+ in the developer console.
- Happened: it showed "Coming soon". It doesn't say whether that's down to region, account or preview status.
- Impact: we assumed it was a region restriction (we're in India) until an organizer explained.
- Suggestion: say "Private preview: available to select partners" and link to the eligibility page.

**3. Several auth details are easy to miss and break account linking without any error.**
- Tried: built OAuth 2.1 for an MCP server against the toolkit docs.
- Happened: three details we found only on careful reading:
  - Alexa+ needs a static client; it doesn't use dynamic client registration.
  - It reads `/.well-known/oauth-protected-resource` at the root.
  - A `401` must not include `WWW-Authenticate`, which is the opposite of the usual MCP / RFC 9728 behaviour.
- Impact: our first spec-compliant server would have failed linking.
- Suggestion: a one-page "Alexa+ vs generic MCP auth differences" table, plus a conformance checker you can run against a public URL.

**4. The two-tier token model needs a worked example.**
- The `client_credentials` service token for discovery, followed by account linking on the first tool call, is a good design. But the docs never show what a correct `401` to a service token looks like on `tools/call`.
- Suggestion: add request/response transcripts for both tiers.

**5. The 500 ms response limit clashes with long-running tools.**
- Recovery verification has to watch fresh traffic for 30 s or more.
- We built `check_recovery`: it starts the watch in the background and reports progress on the next call.
- Suggestion: document a recommended pattern for long-running tools, such as progress notifications or a polling convention.

## Amazon Bedrock (Nova)

**6. Choosing a Nova model ID is confusing.**
- The console and the model list mix on-demand IDs (`amazon.nova-pro-v1:0`), inference profiles (`us.amazon.nova-pro-v1:0`), and provisioned variants (`:24k`, `:300k`).
- Nova 2 Lite works only through an inference profile, which we learned from a failed call.
- Suggestion: on each model card, show which ID to use for the Converse API.

**7. Least-privilege IAM for inference profiles is under-documented.**
- An instance role that can call only Nova Pro needs both the inference-profile ARN and the foundation-model ARN in every region the profile routes to.
- Suggestion: a copy-paste policy example per inference profile.

**What worked well:**
- Converse tool use behaved the same across Nova Micro, Lite and Pro.
- A model call costs a fraction of a cent.
- We needed no stored keys on EC2 (instance role).

## AWS (EC2, IAM, CLI)

**8. Git Bash on Windows changes `/aws/service/...` SSM parameter paths.**
- `aws ssm get-parameter --name /aws/service/canonical/...` failed with `ParameterNotFound` until we set `MSYS_NO_PATHCONV=1`.
- Suggestion: mention this in the CLI troubleshooting guide next to the Windows notes.

**9. Lightsail can't take an IAM role.**
- Lightsail is cheaper, but it can't attach an instance role, so keyless Bedrock access means EC2.
- Suggestion: state this in the Lightsail-vs-EC2 comparison.

## Hackathon logistics

**10. Hosting has to stay live through judging.**
- Judging ends Nov 20, so we needed a host that runs for about 50 days on $50 of credits.
- The organizer's suggestion was Lambda. A stateful incident agent (metrics scraping, alert webhooks, background watches) fits a small always-on VM better.
- Suggestion: a reference "stateful MCP on a budget" architecture.
