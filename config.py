"""Validated process configuration. Only the project .env is loaded, never parent files."""
from __future__ import annotations

import hmac
import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
# Allowed recovery observation windows in seconds (0 = single fresh check).
WATCH_WINDOWS = (0, 10, 30, 60, 120)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url(value: str, name: str, internal: tuple[str, ...] = ()) -> None:
    """HTTPS everywhere, except HTTP to loopback or an explicitly allowlisted
    single-label internal hostname (e.g. a Docker Compose service name)."""
    if not value:
        return
    try:
        parts = urlsplit(value)
        port = parts.port
        valid = (parts.hostname and parts.scheme in ("http", "https") and
                 not parts.username and not parts.password and not parts.query and not parts.fragment and
                 (parts.scheme == "https" or _loopback(parts.hostname) or parts.hostname in internal) and
                 (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in value):
        raise ValueError(f"{name} must be HTTPS (or HTTP loopback / allowlisted internal host), without credentials, query or fragment")


def _strong_key(key: str) -> bool:
    # Entropy cannot be proven from a string; reject obvious placeholders/repetition.
    return (len(key) >= 32 and len(set(key)) >= 12 and not any(c.isspace() for c in key)
            and not any(word in key.lower() for word in ("replace", "example", "changeme", "your-api")))


@dataclass(frozen=True)
class Settings:
    mode: str = "sandbox"
    api_key: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8200
    mcp_port: int = 8000
    target_port: int = 8100
    mcp_url: str = "http://127.0.0.1:8000/mcp"
    target_url: str = "http://127.0.0.1:8100"
    db_path: str = "data/demo.db"
    service: str = "payments"
    mutation_enabled: bool = False
    repo: str = ""
    github_token: str = field(default="", repr=False)
    prometheus_url: str = ""
    prometheus_token: str = field(default="", repr=False)
    prometheus_metric: str = "http_requests_total"
    telemetry_max_age_seconds: int = 120
    rollback_url: str = ""
    rollback_token: str = field(default="", repr=False)
    loki_url: str = ""
    loki_token: str = field(default="", repr=False)
    bedrock_model_id: str = ""
    aws_region: str = "us-east-1"
    aws_profile: str = ""
    # Outbound paging. Configuring a URL is the owner's authorization to send
    # minimal incident metadata (title, service, link) to that endpoint.
    notify_url: str = ""
    notify_token: str = field(default="", repr=False)
    notify_format: str = "json"
    public_url: str = ""
    renotify_minutes: int = 5
    verify_watch_seconds: int = 30
    # Live metrics source. CloudWatch reads two Sum metrics (requests, 5xx) with a
    # single `service` dimension; same unknown-not-healthy contract as Prometheus.
    metrics_source: str = "prometheus"
    cloudwatch_namespace: str = ""
    cloudwatch_requests_metric: str = "Requests"
    cloudwatch_errors_metric: str = "Errors5xx"
    # Unauthenticated, read-only, redacted status page at /status. Off by default.
    public_status: bool = False
    # OAuth 2.1 authorization server on the MCP endpoint (Alexa+ account linking).
    # The operator approves each client on a consent page by entering the API key.
    oauth_enabled: bool = False
    oauth_redirect_hosts: tuple[str, ...] = ("alexa.amazon.com", "pitangui.amazon.com", "layla.amazon.com", "alexa.amazon.co.jp")
    # Statically registered client (Alexa+ does not support dynamic registration).
    # Redirect URIs are the exact list `alexa-ai configure-account-linking` prints.
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    oauth_redirect_uris: tuple[str, ...] = ()
    # Deployment evidence: GitHub deployments API, or a release-controller ledger
    # (GET {deploys_url}?service=... -> {"deployments": [...]}).
    deploys_source: str = "github"
    deploys_url: str = ""
    deploys_token: str = field(default="", repr=False)
    internal_http_hosts: tuple[str, ...] = ()
    # Blast radius: failed checkouts x average order value. 0 = not configured (no $ estimate).
    avg_order_value: float = 0.0
    # Escalation: page the secondary once after N unacknowledged minutes.
    escalation_minutes: int = 10
    secondary_notify_url: str = ""
    secondary_notify_token: str = field(default="", repr=False)
    # (start_hour, end_hour) local server time. Pages still go out; wording says URGENT.
    quiet_hours: tuple[int, int] | None = None
    # Live demo controls (ship the bad release / roll forward to good) via the release controller.
    demo_controls: bool = False
    demo_good_commit: str = "3f9a1c2e7b4d"
    demo_bad_commit: str = "8c2d5e91a0f3"

    def __post_init__(self):
        if self.mode not in ("sandbox", "live"):
            raise ValueError("ONCALL_MODE must be sandbox or live")
        for name in ("port", "mcp_port", "target_port"):
            if not 1 <= getattr(self, name) <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        if len({self.port, self.mcp_port, self.target_port}) != 3:
            raise ValueError("API, MCP and target ports must be distinct")
        if not self.host or "/" in self.host or any(c.isspace() for c in self.host):
            raise ValueError("ONCALL_HOST must be a bind hostname or IP")
        if (self.mode == "live" or not _loopback(self.host) or self.api_key) and not _strong_key(self.api_key):
            raise ValueError("ONCALL_API_KEY must be a randomly generated secret of at least 32 characters")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", self.service):
            raise ValueError("ONCALL_SERVICE must be one literal service identifier")
        if self.repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("ONCALL_REPO must be owner/repository")
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", self.prometheus_metric):
            raise ValueError("ONCALL_PROMETHEUS_METRIC must be a metric name, not a query")
        if not 30 <= self.telemetry_max_age_seconds <= 600:
            raise ValueError("ONCALL_TELEMETRY_MAX_AGE_SECONDS must be between 30 and 600")
        for host in self.internal_http_hosts:
            # Single DNS label only: resolvable on a private network (Compose/VPC
            # service discovery), never a public domain over plaintext HTTP.
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", host):
                raise ValueError("ONCALL_INTERNAL_HTTP_HOSTS entries must be single-label hostnames")
        for name in ("mcp_url", "target_url", "prometheus_url", "rollback_url", "loki_url", "deploys_url"):
            _url(getattr(self, name), name, self.internal_http_hosts)
        for name in ("notify_url", "public_url", "secondary_notify_url"):
            _url(getattr(self, name), name)  # leaves the private network: HTTPS only
        for host in self.oauth_redirect_hosts:
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", host):
                raise ValueError("ONCALL_OAUTH_REDIRECT_HOSTS entries must be hostnames")
        if self.oauth_enabled:
            if not self.api_key:
                raise ValueError("OAuth requires ONCALL_API_KEY (the operator approves clients with it)")
            if not self.public_url.startswith("https://"):
                raise ValueError("OAuth requires an HTTPS ONCALL_PUBLIC_URL (the issuer)")
        if self.oauth_client_id or self.oauth_client_secret or self.oauth_redirect_uris:
            if not re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", self.oauth_client_id):
                raise ValueError("ONCALL_OAUTH_CLIENT_ID must be 8-128 URL-safe characters")
            if not _strong_key(self.oauth_client_secret) or hmac.compare_digest(self.oauth_client_secret, self.api_key):
                raise ValueError("ONCALL_OAUTH_CLIENT_SECRET must be a random secret of at least 32 characters, distinct from the API key")
            if not self.oauth_redirect_uris:
                raise ValueError("ONCALL_OAUTH_REDIRECT_URIS must list the exact redirect URIs for the static client")
            for uri in self.oauth_redirect_uris:
                parts = urlsplit(uri)
                if parts.scheme != "https" or (parts.hostname or "") not in self.oauth_redirect_hosts or parts.fragment:
                    raise ValueError("ONCALL_OAUTH_REDIRECT_URIS must be HTTPS URIs on ONCALL_OAUTH_REDIRECT_HOSTS")
        if self.deploys_source not in ("github", "ledger"):
            raise ValueError("ONCALL_DEPLOYS_SOURCE must be github or ledger")
        if self.deploys_source == "ledger" and not self.deploys_url:
            raise ValueError("ONCALL_DEPLOYS_SOURCE=ledger requires ONCALL_DEPLOYS_URL")
        if self.notify_format not in ("json", "slack", "ntfy"):
            raise ValueError("ONCALL_NOTIFY_FORMAT must be json, slack or ntfy")
        if not 0 <= self.renotify_minutes <= 60:
            raise ValueError("ONCALL_RENOTIFY_MINUTES must be between 0 (disabled) and 60")
        if self.verify_watch_seconds not in WATCH_WINDOWS:
            raise ValueError("ONCALL_VERIFY_WATCH_SECONDS must be one of " + ", ".join(map(str, WATCH_WINDOWS)))
        if not self.db_path or Path(self.db_path).name.lower() == "ops.db":
            raise ValueError("ONCALL_DB_PATH must be nonempty and must not overwrite the original ops.db")
        if not (isinstance(self.avg_order_value, (int, float)) and math.isfinite(self.avg_order_value)
                and 0 <= self.avg_order_value <= 100000):
            raise ValueError("ONCALL_AVG_ORDER_VALUE must be a number between 0 (not configured) and 100000")
        if not 1 <= self.escalation_minutes <= 120:
            raise ValueError("ONCALL_ESCALATION_MINUTES must be between 1 and 120")
        if self.quiet_hours is not None:
            start, end = self.quiet_hours
            if not (0 <= start <= 23 and 0 <= end <= 23 and start != end):
                raise ValueError("ONCALL_QUIET_HOURS must be HH-HH with distinct hours 00-23, e.g. 23-07")
        for name in ("demo_good_commit", "demo_bad_commit"):
            if not re.fullmatch(r"[0-9a-f]{7,64}", getattr(self, name)):
                raise ValueError(f"{name} must be a lowercase hex commit")
        for name in ("api_key", "github_token", "prometheus_token", "rollback_token", "loki_token", "notify_token",
                     "deploys_token", "secondary_notify_token"):
            if any(ord(c) < 32 or ord(c) > 126 for c in getattr(self, name)):
                raise ValueError(f"{name} must contain printable ASCII characters only")
        if self.metrics_source not in ("prometheus", "cloudwatch"):
            raise ValueError("ONCALL_METRICS_SOURCE must be prometheus or cloudwatch")
        for name in ("cloudwatch_requests_metric", "cloudwatch_errors_metric"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", getattr(self, name)):
                raise ValueError(f"{name} must be a CloudWatch metric name")
        if self.cloudwatch_namespace and not re.fullmatch(r"[A-Za-z0-9_./#:-]{1,255}", self.cloudwatch_namespace):
            raise ValueError("ONCALL_CLOUDWATCH_NAMESPACE is not a valid namespace")
        if self.metrics_source == "cloudwatch" and not self.cloudwatch_namespace:
            raise ValueError("CloudWatch metrics require ONCALL_CLOUDWATCH_NAMESPACE")
        if self.mode == "live" and self.deploys_source == "github" and not self.repo:
            raise ValueError("Live mode requires ONCALL_REPO (or ONCALL_DEPLOYS_SOURCE=ledger)")
        if self.mode == "live" and self.metrics_source == "prometheus" and not self.prometheus_url:
            raise ValueError("Live mode requires PROMETHEUS_URL (or ONCALL_METRICS_SOURCE=cloudwatch)")
        if self.mode == "live" and self.mutation_enabled and (not self.rollback_url or not self.rollback_token):
            raise ValueError("Live mutations require ONCALL_ROLLBACK_URL and ONCALL_ROLLBACK_TOKEN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, dotenv_path: Path | str | None = None) -> "Settings":
        # No directory search, interpolation, or mutation of os.environ. Process env wins.
        env = dict(dotenv_values(dotenv_path, interpolate=False)) if dotenv_path else {}
        env.update(os.environ if environ is None else environ)
        def text(name, default=""):
            return str(env.get(name) or default).strip()
        def integer(name, default):
            try:
                return int(text(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None
        def number(name, default):
            try:
                return float(text(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
        def flag(name):
            value = text(name, "false").lower()
            if value not in ("true", "false", "1", "0"):
                raise ValueError(f"{name} must be true or false")
            return value in ("true", "1")
        quiet = text("ONCALL_QUIET_HOURS")
        if quiet:
            match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", quiet)
            if not match:
                raise ValueError("ONCALL_QUIET_HOURS must be HH-HH, e.g. 23-07")
            quiet = (int(match.group(1)), int(match.group(2)))
        mutation = text("ONCALL_MUTATION_ENABLED", "false").lower()
        if mutation not in ("true", "false", "1", "0"):
            raise ValueError("ONCALL_MUTATION_ENABLED must be true or false")
        mcp_port = integer("ONCALL_MCP_PORT", 8000)
        target_port = integer("ONCALL_TARGET_PORT", 8100)
        mode = text("ONCALL_MODE", "sandbox")
        return cls(mode=mode, api_key=text("ONCALL_API_KEY"), host=text("ONCALL_HOST", "127.0.0.1"),
            port=integer("ONCALL_PORT", 8200), mcp_port=mcp_port, target_port=target_port,
            mcp_url=text("ONCALL_MCP_URL", f"http://127.0.0.1:{mcp_port}/mcp"),
            target_url=text("ONCALL_TARGET_URL", f"http://127.0.0.1:{target_port}"),
            db_path=text("ONCALL_DB_PATH", "data/demo.db" if mode == "sandbox" else "data/live.db"),
            service=text("ONCALL_SERVICE", "payments"), mutation_enabled=mutation in ("true", "1"),
            repo=text("ONCALL_REPO"), github_token=text("GITHUB_TOKEN"),
            prometheus_url=text("PROMETHEUS_URL"), prometheus_token=text("PROMETHEUS_TOKEN"),
            prometheus_metric=text("ONCALL_PROMETHEUS_METRIC", "http_requests_total"),
            telemetry_max_age_seconds=integer("ONCALL_TELEMETRY_MAX_AGE_SECONDS", 120),
            rollback_url=text("ONCALL_ROLLBACK_URL"), rollback_token=text("ONCALL_ROLLBACK_TOKEN"),
            loki_url=text("LOKI_URL"), loki_token=text("LOKI_TOKEN"),
            bedrock_model_id=text("ONCALL_BEDROCK_MODEL_ID"), aws_region=text("AWS_REGION", "us-east-1"),
            aws_profile=text("AWS_PROFILE"),
            notify_url=text("ONCALL_NOTIFY_URL"), notify_token=text("ONCALL_NOTIFY_TOKEN"),
            notify_format=text("ONCALL_NOTIFY_FORMAT", "json").lower(), public_url=text("ONCALL_PUBLIC_URL"),
            renotify_minutes=integer("ONCALL_RENOTIFY_MINUTES", 5),
            verify_watch_seconds=integer("ONCALL_VERIFY_WATCH_SECONDS", 30),
            metrics_source=text("ONCALL_METRICS_SOURCE", "prometheus").lower(),
            cloudwatch_namespace=text("ONCALL_CLOUDWATCH_NAMESPACE"),
            cloudwatch_requests_metric=text("ONCALL_CLOUDWATCH_REQUESTS_METRIC", "Requests"),
            cloudwatch_errors_metric=text("ONCALL_CLOUDWATCH_ERRORS_METRIC", "Errors5xx"),
            public_status=text("ONCALL_PUBLIC_STATUS", "false").lower() in ("true", "1"),
            oauth_enabled=text("ONCALL_OAUTH_ENABLED", "false").lower() in ("true", "1"),
            oauth_redirect_hosts=tuple(h.strip().lower() for h in text(
                "ONCALL_OAUTH_REDIRECT_HOSTS", "alexa.amazon.com,pitangui.amazon.com,layla.amazon.com,alexa.amazon.co.jp").split(",") if h.strip()),
            oauth_client_id=text("ONCALL_OAUTH_CLIENT_ID"), oauth_client_secret=text("ONCALL_OAUTH_CLIENT_SECRET"),
            oauth_redirect_uris=tuple(u.strip() for u in text("ONCALL_OAUTH_REDIRECT_URIS").split(",") if u.strip()),
            deploys_source=text("ONCALL_DEPLOYS_SOURCE", "github").lower(),
            deploys_url=text("ONCALL_DEPLOYS_URL"), deploys_token=text("ONCALL_DEPLOYS_TOKEN"),
            internal_http_hosts=tuple(h.strip().lower() for h in text("ONCALL_INTERNAL_HTTP_HOSTS").split(",") if h.strip()),
            avg_order_value=number("ONCALL_AVG_ORDER_VALUE", 0),
            escalation_minutes=integer("ONCALL_ESCALATION_MINUTES", 10),
            secondary_notify_url=text("ONCALL_SECONDARY_NOTIFY_URL"),
            secondary_notify_token=text("ONCALL_SECONDARY_NOTIFY_TOKEN"),
            quiet_hours=quiet or None, demo_controls=flag("ONCALL_DEMO_CONTROLS"),
            demo_good_commit=text("ONCALL_DEMO_GOOD_COMMIT", "3f9a1c2e7b4d"),
            demo_bad_commit=text("ONCALL_DEMO_BAD_COMMIT", "8c2d5e91a0f3"))


def load_settings() -> Settings:
    """Load exactly OnCall/.env if present, then overlay process environment."""
    return Settings.from_env(dotenv_path=ROOT / ".env")


settings = load_settings()
