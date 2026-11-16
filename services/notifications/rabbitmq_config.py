"""RabbitMQ / Message Hub configuration: env-driven connection settings, exchange names and the hub server registry."""

import os
import re
from typing import Any, Dict
from urllib.parse import quote, unquote, urlparse

DEFAULT_SERVICE_KEY = "sec_filing"
DEFAULT_SERVICE_DISPLAY_NAME = "SEC Filing (Share Document)"


def _sanitize_amqp_url(url: str) -> str:
    """Mask password from AMQP URLs to prevent logging credentials."""
    if not url:
        return ""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)


def _explicit_url_parts():
    """Parsed RABBITMQ_URL, or None if unset."""
    explicit = os.getenv("RABBITMQ_URL")
    if not explicit:
        return None
    try:
        return urlparse(explicit)
    except Exception:
        return None


def get_rabbitmq_host() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.hostname:
        return parsed.hostname
    return os.getenv("RABBITMQ_HOST", "127.0.0.1")


def get_rabbitmq_port() -> int:
    parsed = _explicit_url_parts()
    try:
        if parsed and parsed.port:
            return int(parsed.port)
    except ValueError:
        pass
    try:
        return int(os.getenv("RABBITMQ_PORT", "5672"))
    except (ValueError, TypeError):
        return 5672


def get_rabbitmq_management_port() -> int:
    try:
        return int(os.getenv("RABBITMQ_MANAGEMENT_PORT", "15672"))
    except (ValueError, TypeError):
        return 15672


def get_hub_status_cache_ttl() -> float:
    """Seconds a hub snapshot is reused (0 disables caching)."""
    try:
        return max(0.0, float(os.getenv("HUB_STATUS_CACHE_TTL", "3")))
    except (ValueError, TypeError):
        return 3.0


def hub_peer_http_probe_enabled() -> bool:
    """Whether peers must also pass an HTTP /health/live probe to count as online (default off: hub presence is authoritative)."""
    return os.getenv("HUB_PEER_HTTP_PROBE", "false").strip().lower() in ("1", "true", "yes", "on")


def get_rabbitmq_user() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.username:
        return unquote(parsed.username)
    return (
        os.getenv("RABBITMQ_DEFAULT_USER")
        or os.getenv("RABBITMQ_USERNAME")
        or os.getenv("RABBITMQ_USER")
        or "guest"
    )


def get_rabbitmq_password() -> str:
    parsed = _explicit_url_parts()
    if parsed and parsed.password:
        return unquote(parsed.password)
    return (
        os.getenv("RABBITMQ_DEFAULT_PASS")
        or os.getenv("RABBITMQ_PASSWORD")
        or os.getenv("RABBITMQ_PASS")
        or "guest"
    )


def get_rabbitmq_vhost() -> str:
    """Virtual host: from RABBITMQ_URL path, else RABBITMQ_VHOST / RABBITMQ_DEFAULT_VHOST, else '/'."""
    parsed = _explicit_url_parts()
    if parsed:
        path = (parsed.path or "").lstrip("/")
        if path:
            return unquote(path)
    return os.getenv("RABBITMQ_DEFAULT_VHOST") or os.getenv("RABBITMQ_VHOST") or "/"


def _vhost_url_part(vhost: str) -> str:
    return quote(vhost, safe="") if vhost and vhost != "/" else ""


def get_rabbitmq_url() -> str:
    explicit = os.getenv("RABBITMQ_URL")
    if explicit:
        return explicit
    host = get_rabbitmq_host()
    port = get_rabbitmq_port()
    user = get_rabbitmq_user()
    password = get_rabbitmq_password()
    return f"amqp://{user}:{password}@{host}:{port}/{_vhost_url_part(get_rabbitmq_vhost())}"


def get_commands_exchange() -> str:
    return os.getenv("COMMANDS_EXCHANGE", "ceo.commands")


def get_events_exchange() -> str:
    return os.getenv("EVENTS_EXCHANGE", "service.events")


def get_dlx_exchange() -> str:
    return os.getenv("DLX_EXCHANGE", "ceo.commands.dlx")


def get_presence_exchange() -> str:
    return os.getenv("PRESENCE_EXCHANGE", "hub.presence")


def get_prefetch_count() -> int:
    try:
        return int(os.getenv("RABBITMQ_PREFETCH", "20"))
    except (ValueError, TypeError):
        return 20


# Backward-compatibility module constants
COMMANDS_EXCHANGE_NAME = get_commands_exchange()
EVENTS_EXCHANGE_NAME = get_events_exchange()
DLX_EXCHANGE_NAME = get_dlx_exchange()
PRESENCE_EXCHANGE_NAME = get_presence_exchange()
PREFETCH_COUNT = get_prefetch_count()
RABBITMQ_HOST = get_rabbitmq_host()
RABBITMQ_PORT = get_rabbitmq_port()
RABBITMQ_USER = get_rabbitmq_user()
RABBITMQ_PASS = get_rabbitmq_password()
RABBITMQ_URL = get_rabbitmq_url()

# Message Hub registry: identical across all portal backends so every server's dropdown lists the same servers.
_HUB_SERVICES = [
    # (id, display name, rabbitmq user, env prefix, default backend port)
    ("ma", "M&A", "acquisition_service", "M7A", "8003"),
    ("administration", "Administration", "administration_service", "ADMINISTRATION", "8002"),
    ("ceo", "CEO Dashboard", "ceo_service", "CEO", "8000"),
    ("social_media_compliance", "Social Media Compliance", "compliance_service", "SOCIAL_MEDIA_COMPLIANCE", "8009"),
    ("finance", "Finance (Enterprise System)", "finance_service", "FINANCE", "8001"),
    ("marketing", "Marketing Management", "marketing_service", "MARKETING", "8010"),
    ("sec_filing", "SEC Filing (Share Document)", "sec_filing_service", "SEC_FILING", "8006"),
    ("travel", "Travel Management", "travel_service", "TRAVEL", "8008"),
    ("real_estate", "Real Estate", "real_estate_service", "REAL_ESTATE", "8007"),
]

# Services whose command/DLQ/event queues this server declares (queue arguments must match peers').
_TOPOLOGY_SERVICES = ("travel", "marketing", "administration", "ma", "ceo", "sec_filing")


def _build_hub_application(app_id: str, name: str, user: str, prefix: str, default_port: str) -> Dict[str, Any]:
    # This service uses its own PORT; peers use <PREFIX>_PORT / <PREFIX>_BACKEND_URL from the environment.
    if app_id == DEFAULT_SERVICE_KEY:
        port = int(os.getenv("PORT", default_port))
        base_url = f"http://127.0.0.1:{port}"
    else:
        port = int(os.getenv(f"{prefix}_PORT", default_port))
        base_url = os.getenv(f"{prefix}_BACKEND_URL", f"http://127.0.0.1:{port}").rstrip("/")
    return {
        "id": app_id,
        "name": name,
        "service_user": user,
        "port": port,
        "health_url": f"{base_url}/health/live",
        "queues": [f"{app_id}.commands", f"{app_id}.commands.dlq", f"ceo.{app_id}.events"],
        "primary_queue": f"{app_id}.commands",
        "streams": [f"{app_id}.events.stream", "hub.presence.stream"],
        "primary_stream": f"{app_id}.events.stream",
    }


HUB_APPLICATIONS: Dict[str, Dict[str, Any]] = {
    svc[0]: _build_hub_application(*svc) for svc in _HUB_SERVICES
}
