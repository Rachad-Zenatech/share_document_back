"""Message Hub snapshot helpers: broker state (management API / AMQP), peer liveness and queue descriptions."""

import asyncio
import logging
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import httpx

from services.notifications.rabbitmq_config import (
    HUB_APPLICATIONS,
    get_rabbitmq_host,
    get_rabbitmq_management_port,
    get_rabbitmq_password,
    get_rabbitmq_user,
    get_rabbitmq_vhost,
    hub_peer_http_probe_enabled,
)

logger = logging.getLogger(__name__)


async def fetch_broker_state(manager) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], str, bool]:
    """Live connections, queue/stream stats, broker version and whether the management API answered."""
    host = get_rabbitmq_host()
    mgmt_port = get_rabbitmq_management_port()
    vhost = get_rabbitmq_vhost()
    vhost_encoded = quote(vhost, safe="")
    broker_connections: List[Dict[str, Any]] = []
    broker_queues: Dict[str, Dict[str, Any]] = {}
    broker_version = "unknown"
    mgmt_reachable = False
    # 1. Management HTTP API: live connections and queue/stream stats
    mgmt_base = f"http://{host}:{mgmt_port}/api"
    try:
        async with httpx.AsyncClient(timeout=1.5, auth=(get_rabbitmq_user(), get_rabbitmq_password())) as client:
            overview_resp, conns_resp, queues_resp = await asyncio.gather(
                client.get(f"{mgmt_base}/overview"),
                client.get(f"{mgmt_base}/connections"),
                client.get(f"{mgmt_base}/queues/{vhost_encoded}"),
                return_exceptions=True,
            )
            if not isinstance(overview_resp, Exception) and overview_resp.status_code == 200:
                mgmt_reachable = True
                broker_version = overview_resp.json().get("rabbitmq_version") or broker_version
            if not isinstance(conns_resp, Exception) and conns_resp.status_code == 200:
                mgmt_reachable = True
                broker_connections = [c for c in conns_resp.json() if c.get("vhost") == vhost or not vhost]
            if not isinstance(queues_resp, Exception) and queues_resp.status_code == 200:
                mgmt_reachable = True
                for q in queues_resp.json():
                    if q.get("name"):
                        broker_queues[q["name"]] = q
    except Exception as exc:
        logger.debug(f"Management API query failed, falling back to AMQP passive checks: {exc}")

    # 2. Fallback: passive-declare the hub queues over AMQP
    if not broker_queues and manager.is_connected and manager._connection:
        try:
            check_channel = await manager._connection.channel()
            try:
                names = {n for app in HUB_APPLICATIONS.values() for n in (*app["queues"], *app["streams"])}
                for q_name in sorted(names):
                    try:
                        decl = await check_channel.declare_queue(q_name, passive=True)
                        broker_queues[q_name] = {
                            "name": q_name,
                            "type": "stream" if q_name.endswith(".stream") else "classic",
                            "state": "running",
                            "consumers": getattr(decl.declaration_result, "consumer_count", 0),
                            "messages": getattr(decl.declaration_result, "message_count", 0),
                        }
                    except Exception:
                        # A failed passive declare closes the channel; open a fresh one for the next name
                        try:
                            check_channel = await manager._connection.channel()
                        except Exception:
                            break
            finally:
                try:
                    await check_channel.close()
                except Exception:
                    pass
        except Exception:
            pass
    return broker_connections, broker_queues, broker_version, mgmt_reachable


async def peer_alive_flags(manager) -> List[bool]:
    """Per hub application: is it considered alive (own connection, or optional HTTP /health/live probe for peers)."""
    # 3. Optional peer HTTP health checks (HUB_PEER_HTTP_PROBE) run concurrently; off by default
    probe_peers = hub_peer_http_probe_enabled()

    async def _http_alive(app_meta: Dict[str, Any], is_self: bool) -> bool:
        if is_self:
            return manager.is_connected
        if not probe_peers:
            return True  # hub presence/consumers decide; has_hub_connection still gates online
        try:
            async with httpx.AsyncClient(timeout=0.7) as hc:
                return (await hc.get(app_meta["health_url"])).status_code in (200, 307, 308)
        except Exception:
            return False

    alive_flags = await asyncio.gather(
        *[_http_alive(m, k == manager.service_key) for k, m in HUB_APPLICATIONS.items()]
    )
    return list(alive_flags)


def describe_items(broker_queues: Dict[str, Dict[str, Any]], names: List[str], primary: str, want_stream: bool):
    """Per-queue/stream status items, whether all exist, and the primary one's consumer count."""
    items, all_exist, primary_consumers = [], True, 0
    for name in names:
        info = broker_queues.get(name)
        if info:
            state = info.get("state", "running")
            typ = info.get("type") or (info.get("arguments") or {}).get("x-queue-type", "stream" if want_stream else "classic")
            consumers = int(info.get("consumers") or 0)
            if name == primary:
                primary_consumers = consumers
            working = state == "running" and (typ == "stream" if want_stream else True)
            items.append({"name": name, "type": typ, "state": state, "consumers": consumers,
                          "messages": int(info.get("messages") or 0), "working": working})
        else:
            all_exist = False
            items.append({"name": name, "type": "stream" if want_stream else "classic", "state": "missing",
                          "consumers": 0, "messages": 0, "working": False})
    return items, all_exist, primary_consumers
