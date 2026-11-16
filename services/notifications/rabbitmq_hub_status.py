"""Message Hub behaviour for RabbitMQManager: SSE fan-out, shutdown, presence heartbeats, peer monitoring and live status snapshots."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx

try:
    import aio_pika
except ImportError:  # optional dependency; hub probes are skipped without it
    aio_pika = None

from services.notifications.rabbitmq_hub_snapshot import describe_items, fetch_broker_state, peer_alive_flags
from services.notifications.rabbitmq_config import (
    DEFAULT_SERVICE_KEY,
    get_rabbitmq_host,
    get_rabbitmq_port,
    get_rabbitmq_management_port,
    get_hub_status_cache_ttl,
    hub_peer_http_probe_enabled,
    get_rabbitmq_user,
    get_rabbitmq_password,
    get_rabbitmq_vhost,
    get_events_exchange,
    get_presence_exchange,
    HUB_APPLICATIONS,
)

logger = logging.getLogger(__name__)


class HubStatusMixin:
    """Methods mixed into RabbitMQManager; relies on its connection/registry attributes."""

    # ------------------------------------------------------------------
    # Message Hub: SSE fan-out, shutdown, presence, probe, live status
    # ------------------------------------------------------------------
    def subscribe_sse(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=25)
        self._sse_subscribers.add(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue) -> None:
        self._sse_subscribers.discard(q)

    def _broadcast_to_sse(self, status_payload: Optional[Dict[str, Any]]) -> None:
        for q in list(self._sse_subscribers):
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(status_payload)
            except Exception:
                self._sse_subscribers.discard(q)

    def notify_shutting_down(self) -> None:
        """Wakes all SSE streams so Uvicorn exits without hanging, and announces offline to peers."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._broadcast_to_sse(None)
        try:
            asyncio.get_running_loop().create_task(self.close())
        except Exception:
            pass

    def install_uvicorn_shutdown_hook(self) -> None:
        """Hook Uvicorn's Server.handle_exit so SIGINT/SIGTERM closes SSE streams and broadcasts offline."""
        try:
            import uvicorn.server
            orig_handle_exit = uvicorn.server.Server.handle_exit
            if getattr(orig_handle_exit, "_rmq_hooked", False):
                return
            mgr = self

            def _hooked_handle_exit(server_self, sig, frame):
                mgr.notify_shutting_down()
                return orig_handle_exit(server_self, sig, frame)

            _hooked_handle_exit._rmq_hooked = True  # type: ignore[attr-defined]
            uvicorn.server.Server.handle_exit = _hooked_handle_exit  # type: ignore[assignment]
        except Exception:
            pass

    async def broadcast_server_status_update(self, target_service: str, status: str, reason: str = "status_changed") -> None:
        """Publishes a server online/offline message on the presence exchange so every peer updates its registry."""
        app_cfg = HUB_APPLICATIONS.get(target_service) or HUB_APPLICATIONS[DEFAULT_SERVICE_KEY]
        if status == "offline":
            event_type = "service.presence.offline"
        elif reason == "server_connected":
            event_type = "service.presence.online"
        else:
            event_type = "service.presence.heartbeat"
        payload = {
            "eventType": event_type,
            "service": target_service,
            "name": app_cfg["name"],
            "instanceId": self.instance_id if target_service == self.service_key else f"{target_service}-peer-detected",
            "status": status,
            "reason": reason,
            "reportedBy": self.service_key,
            "port": app_cfg["port"],
            "queues": app_cfg["queues"],
            "streams": app_cfg["streams"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._presence_registry[target_service] = {**payload, "received_at_epoch": time.time()}
        self._known_peer_online[target_service] = status == "online"

        if self.is_connected and self._channel:
            try:
                presence_ex = self._presence_exchange or await self._channel.get_exchange(get_presence_exchange())
                await presence_ex.publish(
                    aio_pika.Message(
                        body=json.dumps(payload).encode("utf-8"),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type="application/json",
                    ),
                    routing_key=f"presence.{target_service}.{status}",
                )
            except Exception as exc:
                logger.debug(f"Failed publishing to presence exchange: {exc}")

    async def _publish_presence_event(self, status: str = "online", reason: str = "heartbeat"):
        await self.broadcast_server_status_update(self.service_key, status=status, reason=reason)

    async def _presence_heartbeat_loop(self):
        """Announces this server's live status every 5 seconds."""
        while True:
            try:
                if self.is_connected:
                    await self._publish_presence_event("online", reason="heartbeat")
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Presence heartbeat loop error: {exc}")
                await asyncio.sleep(5.0)

    async def _peer_status_monitor_loop(self):
        """Every 2.5s re-evaluates all hub servers; pushes SSE updates and announces peers that dropped/returned."""
        while True:
            try:
                await asyncio.sleep(2.5)
                if not self.is_connected:
                    continue
                snapshot = await self.get_hub_live_status(active_test=False)
                state_changed = False
                for srv in snapshot.get("servers", []):
                    srv_id = srv["id"]
                    is_now_online = bool(srv["online"])
                    was_online = self._known_peer_online.get(srv_id)
                    if was_online is None:
                        self._known_peer_online[srv_id] = is_now_online
                    elif was_online != is_now_online:
                        state_changed = True
                        self._known_peer_online[srv_id] = is_now_online
                        new_st = "online" if is_now_online else "offline"
                        logger.info(f"[MessageHub] Server '{srv['name']}' ({srv_id}) is now {new_st}")
                        if srv_id != self.service_key:
                            await self.broadcast_server_status_update(
                                srv_id, status=new_st,
                                reason="peer_reconnected" if is_now_online else "peer_disconnected",
                            )
                if state_changed:
                    self._broadcast_to_sse(await self.get_hub_live_status(active_test=False))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"Peer status monitor error: {exc}")

    async def _run_active_queue_and_stream_probe(self) -> Dict[str, Any]:
        """Round-trips a message through a temporary queue and publishes a healthcheck event to the events exchange."""
        results: Dict[str, Any] = {
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "queue_roundtrip_ok": False,
            "queue_roundtrip_ms": None,
            "stream_publish_ok": False,
            "stream_roundtrip_ms": None,
        }
        if not self.is_connected or not self._connection:
            return results
        try:
            probe_channel = await self._connection.channel(publisher_confirms=True)
            try:
                t0 = time.perf_counter()
                probe_id = uuid4().hex
                temp_queue = await probe_channel.declare_queue(
                    f"hub.probe.{self.service_key}.{probe_id[:8]}", durable=False, exclusive=True, auto_delete=True,
                )
                await probe_channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps({"probe_id": probe_id, "service": self.service_key}).encode("utf-8"),
                        content_type="application/json",
                    ),
                    routing_key=temp_queue.name,
                )
                incoming = await temp_queue.get(timeout=2.0, fail=False)
                if incoming:
                    await incoming.ack()
                    if json.loads(incoming.body.decode("utf-8")).get("probe_id") == probe_id:
                        results["queue_roundtrip_ok"] = True
                        results["queue_roundtrip_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                await temp_queue.delete(if_unused=False, if_empty=False)

                t1 = time.perf_counter()
                events_exchange = await probe_channel.get_exchange(get_events_exchange())
                await events_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps({
                            "probe_id": probe_id,
                            "eventType": f"{self.service_key}.stream.healthcheck",
                            "service": self.service_key,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }).encode("utf-8"),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        content_type="application/json",
                    ),
                    routing_key=f"{self.service_key}.healthcheck",
                )
                await self._publish_presence_event("online", reason="heartbeat")
                results["stream_publish_ok"] = True
                results["stream_roundtrip_ms"] = round((time.perf_counter() - t1) * 1000, 2)
            finally:
                await probe_channel.close()
        except Exception as exc:
            logger.warning(f"Active queue/stream probe error: {exc}")
        self._last_active_test = results
        return results

    async def get_hub_live_status(self, active_test: bool = False, force: bool = False) -> Dict[str, Any]:
        """Live Message Hub snapshot, cached for HUB_STATUS_CACHE_TTL seconds with single-flight refresh.

        Each uncached build costs 3 RabbitMQ management calls (plus optional peer /health/live probes), so
        the monitor loop, status polls and SSE connects share one result. ``active_test`` and ``force``
        bypass the cache (explicit user test / presence change).
        """
        ttl = get_hub_status_cache_ttl()
        bypass = active_test or force or ttl <= 0
        if not bypass and self._hub_status_cache is not None and time.monotonic() - self._hub_status_cached_at < ttl:
            return self._hub_status_cache

        async with self._hub_status_lock:
            # A concurrent caller may have refreshed while we waited for the lock
            if (
                not bypass
                and self._hub_status_cache is not None
                and time.monotonic() - self._hub_status_cached_at < ttl
            ):
                return self._hub_status_cache
            snapshot = await self._build_hub_live_status(active_test=active_test)
            self._hub_status_cache = snapshot
            self._hub_status_cached_at = time.monotonic()
            return snapshot

    async def _build_hub_live_status(self, active_test: bool = False) -> Dict[str, Any]:
        """Live Message Hub snapshot (same shape as the other portal backends) for the topbar servers dropdown."""
        if not self.is_connected and not self._shutting_down and time.monotonic() - self._last_reconnect_attempt > 10.0:
            self._last_reconnect_attempt = time.monotonic()
            await self.connect(max_retries=1, retry_delay=0.3)

        if active_test and self.is_connected:
            await self._run_active_queue_and_stream_probe()

        host = get_rabbitmq_host()
        amqp_port = get_rabbitmq_port()
        mgmt_port = get_rabbitmq_management_port()
        vhost = get_rabbitmq_vhost()
        now_iso = datetime.now(timezone.utc).isoformat()
        now_epoch = time.time()

        # 1-2. Management HTTP API (or AMQP passive checks): connections and queue/stream stats
        broker_connections, broker_queues, broker_version, mgmt_reachable = await fetch_broker_state(self)

        # 3. Peer liveness (optional HTTP health probes, HUB_PEER_HTTP_PROBE; off by default)
        alive_flags = await peer_alive_flags(self)

        def _describe(names: List[str], primary: str, want_stream: bool):
            return describe_items(broker_queues, names, primary, want_stream)

        servers_list: List[Dict[str, Any]] = []
        for (app_key, app_meta), http_alive in zip(HUB_APPLICATIONS.items(), alive_flags):
            svc_user = app_meta["service_user"]
            is_self = app_key == self.service_key

            matching_conns = []
            for conn in broker_connections:
                props = conn.get("client_properties") or {}
                if (
                    conn.get("user", "") == svc_user
                    or props.get("service") == app_key
                    or (props.get("connection_name") or "").startswith(f"{app_key}:")
                ):
                    matching_conns.append(conn)

            queue_items, queues_exist, primary_q_consumers = _describe(app_meta["queues"], app_meta["primary_queue"], False)
            stream_items, streams_exist, primary_s_consumers = _describe(app_meta["streams"], app_meta["primary_stream"], True)

            entry = self._presence_registry.get(app_key)
            explicitly_offline = recent_heartbeat = False
            last_seen = None
            if entry:
                age = now_epoch - float(entry.get("received_at_epoch", 0))
                last_seen = entry.get("timestamp")
                if entry.get("status") == "offline" and age <= 30.0:
                    explicitly_offline = True
                elif age <= 15.0 and entry.get("status") == "online":
                    recent_heartbeat = True

            # Queue/stream consumer counts are NOT evidence a peer is up: a server may run
            # in-process processors for peers' queues. Only the peer's own broker connection
            # or a fresh presence heartbeat counts.
            has_hub_connection = (
                bool(matching_conns)
                or (is_self and self.is_connected)
                or recent_heartbeat
            )
            queues_working = queues_exist and bool(queue_items) and all(i["working"] for i in queue_items)
            streams_working = streams_exist and bool(stream_items) and all(i["working"] for i in stream_items)

            app_online = bool(self.is_connected and not explicitly_offline and http_alive and has_hub_connection)
            if app_online and queues_working and streams_working:
                app_status = "online"
            elif app_online:
                app_status = "degraded"
            else:
                app_status = "offline"

            client_names = [
                (c.get("client_properties") or {}).get("connection_name") or c.get("name", "")
                for c in matching_conns
            ]
            servers_list.append({
                "id": app_key,
                "name": app_meta["name"],
                "service_user": svc_user,
                "port": app_meta["port"],
                "is_self": is_self,
                "online": app_status == "online",
                "status": app_status,
                "connection_state": "CONNECTED" if app_online else "DISCONNECTED",
                "connections_count": max(len(matching_conns), 1) if app_online else 0,
                "client_name": client_names[0] if (client_names and app_online) else (f"{app_key}:{self.instance_id}" if is_self and app_online else None),
                "queues": {
                    "working": bool(queues_working and app_online),
                    "topology_ready": queues_working,
                    "status": "online" if (queues_working and app_online) else "offline",
                    "consumers": primary_q_consumers if app_online else 0,
                    "items": queue_items,
                    "roundtrip_ms": self._last_active_test.get("queue_roundtrip_ms") if (is_self and app_online) else None,
                },
                "streams": {
                    "working": bool(streams_working and app_online),
                    "topology_ready": streams_working,
                    "status": "online" if (streams_working and app_online) else "offline",
                    "consumers": primary_s_consumers if app_online else 0,
                    "items": stream_items,
                    "roundtrip_ms": self._last_active_test.get("stream_roundtrip_ms") if (is_self and app_online) else None,
                },
                "last_seen": last_seen or (now_iso if app_online else None),
            })

        return self._hub_summary(servers_list, now_iso, host, amqp_port, mgmt_port, vhost,
                                 broker_version, mgmt_reachable, len(broker_connections))

    def _hub_summary(self, servers_list, now_iso, host, amqp_port, mgmt_port, vhost,
                     broker_version, mgmt_reachable, total_connections) -> Dict[str, Any]:
        """Top-level hub payload wrapping the per-server entries."""
        online_count = sum(1 for s in servers_list if s["online"])
        total_count = len(servers_list)
        if self.is_connected and online_count == total_count:
            hub_status = "online"
        elif self.is_connected and online_count > 0:
            hub_status = "degraded"
        else:
            hub_status = "offline"

        return {
            "status": hub_status,
            "connected": self.is_connected,
            "checked_at": now_iso,
            "hub": {
                "name": "RabbitMQ Message Hub",
                "status": "online" if self.is_connected else "offline",
                "connected": self.is_connected,
                "host": f"{host}:{amqp_port}",
                "management_url": f"http://{host}:{mgmt_port}",
                "management_api_reachable": mgmt_reachable,
                "vhost": vhost,
                "version": broker_version,
                "total_connections": total_connections,
                "online_servers": online_count,
                "total_servers": total_count,
            },
            "active_test": self._last_active_test or None,
            "servers": servers_list,
        }
