#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Version 4.0 emergency_broker -> MiniBroker multi-nodo: Meshtastic Serial + TCP + MeshCore.

Diseño:
- N nodos activos simultáneamente, cada uno con su propio manager y sendq.
- Tipos de nodo soportados: meshtastic_serial, meshtastic_tcp, meshcore_serial.
- Todos los nodos publican eventos al mismo JsonlHub (campo node_id identifica origen).
- ChannelRouter replica mensajes entre nodos según reglas configurables.
- Cola de envíos persiste mensajes mientras el nodo está caído y los reenvía al reconectar.
- ControlServer: SEND_TEXT acepta node_id opcional; NODE_LIST y NODE_STATUS para gestión.

Configuración (variables de entorno NODE_N_*):
    NODE_1_TYPE=meshtastic_serial
    NODE_1_ALIAS=local-usb
    NODE_1_PORT=/dev/ttyV0
    NODE_1_PRIMARY=1

    NODE_2_TYPE=meshtastic_tcp
    NODE_2_ALIAS=remoto-wifi
    NODE_2_HOST=192.168.1.50
    NODE_2_TCP_PORT=4403

    NODE_3_TYPE=meshcore_serial
    NODE_3_ALIAS=meshcore-zgz
    NODE_3_PORT=/dev/ttyUSB1

    # Routing: src_alias:canal -> dst_alias:canal
    NODE_ROUTE_1=src:local-usb:0,dst:remoto-wifi:0
    NODE_ROUTE_2=src:remoto-wifi:0,dst:local-usb:0

Compatibilidad hacia atrás (v2/v3):
    Si no existe NODE_1_TYPE en el entorno, el broker arranca en modo legacy
    usando MESH_CONN_MODE / MESH_USB_PORT / MESH_TCP_HOST como en v3.0.
"""
from __future__ import annotations

import abc
import argparse
import asyncio
import json
import os
import queue
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


# ======================================================================================
# Utilidades generales
# ======================================================================================

def log(msg: str) -> None:
    """Log estándar con timestamp local."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] {msg}", flush=True)


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return truthy(value, default)
    return default


def json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def sanitize_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


# ======================================================================================
# Descriptor de nodo
# ======================================================================================

NODE_TYPE_MESH_SERIAL = "meshtastic_serial"
NODE_TYPE_MESH_TCP    = "meshtastic_tcp"
NODE_TYPE_MC_SERIAL   = "meshcore_serial"
NODE_TYPE_MC_TCP      = "meshcore_tcp"
_VALID_NODE_TYPES     = {NODE_TYPE_MESH_SERIAL, NODE_TYPE_MESH_TCP,
                         NODE_TYPE_MC_SERIAL,   NODE_TYPE_MC_TCP}


@dataclass
class NodeDescriptor:
    """
    Descripción completa de un nodo de radio conectado al broker.

    Args:
        node_id:   Identificador interno (ej. "node_1"). Asignado por el loader.
        alias:     Nombre legible y único (ej. "local-usb").
        node_type: Tipo de nodo; uno de NODE_TYPE_*.
        primary:   Si True, es el destino por defecto de SEND_TEXT sin node_id.
        port:      Puerto serie (/dev/ttyV0) o equivalente según el tipo.
        baud:      Baudrate serie.
        no_proto:  noProto flag del SDK Meshtastic.
        usb_lock_enable: Si True, usa flock para evitar doble apertura del puerto.
        tcp_host:  Hostname/IP para nodos TCP.
        tcp_port:  Puerto TCP (defecto 4403).
        tcp_dead_silence_sec: Segundos de silencio antes de declarar enlace TCP muerto.
        dead_link_silence_sec: Segundos de silencio + path ausente = enlace serial muerto.
        link_check_interval_sec: Intervalo del healthcheck.
        cooldown_secs: Cooldown base tras desconexión.
        force_reconnect_grace_sec: Cooldown corto para FORCE_RECONNECT.
        early_drop_window_sec: Ventana de caída temprana.
        early_drop_cooldown_sec: Cooldown escalado en caída temprana.
        reconnect_min_sec: Mínimo backoff entre reintentos.
        reconnect_max_sec: Máximo backoff entre reintentos.
        send_queue_max: Tamaño máximo de la cola de envíos.
        tx_block_during_cooldown: Si True, bloquea TX durante cooldown.
        mc_baud: Baudrate para nodos MeshCore.
        mc_channel_map: Mapa canales Mesh->MeshCore.
        mc_contact_to_ch: Mapa contacto->canal.
        mc_chanidx_to_ch: Mapa chanidx->canal.
        mc_aliases: Aliases de contactos MeshCore.
        mc_rx_prefix_style: "alias" o "prefix" para el encabezado RX.
        mc_default_ch: Canal Meshtastic por defecto para MeshCore sin mapeo.
        mc_inject_dedup_sec: Ventana de deduplicación inyección MeshCore->Mesh.
        mc_silence_reconnect_sec: Segundos de silencio antes de reconectar MeshCore.
        mc_max_text_bytes: Límite de bytes por fragmento MeshCore.
    """
    node_id:   str   = ""
    alias:     str   = ""
    node_type: str   = NODE_TYPE_MESH_SERIAL
    primary:   bool  = False

    port:     str  = "/dev/ttyUSB0"
    baud:     int  = 115200
    no_proto: bool = False
    usb_lock_enable: bool = True

    tcp_host:             str   = ""
    tcp_port:             int   = 4403
    tcp_dead_silence_sec: float = 60.0

    dead_link_silence_sec:    float = 25.0
    link_check_interval_sec:  float = 1.0
    cooldown_secs:             float = 90.0
    force_reconnect_grace_sec: float = 2.0
    early_drop_window_sec:    float = 20.0
    early_drop_cooldown_sec:  float = 180.0
    reconnect_min_sec:        float = 2.0
    reconnect_max_sec:        float = 60.0
    send_queue_max:           int   = 500
    tx_block_during_cooldown: bool  = True

    mc_baud:                  int   = 115200
    mc_tcp_host:              str   = ""
    mc_tcp_port:              int   = 4000
    mc_tcp_dead_silence_sec:  float = 60.0
    mc_channel_map:           str   = ""
    mc_contact_to_ch:         str   = ""
    mc_chanidx_to_ch:         str   = ""
    mc_aliases:               str   = ""
    mc_rx_prefix_style:       str   = "alias"
    mc_default_ch:            int   = 0
    mc_inject_dedup_sec:      float = 12.0
    mc_silence_reconnect_sec: int   = 120
    mc_max_text_bytes:        int   = 180

    def endpoint(self) -> str:
        """Cadena legible del endpoint de conexión."""
        if self.node_type == NODE_TYPE_MESH_TCP:
            return f"tcp://{self.tcp_host}:{self.tcp_port}"
        if self.node_type == NODE_TYPE_MC_TCP:
            return f"mc-tcp://{self.mc_tcp_host}:{self.mc_tcp_port}"
        return self.port


# ======================================================================================
# Carga de descriptores desde variables de entorno
# ======================================================================================

def _load_node_descriptors_from_env() -> list[NodeDescriptor]:
    """
    Lee la configuración multi-nodo desde variables de entorno NODE_N_*.

    Itera NODE_1 .. NODE_32. Si ningún NODE_N_TYPE existe, retorna lista vacía.

    Returns:
        Lista de NodeDescriptor ordenada por índice N.
    """
    nodes: list[NodeDescriptor] = []
    for idx in range(1, 33):
        prefix = f"NODE_{idx}_"
        ntype  = os.getenv(f"{prefix}TYPE", "").strip().lower()
        if not ntype:
            continue
        if ntype not in _VALID_NODE_TYPES:
            log(f"[cfg] NODE_{idx}_TYPE='{ntype}' desconocido; ignorado")
            continue
        alias = os.getenv(f"{prefix}ALIAS", f"node_{idx}").strip()
        port  = os.getenv(f"{prefix}PORT", "/dev/ttyUSB0").strip()
        host  = os.getenv(f"{prefix}HOST", port).strip()
        baud  = safe_int(os.getenv(f"{prefix}BAUD", "115200"), 115200)
        nd = NodeDescriptor(
            node_id  = f"node_{idx}",
            alias    = alias,
            node_type= ntype,
            primary  = truthy(os.getenv(f"{prefix}PRIMARY", "0")),
            port     = port,
            baud     = baud,
            no_proto = truthy(os.getenv(f"{prefix}NO_PROTO", "0")),
            usb_lock_enable = truthy(os.getenv(f"{prefix}USB_LOCK", "1"), True),
            tcp_host = host if ntype == NODE_TYPE_MESH_TCP else "",
            tcp_port = safe_int(os.getenv(f"{prefix}TCP_PORT", "4403"), 4403),
            tcp_dead_silence_sec     = float(os.getenv(f"{prefix}TCP_DEAD_SILENCE_SEC", "60")),
            dead_link_silence_sec    = float(os.getenv(f"{prefix}DEAD_SILENCE_SEC", "25")),
            link_check_interval_sec  = float(os.getenv(f"{prefix}LINK_CHECK_SEC", "1.0")),
            cooldown_secs            = float(os.getenv(f"{prefix}COOLDOWN_SECS", os.getenv("BROKER_COOLDOWN_SECS", "90"))),
            force_reconnect_grace_sec= float(os.getenv(f"{prefix}RECONNECT_GRACE_SEC", os.getenv("BROKER_FORCE_RECONNECT_GRACE_SEC", "2"))),
            early_drop_window_sec    = float(os.getenv(f"{prefix}EARLY_DROP_WINDOW", os.getenv("BROKER_EARLY_DROP_WINDOW", "20"))),
            early_drop_cooldown_sec  = float(os.getenv(f"{prefix}EARLY_DROP_ESCALATE", os.getenv("BROKER_EARLY_DROP_ESCALATE_TO", "180"))),
            reconnect_min_sec = float(os.getenv("BROKER_RECONNECT_MIN_SEC", "2")),
            reconnect_max_sec = float(os.getenv("BROKER_RECONNECT_MAX_SEC", "60")),
            send_queue_max    = safe_int(os.getenv("BROKER_SENDQ_MAX", "500"), 500),
            tx_block_during_cooldown = truthy(os.getenv("BROKER_TX_BLOCK_DURING_COOLDOWN", "1"), True),
            mc_baud             = safe_int(os.getenv(f"{prefix}BAUD", os.getenv("MESHCORE_SERIAL_BAUD", "115200")), 115200),
            mc_tcp_host         = os.getenv(f"{prefix}MC_TCP_HOST", os.getenv("MESHCORE_TCP_HOST", "")).strip(),
            mc_tcp_port         = safe_int(os.getenv(f"{prefix}MC_TCP_PORT", os.getenv("MESHCORE_TCP_PORT", "4000")), 4000),
            mc_tcp_dead_silence_sec = float(os.getenv(f"{prefix}MC_TCP_DEAD_SILENCE_SEC", os.getenv("MESHCORE_TCP_DEAD_SILENCE_SEC", "60"))),
            mc_channel_map      = os.getenv(f"{prefix}MC_CHANNEL_MAP",  os.getenv("MESHCORE_CHANNEL_MAP", "")),
            mc_contact_to_ch    = os.getenv(f"{prefix}MC_CONTACT_TO_CH", os.getenv("MESHCORE_CONTACT_TO_CH", "")),
            mc_chanidx_to_ch    = os.getenv(f"{prefix}MC_CHANIDX_TO_CH", os.getenv("MESHCORE_CHANIDX_TO_CH", "")),
            mc_aliases          = os.getenv(f"{prefix}MC_ALIASES", os.getenv("MESHCORE_CONTACT_ALIASES", "")),
            mc_rx_prefix_style  = os.getenv(f"{prefix}MC_RX_PREFIX_STYLE", os.getenv("MESHCORE_RX_PREFIX_STYLE", "alias")),
            mc_default_ch       = safe_int(os.getenv(f"{prefix}MC_DEFAULT_CH", os.getenv("MESHCORE_CONTACT_DEFAULT_CH", "0")), 0),
            mc_inject_dedup_sec = float(os.getenv(f"{prefix}MC_INJECT_DEDUP_SEC", os.getenv("MESHCORE_INJECT_DEDUP_SEC", "12"))),
            mc_silence_reconnect_sec = safe_int(os.getenv(f"{prefix}MC_SILENCE_RECONNECT_SEC", os.getenv("MESHCORE_SILENCE_RECONNECT_SEC", "120")), 120),
            mc_max_text_bytes   = safe_int(os.getenv(f"{prefix}MC_MAX_TEXT_BYTES", os.getenv("MESHCORE_MAX_TEXT_BYTES", "180")), 180),
        )
        nodes.append(nd)
        log(f"[cfg] nodo: {nd.node_id} alias={nd.alias} type={nd.node_type} "
            f"endpoint={nd.endpoint()} primary={nd.primary}")
    return nodes


def _legacy_node_descriptor_from_env() -> list[NodeDescriptor]:
    """
    Crea un único NodeDescriptor desde variables de entorno v2/v3.

    Garantiza compatibilidad con instalaciones previas sin NODE_N_* definidos.

    Returns:
        Lista con un único NodeDescriptor marcado como primary.
    """
    mode = (os.getenv("MESH_CONN_MODE", "serial") or "serial").strip().lower()
    if mode == "tcp":
        nd = NodeDescriptor(
            node_id="node_1", alias="legacy-tcp", node_type=NODE_TYPE_MESH_TCP,
            primary=True,
            tcp_host = os.getenv("MESH_TCP_HOST", "").strip(),
            tcp_port = safe_int(os.getenv("MESH_TCP_PORT", "4403"), 4403),
            tcp_dead_silence_sec     = float(os.getenv("MESH_TCP_DEAD_SILENCE_SEC", "60")),
            dead_link_silence_sec    = float(os.getenv("BROKER_DEAD_LINK_SILENCE_SEC", "25")),
            link_check_interval_sec  = float(os.getenv("BROKER_LINK_CHECK_INTERVAL_SEC", "1.0")),
            cooldown_secs            = float(os.getenv("BROKER_COOLDOWN_SECS", "90")),
            force_reconnect_grace_sec= float(os.getenv("BROKER_FORCE_RECONNECT_GRACE_SEC", "2")),
            early_drop_window_sec    = float(os.getenv("BROKER_EARLY_DROP_WINDOW", "20")),
            early_drop_cooldown_sec  = float(os.getenv("BROKER_EARLY_DROP_ESCALATE_TO", "180")),
            reconnect_min_sec = float(os.getenv("BROKER_RECONNECT_MIN_SEC", "2")),
            reconnect_max_sec = float(os.getenv("BROKER_RECONNECT_MAX_SEC", "60")),
        )
    else:
        nd = NodeDescriptor(
            node_id="node_1", alias="legacy-serial", node_type=NODE_TYPE_MESH_SERIAL,
            primary=True,
            port     = os.getenv("MESH_USB_PORT", "/dev/ttyUSB0"),
            baud     = safe_int(os.getenv("MESH_USB_BAUD", "115200"), 115200),
            no_proto = truthy(os.getenv("MESH_USB_NO_PROTO", "0")),
            usb_lock_enable = truthy(os.getenv("BROKER_USB_LOCK_ENABLE", "1"), True),
            dead_link_silence_sec    = float(os.getenv("BROKER_DEAD_LINK_SILENCE_SEC", "25")),
            link_check_interval_sec  = float(os.getenv("BROKER_LINK_CHECK_INTERVAL_SEC", "1.0")),
            cooldown_secs            = float(os.getenv("BROKER_COOLDOWN_SECS", "90")),
            force_reconnect_grace_sec= float(os.getenv("BROKER_FORCE_RECONNECT_GRACE_SEC", "2")),
            early_drop_window_sec    = float(os.getenv("BROKER_EARLY_DROP_WINDOW", "20")),
            early_drop_cooldown_sec  = float(os.getenv("BROKER_EARLY_DROP_ESCALATE_TO", "180")),
            reconnect_min_sec = float(os.getenv("BROKER_RECONNECT_MIN_SEC", "2")),
            reconnect_max_sec = float(os.getenv("BROKER_RECONNECT_MAX_SEC", "60")),
        )
    log(f"[cfg] modo legacy: {nd.alias} type={nd.node_type} endpoint={nd.endpoint()}")
    return [nd]


# ======================================================================================
# Routing entre nodos
# ======================================================================================

@dataclass(frozen=True)
class RouteEndpoint:
    """Extremo de una regla de routing (alias + canal)."""
    alias:   str
    channel: int


@dataclass(frozen=True)
class RouteRule:
    """Regla de routing: mensaje en src -> replicar a dst."""
    src: RouteEndpoint
    dst: RouteEndpoint


class ChannelRouter:
    """
    Tabla de reglas de routing entre nodos.

    Formato de variable de entorno:
        NODE_ROUTE_N=src:alias-origen:canal,dst:alias-destino:canal

    Ejemplo:
        NODE_ROUTE_1=src:local-usb:0,dst:remoto-wifi:0
        NODE_ROUTE_2=src:meshcore-zgz:0,dst:local-usb:0
    """

    def __init__(self, rules: list[RouteRule]):
        self._rules = list(rules)
        self._index: dict[tuple[str, int], list[RouteEndpoint]] = {}
        for r in self._rules:
            self._index.setdefault((r.src.alias, r.src.channel), []).append(r.dst)

    @classmethod
    def from_env(cls) -> "ChannelRouter":
        """
        Construye el router desde variables de entorno NODE_ROUTE_N.

        Returns:
            ChannelRouter configurado con todas las reglas encontradas.
        """
        rules: list[RouteRule] = []
        for idx in range(1, 64):
            raw = os.getenv(f"NODE_ROUTE_{idx}", "").strip()
            if not raw:
                continue
            rule = cls._parse_route(raw, idx)
            if rule:
                rules.append(rule)
                log(f"[router] ruta {idx}: {rule.src.alias}:{rule.src.channel} "
                    f"-> {rule.dst.alias}:{rule.dst.channel}")
        return cls(rules)

    @staticmethod
    def _parse_route(raw: str, idx: int) -> RouteRule | None:
        """
        Parsea una regla desde su cadena de texto.

        Args:
            raw: Cadena "src:alias:canal,dst:alias:canal".
            idx: Índice (para mensajes de error).

        Returns:
            RouteRule o None si el formato es inválido.
        """
        src_ep = dst_ep = None
        for part in raw.split(","):
            m = re.match(r"^(src|dst):([^:]+):(\d+)$", part.strip(), re.IGNORECASE)
            if not m:
                continue
            ep = RouteEndpoint(alias=m.group(2).strip(), channel=int(m.group(3)))
            if m.group(1).lower() == "src":
                src_ep = ep
            else:
                dst_ep = ep
        if src_ep and dst_ep:
            return RouteRule(src=src_ep, dst=dst_ep)
        log(f"[router] NODE_ROUTE_{idx}='{raw}' formato inválido; ignorado")
        return None

    def destinations(self, alias: str, channel: int) -> list[RouteEndpoint]:
        """
        Devuelve los destinos para un origen dado.

        Args:
            alias:   Alias del nodo origen.
            channel: Canal del mensaje entrante.

        Returns:
            Lista de RouteEndpoint (puede estar vacía).
        """
        return list(self._index.get((alias, channel), []))

    @property
    def rules(self) -> list[RouteRule]:
        return list(self._rules)


# ======================================================================================
# Configuración global del broker
# ======================================================================================

@dataclass(slots=True)
class BrokerConfig:
    """Configuración global del broker (no por-nodo)."""
    bind_host:     str   = field(default_factory=lambda: os.getenv("BROKER_HOST", "127.0.0.1"))
    bind_port:     int   = field(default_factory=lambda: safe_int(os.getenv("BROKER_PORT", "8765"), 8765))
    ctrl_host:     str   = field(default_factory=lambda: os.getenv("BROKER_CTRL_HOST", "127.0.0.1"))
    ctrl_port:     int   = field(default_factory=lambda: safe_int(os.getenv("BROKER_CTRL_PORT", "8766"), 8766))
    heartbeat_sec: float = field(default_factory=lambda: float(os.getenv("BROKER_HEARTBEAT_SEC", "15")))
    no_heartbeat:  bool  = field(default_factory=lambda: truthy(os.getenv("BROKER_NO_HEARTBEAT", "0"), False))
    data_dir:      Path  = field(default_factory=lambda: Path(os.getenv("MINIBROKER_DATA_DIR", "./data")).resolve())
    positions_log:  Path = field(init=False)
    offline_log:    Path = field(init=False)
    runtime_status: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.positions_log  = self.data_dir / "positions.jsonl"
        self.offline_log    = self.data_dir / "broker_offline_log.jsonl"
        self.runtime_status = self.data_dir / "broker_status.json"


# ======================================================================================
# Persistencia y publicación JSONL
# ======================================================================================

class JsonlJournal:
    """Escritor JSONL thread-safe con soporte de backlog filtrable por node_id."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        """Serializa y escribe un registro JSONL en disco de forma atómica."""
        line = json.dumps(record, ensure_ascii=False, default=json_default)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(
        self,
        *,
        limit:    int = 200,
        since_ts: float | None = None,
        portnums: Iterable[str] | None = None,
        node_id:  str | None = None,
    ) -> list[dict]:
        """
        Lee el backlog reciente con filtros opcionales.

        Args:
            limit:    Máximo de registros devueltos.
            since_ts: Solo registros con ts >= since_ts.
            portnums: Filtro por portnum (mayúsculas).
            node_id:  Filtro por node_id exacto.

        Returns:
            Lista de dicts cronológicamente ascendente.
        """
        if not self.path.exists():
            return []
        allowed_ports = {str(p).upper() for p in (portnums or []) if str(p).strip()}
        out: list[dict] = []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            try:
                ts_f = float(obj.get("ts") or 0)
            except Exception:
                ts_f = 0.0
            if since_ts is not None and ts_f < float(since_ts):
                continue
            if allowed_ports and str(obj.get("portnum") or "").upper() not in allowed_ports:
                continue
            if node_id and obj.get("node_id") != node_id:
                continue
            out.append(obj)
            if len(out) >= max(1, int(limit)):
                break
        out.reverse()
        return out


class JsonlHub:
    """Servidor ligero de suscriptores JSONL por TCP."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self._server:  socket.socket | None = None
        self._clients: dict[socket.socket, bytearray] = {}
        self._lock  = threading.Lock()
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None
        self.max_buf = 256 * 1024

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jsonl-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._server:
                self._server.close()
        except Exception:
            pass
        with self._lock:
            for s in list(self._clients):
                try:
                    s.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        srv.settimeout(1.0)
        self._server = srv
        log(f"[hub] JSONL escuchando en {self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                client, addr = srv.accept()
                client.setblocking(False)
                with self._lock:
                    self._clients[client] = bytearray()
                log(f"[hub] cliente conectado {addr}")
            except socket.timeout:
                pass
            except OSError:
                if self._stop.is_set():
                    break
            self._flush_clients()

    def publish(self, record: dict) -> None:
        """Difunde un evento a todos los suscriptores conectados."""
        line = json.dumps(record, ensure_ascii=False, default=json_default) + "\n"
        data = line.encode("utf-8", errors="ignore")
        with self._lock:
            dead: list[socket.socket] = []
            for sock, bl in self._clients.items():
                try:
                    if bl:
                        bl.extend(data)
                    else:
                        sent = sock.send(data)
                        if sent < len(data):
                            bl.extend(data[sent:])
                except (BlockingIOError, InterruptedError):
                    bl.extend(data)
                except Exception:
                    dead.append(sock)
                    continue
                if len(bl) > self.max_buf:
                    dead.append(sock)
            for s in dead:
                self._drop(s)

    def _flush_clients(self) -> None:
        with self._lock:
            for sock, bl in list(self._clients.items()):
                if not bl:
                    continue
                try:
                    sent = sock.send(bl)
                    if sent > 0:
                        del bl[:sent]
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    self._drop(sock)

    def _drop(self, sock: socket.socket) -> None:
        try:
            sock.close()
        except Exception:
            pass
        self._clients.pop(sock, None)


# ======================================================================================
# Estado por nodo
# ======================================================================================

class NodeState:
    """
    Estado runtime de un nodo individual.

    Cada nodo tiene su propio NodeState; BrokerState los agrega.

    Args:
        nd:  Descriptor del nodo.
        cfg: Configuración global del broker.
    """

    def __init__(self, nd: NodeDescriptor, cfg: BrokerConfig):
        self.nd  = nd
        self.cfg = cfg
        self._lock = threading.Lock()
        self.connected    = False
        self.status       = "starting"
        self.started_ts   = now_ts()
        self.last_connect_ts:           float | None = None
        self.last_disconnect_ts:        float | None = None
        self.last_packet_ts:            float | None = None
        self.last_error:                str   | None = None
        self.last_connect_duration_sec: float | None = None
        self.sendq_size       = 0
        self.cooldown_until:  float = 0.0
        self.tx_blocked       = False
        self.reconnect_attempt = 0
        self.mgr_paused        = False
        self.usb_lock_acquired = False

    def snapshot(self) -> dict:
        """Devuelve snapshot del estado de este nodo."""
        with self._lock:
            now = now_ts()
            cr  = max(0, int(self.cooldown_until - now))
            st  = self.status
            if self.mgr_paused:
                st = "paused"
            elif cr > 0:
                st = "cooldown"
            elif self.connected:
                st = "running"
            elif st not in {"starting", "connecting", "reconnecting", "stopping", "unavailable"}:
                st = "disconnected"
            return {
                "node_id":   self.nd.node_id,
                "alias":     self.nd.alias,
                "type":      self.nd.node_type,
                "endpoint":  self.nd.endpoint(),
                "primary":   self.nd.primary,
                "status":    st,
                "connected": bool(self.connected),
                "started_ts":  self.started_ts,
                "last_connect_ts":    self.last_connect_ts,
                "last_disconnect_ts": self.last_disconnect_ts,
                "last_packet_ts":     self.last_packet_ts,
                "last_error":         self.last_error,
                "last_connect_duration_sec": self.last_connect_duration_sec,
                "sendq_size":          self.sendq_size,
                "cooldown_until":      self.cooldown_until,
                "cooldown_remaining":  cr,
                "tx_blocked":          bool(self.tx_blocked),
                "reconnect_attempt":   int(self.reconnect_attempt),
                "usb_lock_acquired":   bool(self.usb_lock_acquired),
            }

    def set_connected(self, connected: bool, err: str | None = None) -> None:
        with self._lock:
            now = now_ts()
            self.connected = bool(connected)
            if connected:
                self.status = "running"
                self.last_connect_ts = now
                self.last_error = None
                self.last_connect_duration_sec = None
            else:
                self.status = "disconnected"
                self.last_disconnect_ts = now
                if self.last_connect_ts:
                    self.last_connect_duration_sec = max(0.0, now - self.last_connect_ts)
                if err:
                    self.last_error = err

    def note_packet(self) -> None:
        with self._lock:
            self.last_packet_ts = now_ts()

    def set_sendq_size(self, v: int) -> None:
        with self._lock:
            self.sendq_size = int(v)

    def set_status(self, v: str) -> None:
        with self._lock:
            self.status = str(v or "running")

    def set_reconnect_attempt(self, v: int) -> None:
        with self._lock:
            self.reconnect_attempt = max(0, int(v))

    def set_cooldown(self, seconds: float) -> None:
        with self._lock:
            secs = max(0.0, float(seconds))
            self.cooldown_until = now_ts() + secs if secs > 0 else 0.0
            self.tx_blocked = secs > 0
            if secs > 0 and not self.connected:
                self.status = "cooldown"

    def clear_cooldown(self) -> None:
        with self._lock:
            self.cooldown_until = 0.0
            self.tx_blocked = False
            if self.connected:
                self.status = "running"
            elif self.status == "cooldown":
                self.status = "disconnected"

    def cooldown_remaining(self) -> int:
        with self._lock:
            return max(0, int(self.cooldown_until - now_ts()))

    def set_mgr_paused(self, v: bool) -> None:
        with self._lock:
            self.mgr_paused = bool(v)

    def set_usb_lock_acquired(self, v: bool) -> None:
        with self._lock:
            self.usb_lock_acquired = bool(v)


# ======================================================================================
# Estado global agregado
# ======================================================================================

class BrokerState:
    """
    Estado global del broker: agrega los estados de todos los nodos.

    Mantiene compatibilidad con el watchdog shell (broker_status.json)
    a través del campo last_packet_ts global.
    """

    def __init__(self, cfg: BrokerConfig):
        self.cfg = cfg
        self._node_states: dict[str, NodeState] = {}
        self._lock = threading.Lock()
        self.started_ts = now_ts()

    def register(self, node_id: str, ns: NodeState) -> None:
        with self._lock:
            self._node_states[node_id] = ns

    def get(self, node_id: str) -> NodeState | None:
        return self._node_states.get(node_id)

    def snapshot(self) -> dict:
        """
        Devuelve snapshot global del broker.

        El campo last_packet_ts es el máximo entre todos los nodos,
        para que el watchdog shell (broker_status.json) siga funcionando.

        Returns:
            Diccionario con estado global y lista detallada de nodos.
        """
        with self._lock:
            nodes = [ns.snapshot() for ns in self._node_states.values()]
        last_packet  = max((n.get("last_packet_ts") or 0 for n in nodes), default=None)
        any_connected = any(n.get("connected") for n in nodes)
        statuses      = [n.get("status", "disconnected") for n in nodes]
        if any_connected:
            gst = "running"
        elif any(s == "cooldown" for s in statuses):
            gst = "cooldown"
        elif any(s in {"connecting", "reconnecting"} for s in statuses):
            gst = "connecting"
        else:
            gst = "disconnected"
        return {
            "ok":             True,
            "status":         gst,
            "connected":      any_connected,
            "started_ts":     self.started_ts,
            "last_packet_ts": last_packet if last_packet else None,
            "node_count":     len(nodes),
            "nodes":          nodes,
        }

    def persist(self, cfg: BrokerConfig) -> None:
        """Escribe broker_status.json con el snapshot global."""
        try:
            cfg.runtime_status.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2, default=json_default),
                encoding="utf-8",
            )
        except Exception:
            pass


# ======================================================================================
# Lock exclusivo USB
# ======================================================================================

class UsbPortLock:
    """Lock exclusivo best-effort para puertos USB (flock en Linux)."""

    def __init__(self, port_name: str, data_dir: Path):
        lock_name = (port_name or "ttyUSB").replace("/", "_").replace("\\", "_").replace(":", "_")
        self.path = Path(data_dir) / f"{lock_name}.lock"
        self._fh     = None
        self._locked = False

    @property
    def locked(self) -> bool:
        return bool(self._locked)

    def acquire(self) -> bool:
        if self._locked:
            return True
        try:
            import fcntl
        except Exception:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0); fh.truncate(0)
            fh.write(f"{os.getpid()}\n"); fh.flush()
            self._fh = fh; self._locked = True
            return True
        except BlockingIOError:
            fh.close()
            return False
        except Exception:
            fh.close()
            raise

    def release(self) -> None:
        if not self._fh:
            self._locked = False
            return
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None; self._locked = False


# ======================================================================================
# Clase base abstracta del manager de nodo
# ======================================================================================

class _NodeManagerBase(abc.ABC):
    """
    Clase base abstracta para todos los managers de nodo.

    Implementa el ciclo supervisor 24/7 compartido. Subclases implementan
    _connect_once, _close_iface y _healthcheck_link_once.

    Args:
        nd:            Descriptor del nodo.
        cfg:           Configuración global del broker.
        hub:           Hub JSONL compartido.
        journal:       Journal JSONL compartido.
        on_text_event: Callback (record: dict) -> None para TEXT_MESSAGE_APP.
    """

    log_tag: str = "[node]"

    def __init__(
        self,
        nd:            NodeDescriptor,
        cfg:           BrokerConfig,
        hub:           JsonlHub,
        journal:       JsonlJournal,
        on_text_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.nd      = nd
        self.cfg     = cfg
        self.hub     = hub
        self.journal = journal
        self.on_text_event = on_text_event
        self.state   = NodeState(nd, cfg)
        self._iface  = None
        self._pub    = None
        self._lock   = threading.RLock()
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None
        self._sendq: queue.Queue[dict] = queue.Queue(maxsize=nd.send_queue_max)
        self._force_reconnect = threading.Event()
        self._heartbeat_last  = 0.0
        self._connect_started_ts:  float | None = None
        self._cooldown_force_once: float | None = None
        self._link_lost        = threading.Event()
        self._link_lost_reason: str | None = None
        self.state.set_mgr_paused(False)
        self.state.set_status("starting")

    # ------------------------------------------------------------------
    # Abstractos específicos de cada transporte
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _connect_once(self) -> None:
        """Abre la interfaz y se suscribe al bus de eventos."""
        ...

    @abc.abstractmethod
    def _close_iface(self) -> None:
        """Cierra la interfaz y libera recursos del transporte."""
        ...

    @abc.abstractmethod
    def _healthcheck_link_once(self) -> None:
        """Verifica salud del enlace; llama _mark_link_lost si detecta problema."""
        ...

    # ------------------------------------------------------------------
    # Ciclo supervisor 24/7 compartido
    # ------------------------------------------------------------------

    def _mark_link_lost(self, reason: str) -> None:
        """Marca el enlace como perdido para disparar reconexión en _run()."""
        msg = sanitize_text(reason or "link lost") or "link lost"
        self._link_lost_reason = msg
        self._link_lost.set()
        self.state.set_connected(False, msg)
        self.state.set_status("reconnecting")

    def _raise_if_link_lost(self) -> None:
        """Si _link_lost está activa lanza RuntimeError."""
        if not self._link_lost.is_set():
            return
        reason = self._link_lost_reason or "link lost"
        self._link_lost.clear()
        self._link_lost_reason = None
        raise RuntimeError(reason)

    def start(self) -> None:
        """Arranca el hilo supervisor."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"node-{self.nd.alias}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Para el manager."""
        self._stop.set()
        self._force_reconnect.set()
        self.state.set_status("stopping")
        self._close_iface()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def request_reconnect(self, grace_window_s: float | None = None) -> None:
        """
        Fuerza reconexión limpia.

        Args:
            grace_window_s: Cooldown corto para este rearme.
        """
        self._cooldown_force_once = max(0.0, float(
            grace_window_s if grace_window_s is not None else self.nd.force_reconnect_grace_sec
        ))
        self._force_reconnect.set()
        self.state.set_status("reconnecting")
        self._close_iface()

    def can_tx_now(self) -> tuple[bool, str | None]:
        """Indica si el nodo puede transmitir ahora."""
        if self.state.cooldown_remaining() > 0:
            return False, "cooldown active"
        if self._iface is None:
            return False, "interface not connected"
        return True, None

    def enqueue_send(self, item: dict) -> dict:
        """
        Inserta un mensaje en la cola de envíos del nodo.

        La cola persiste durante la vida del proceso; los mensajes
        acumulados mientras el nodo está caído se envían al reconectar.

        Args:
            item: Dict con 'text', 'ch', 'dest', 'ack'.

        Returns:
            Dict con 'ok' y opcionalmente 'error' o 'queued'.
        """
        text = sanitize_text(str(item.get("text") or ""))
        if not text:
            return {"ok": False, "error": "empty text"}
        if self.nd.tx_block_during_cooldown:
            ok, reason = self.can_tx_now()
            if not ok:
                return {"ok": False, "error": reason}
        try:
            self._sendq.put_nowait({
                "text": text,
                "ch":   safe_int(item.get("ch", 0), 0),
                "dest": item.get("dest") or None,
                "ack":  bool(item.get("ack", False)),
            })
            self.state.set_sendq_size(self._sendq.qsize())
            return {"ok": True, "queued": True}
        except queue.Full:
            return {"ok": False, "error": "send queue full"}

    def _run(self) -> None:
        """Bucle supervisor 24/7 (cooldown -> connect -> loop -> except -> backoff)."""
        backoff = max(1.0, self.nd.reconnect_min_sec)
        attempt = 0
        while not self._stop.is_set():
            try:
                self._wait_for_cooldown()
                attempt += 1
                self.state.set_reconnect_attempt(attempt)
                self.state.set_status("connecting")
                self._connect_once()
                backoff = max(1.0, self.nd.reconnect_min_sec)
                attempt = 0
                self.state.set_reconnect_attempt(0)
                while not self._stop.is_set():
                    self._drain_sendq_once()
                    self._maybe_emit_heartbeat()
                    self._healthcheck_link_once()
                    self._raise_if_link_lost()
                    if self._force_reconnect.is_set():
                        self._force_reconnect.clear()
                        raise RuntimeError("forced reconnect")
                    time.sleep(0.15)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                dur = self._compute_connect_duration()
                cd  = self._next_cooldown(dur)
                self.state.set_connected(False, err)
                self.state.set_status("cooldown" if cd > 0 else "disconnected")
                if cd > 0:
                    self.state.set_cooldown(cd)
                log(f"{self.log_tag}[{self.nd.alias}] desconectado: {err}")
                self._close_iface()
                if self._stop.is_set():
                    break
                sf = min(backoff, self.nd.reconnect_max_sec)
                log(f"{self.log_tag}[{self.nd.alias}] reintento en {sf:.1f}s")
                self._sleep_cancelable(sf)
                backoff = min(self.nd.reconnect_max_sec, max(backoff * 1.8, self.nd.reconnect_min_sec))

    def _wait_for_cooldown(self) -> None:
        rem = self.state.cooldown_remaining()
        if rem <= 0:
            self.state.clear_cooldown()
            return
        self.state.set_status("cooldown")
        while rem > 0 and not self._stop.is_set():
            time.sleep(min(1.0, float(rem)))
            rem = self.state.cooldown_remaining()
        self.state.clear_cooldown()

    def _sleep_cancelable(self, secs: float) -> None:
        end = now_ts() + max(0.1, secs)
        while now_ts() < end and not self._stop.is_set():
            time.sleep(0.1)

    def _compute_connect_duration(self) -> float | None:
        if self._connect_started_ts is None:
            return None
        return max(0.0, now_ts() - self._connect_started_ts)

    def _next_cooldown(self, duration: float | None) -> float:
        """
        Calcula el cooldown a aplicar tras una desconexión.

        Args:
            duration: Segundos que duró la conexión. None si nunca conectó.

        Returns:
            Segundos de cooldown a aplicar.
        """
        if self._cooldown_force_once is not None:
            forced = max(0.0, float(self._cooldown_force_once))
            self._cooldown_force_once = None
            return forced
        base = max(0.0, self.nd.cooldown_secs)
        if duration is None:
            return base
        if duration < max(1.0, self.nd.early_drop_window_sec):
            return max(base, self.nd.early_drop_cooldown_sec)
        return base

    def _maybe_emit_heartbeat(self) -> None:
        """Emite heartbeat periódico al hub JSONL."""
        if self.cfg.no_heartbeat or self.cfg.heartbeat_sec <= 0:
            return
        now = now_ts()
        if (now - self._heartbeat_last) < self.cfg.heartbeat_sec:
            return
        self._heartbeat_last = now
        self._emit_event({
            "type":       "heartbeat",
            "source":     "broker",
            "portnum":    "HEARTBEAT",
            "node_id":    self.nd.node_id,
            "alias":      self.nd.alias,
            "node_type":  self.nd.node_type,
            "connected":  bool(self.state.snapshot().get("connected")),
            "sendq_size": self._sendq.qsize(),
            "cooldown_remaining": self.state.cooldown_remaining(),
        })

    def _drain_sendq_once(self) -> None:
        """Extrae un mensaje de la cola y lo envía."""
        try:
            item = self._sendq.get_nowait()
        except queue.Empty:
            self.state.set_sendq_size(self._sendq.qsize())
            return
        self.state.set_sendq_size(self._sendq.qsize())
        text = sanitize_text(str(item.get("text") or ""))
        ch   = safe_int(item.get("ch", 0), 0)
        dest = item.get("dest") or None
        ack  = bool(item.get("ack", False))
        if not text:
            return
        if self.nd.tx_block_during_cooldown and self.state.cooldown_remaining() > 0:
            raise RuntimeError("tx blocked by cooldown")
        self._do_send(text=text, ch=ch, dest=dest, ack=ack)
    
    def _do_send(self, *, text: str, ch: int, dest: str | None, ack: bool) -> None:
        """
        Envía un mensaje por la interfaz activa del nodo.

        Subclases pueden sobreescribir este método si el mecanismo de envío
        difiere del patrón sendText de Meshtastic (ej. MeshCore asyncio).

        Lógica de radio/Meshtastic aplicada:
            - Si ``dest`` tiene valor, el envío se realiza como mensaje dirigido
            usando ``destinationId``.
            - Si ``dest`` es ``None``, el envío se realiza como broadcast de canal
            y NO se incluye ``destinationId``.
            - Se prueban varias firmas compatibles con distintas versiones del
            SDK Meshtastic (``channelIndex``/``channel`` y con/sin ``wantAck``).

        Args:
            text: Texto a enviar.
            ch: Índice de canal Meshtastic.
            dest: NodeId destino o ``None`` para broadcast.
            ack: Si True, solicita acuse de recibo cuando la firma lo soporte.

        Raises:
            RuntimeError: Si la interfaz no está conectada o el envío falla.
        """
        iface = self._iface
        if iface is None:
            raise RuntimeError("interface not connected")

        sent_ok = False
        last_err: Exception | None = None

        if dest is not None and str(dest).strip():
            clean_dest = str(dest).strip()
            variants = [
                {"text": text, "destinationId": clean_dest, "channelIndex": ch, "wantAck": ack},
                {"text": text, "destinationId": clean_dest, "channelIndex": ch},
                {"text": text, "destinationId": clean_dest, "channel": ch, "wantAck": ack},
                {"text": text, "destinationId": clean_dest, "channel": ch},
            ]
        else:
            variants = [
                {"text": text, "channelIndex": ch, "wantAck": ack},
                {"text": text, "channelIndex": ch},
                {"text": text, "channel": ch, "wantAck": ack},
                {"text": text, "channel": ch},
            ]

        for kwargs in variants:
            try:
                iface.sendText(**kwargs)
                sent_ok = True
                break
            except TypeError as exc:
                last_err = exc
                continue
            except Exception as exc:
                last_err = exc
                break

        if not sent_ok:
            raise RuntimeError(f"sendText failed: {last_err}")

        self._emit_event({
            "type": "tx",
            "source": "broker",
            "portnum": "TEXT_MESSAGE_APP",
            "node_id": self.nd.node_id,
            "alias": self.nd.alias,
            "channel": ch,
            "destination": dest,
            "require_ack": ack,
            "text": text,
        })

    def _on_receive(self, packet=None, interface=None, topic=None, **kwargs) -> None:
        """
        Callback del bus pubsub de Meshtastic (serial y TCP).

        Normaliza el paquete, lo publica al hub con node_id y llama
        on_text_event si el portnum es TEXT_MESSAGE_APP.

        Args:
            packet:    Dict con el paquete Meshtastic del SDK.
            interface: Instancia de la interfaz (no usada directamente).
            topic:     Topic pubsub.
            **kwargs:  Parámetros adicionales del bus pubsub.
        """
        obj = packet if isinstance(packet, dict) else kwargs.get("packet")
        if not isinstance(obj, dict):
            obj = {}
        decoded = obj.get("decoded") if isinstance(obj.get("decoded"), dict) else {}
        portnum = str(decoded.get("portnum") or obj.get("portnum") or "UNKNOWN")
        record: dict[str, Any] = {
            "ts": now_ts(), "iso": iso_now(), "type": "rx",
            "source": "meshtastic", "node_id": self.nd.node_id, "alias": self.nd.alias,
            "portnum": portnum,
            "from":    obj.get("from") or obj.get("fromId"),
            "to":      obj.get("to") or obj.get("toId"),
            "channel": obj.get("channel") if obj.get("channel") is not None else obj.get("channelIndex"),
            "hop_limit": obj.get("hopLimit"),
            "hop_start": obj.get("hopStart"),
            "raw": obj,
        }
        text = decoded.get("text") if isinstance(decoded, dict) else None
        if isinstance(text, str) and text.strip():
            record["text"] = text.strip()
        position = decoded.get("position") if isinstance(decoded, dict) else None
        if isinstance(position, dict):
            record["position"] = position
            self._append_position(record)
        telemetry = decoded.get("telemetry") if isinstance(decoded, dict) else None
        if isinstance(telemetry, dict):
            record["telemetry"] = telemetry
        self.state.note_packet()
        self._emit_event(record)
        if portnum.upper() == "TEXT_MESSAGE_APP" and callable(self.on_text_event):
            try:
                self.on_text_event(record)
            except Exception as exc:
                log(f"{self.log_tag}[{self.nd.alias}] on_text_event error: {exc}")

    def _append_position(self, record: dict) -> None:
        try:
            with self.cfg.positions_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")
        except Exception as exc:
            log(f"{self.log_tag}[{self.nd.alias}] position log error: {exc}")

    def _emit_event(self, payload: dict) -> None:
        """Publica evento al hub JSONL y lo persiste en el journal."""
        payload.setdefault("ts",      now_ts())
        payload.setdefault("iso",     iso_now())
        payload.setdefault("node_id", self.nd.node_id)
        payload.setdefault("alias",   self.nd.alias)
        self.journal.append(payload)
        self.hub.publish(payload)


# ======================================================================================
# Meshtastic Serial
# ======================================================================================

class MeshtasticSerialManager(_NodeManagerBase):
    """
    Manager de nodo Meshtastic por USB/Serial.

    Implementa SerialInterface + UsbPortLock y healthcheck de existencia
    del fichero de dispositivo (/dev/ttyV0).

    Args:
        nd:  NodeDescriptor con node_type=meshtastic_serial.
        cfg: Configuración global.
        hub: Hub JSONL.
        journal: Journal JSONL.
        on_text_event: Callback de texto.
    """

    log_tag = "[mesh-serial]"

    def __init__(self, nd, cfg, hub, journal, on_text_event=None) -> None:
        super().__init__(nd, cfg, hub, journal, on_text_event)
        self._usb_lock = UsbPortLock(nd.port, cfg.data_dir) if nd.usb_lock_enable else None
        self._last_link_check_ts = 0.0

    def _connect_once(self) -> None:
        """
        Abre SerialInterface, toma el lock USB y se suscribe al bus pubsub.

        Raises:
            RuntimeError: Si el lock USB ya está tomado por otro proceso.
            Exception:    Cualquier error del SDK Meshtastic.
        """
        log(f"{self.log_tag}[{self.nd.alias}] conectando -> {self.nd.port}")
        if self._usb_lock is not None:
            locked = self._usb_lock.acquire()
            self.state.set_usb_lock_acquired(locked)
            if not locked:
                raise RuntimeError(f"usb port busy/locked: {self.nd.port}")
        try:
            from pubsub import pub
            from meshtastic.serial_interface import SerialInterface
            iface = SerialInterface(devPath=self.nd.port, debugOut=None, noProto=self.nd.no_proto)
            self._pub = pub; self._iface = iface
            self._connect_started_ts = now_ts()
            self._link_lost.clear(); self._link_lost_reason = None
            self._last_link_check_ts = 0.0
            pub.subscribe(self._on_receive, "meshtastic.receive")
            self.state.clear_cooldown()
            self.state.set_connected(True)
            self.state.set_status("running")
            log(f"{self.log_tag}[{self.nd.alias}] conectado a {self.nd.port}")
            self._emit_event({"type": "status", "subtype": "connected", "source": "meshtastic",
                              "conn_mode": "serial", "portnum": "BROKER_STATUS", "mesh_port": self.nd.port})
        except Exception:
            self._release_usb_lock()
            raise

    def _close_iface(self) -> None:
        """Cierra SerialInterface y libera el lock USB."""
        with self._lock:
            try:
                if self._pub:
                    self._pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:
                pass
            self._pub = None
            try:
                if self._iface:
                    self._iface.close()
            except Exception:
                pass
            self._iface = None
            self._connect_started_ts = None
            self._release_usb_lock()

    def _release_usb_lock(self) -> None:
        if self._usb_lock is None:
            self.state.set_usb_lock_acquired(False)
            return
        try:
            self._usb_lock.release()
        finally:
            self.state.set_usb_lock_acquired(False)

    def _healthcheck_link_once(self) -> None:
        """
        Verifica salud del enlace serial.

        Detecta: path desaparecido, interfaz zombie, silencio excesivo + path ausente.
        """
        now = now_ts()
        if (now - self._last_link_check_ts) < max(0.2, self.nd.link_check_interval_sec):
            return
        self._last_link_check_ts = now
        mp = Path(self.nd.port)
        if not mp.exists():
            self._mark_link_lost(f"serial path disappeared: {self.nd.port}")
            return
        snap = self.state.snapshot()
        if snap.get("connected") and self._iface is None:
            self._mark_link_lost("serial interface dropped unexpectedly")
            return
        silence_limit = max(5.0, self.nd.dead_link_silence_sec)
        lpt = snap.get("last_packet_ts")
        if snap.get("connected") and self._iface is not None and lpt:
            try:
                silence = now - float(lpt)
            except Exception:
                silence = 0.0
            if silence >= silence_limit and not mp.exists():
                self._mark_link_lost(f"serial link silent and path missing: {self.nd.port}")


# ======================================================================================
# Meshtastic TCP
# ======================================================================================

class MeshtasticTcpManager(_NodeManagerBase):
    """
     Manager de nodo Meshtastic por TCP.

    Usa TCPInterface del SDK Meshtastic. No hay UsbPortLock.

    Política de estabilidad 24x7:
        - Mantener la sesión TCP mientras la interfaz siga viva.
        - Reconectar solo si la interfaz cae realmente o el SDK rompe
          la conexión.
        - No usar la ausencia de tráfico como criterio único de caída.

    Args:
        nd:  NodeDescriptor con node_type=meshtastic_tcp.
             nd.tcp_host y nd.tcp_port deben estar configurados.
        cfg: Configuración global.
        hub: Hub JSONL.
        journal: Journal JSONL.
        on_text_event: Callback de texto.
    """

    log_tag = "[mesh-tcp]"

    def __init__(self, nd, cfg, hub, journal, on_text_event=None) -> None:
        super().__init__(nd, cfg, hub, journal, on_text_event)
        if not nd.tcp_host:
            raise ValueError(f"nodo '{nd.alias}': tcp_host vacío (NODE_N_HOST o MESH_TCP_HOST)")
        self._last_link_check_ts = 0.0

    def _connect_once(self) -> None:
        """
        Abre TCPInterface y se suscribe al bus pubsub del SDK.

        Raises:
            Exception: Cualquier error de red o del SDK.
        """
        host, port = self.nd.tcp_host, self.nd.tcp_port
        log(f"{self.log_tag}[{self.nd.alias}] conectando -> {host}:{port}")
        self.state.set_usb_lock_acquired(False)
        try:
            from pubsub import pub
            from meshtastic.tcp_interface import TCPInterface
            iface = TCPInterface(hostname=host, portNumber=port, debugOut=None, noProto=self.nd.no_proto)
            self._pub = pub; self._iface = iface
            self._connect_started_ts = now_ts()
            self._link_lost.clear(); self._link_lost_reason = None
            self._last_link_check_ts = 0.0
            pub.subscribe(self._on_receive, "meshtastic.receive")
            self.state.clear_cooldown()
            self.state.set_connected(True)
            self.state.set_status("running")
            log(f"{self.log_tag}[{self.nd.alias}] conectado a {host}:{port}")
            self._emit_event({"type": "status", "subtype": "connected", "source": "meshtastic",
                              "conn_mode": "tcp", "portnum": "BROKER_STATUS",
                              "tcp_host": host, "tcp_port": port})
        except Exception:
            with self._lock:
                try:
                    if self._iface:
                        self._iface.close()
                except Exception:
                    pass
                self._iface = None; self._pub = None; self._connect_started_ts = None
            raise

    def _close_iface(self) -> None:
        """Cierra TCPInterface y se desuscribe del bus pubsub."""
        with self._lock:
            try:
                if self._pub:
                    self._pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:
                pass
            self._pub = None
            try:
                if self._iface:
                    self._iface.close()
            except Exception:
                pass
            self._iface = None
            self._connect_started_ts = None
            self.state.set_usb_lock_acquired(False)

    def _healthcheck_link_once(self) -> None:
        """Verifica salud del enlace Meshtastic TCP sin reconectar por silencio.

        Política:
            - Si la interfaz TCP desaparece estando marcada como conectada,
              se considera enlace perdido.
            - Si la interfaz sigue viva, no se fuerza reconexión por ausencia
              de tráfico. En TCP la inactividad no implica caída real.
        """
        now = now_ts()
        if (now - self._last_link_check_ts) < max(0.2, self.nd.link_check_interval_sec):
            return

        self._last_link_check_ts = now
        snap = self.state.snapshot()

        if snap.get("connected") and self._iface is None:
            self._mark_link_lost("tcp interface dropped unexpectedly")
            return  

# ======================================================================================
# MeshCore Serial (nodo de primer nivel)
# ======================================================================================

def _parse_meshcore_channel_map(raw: str) -> dict[int, dict]:
    """
    Parsea la cadena de mapeo de canales MeshCore.

    Formatos admitidos: "0:chan:0:PUBLIC", "0:contact:prefix:DM", "0:prefix:DM".

    Args:
        raw: Cadena MESHCORE_CHANNEL_MAP o NODE_N_MC_CHANNEL_MAP.

    Returns:
        Diccionario {canal_mesh: {kind, target, tag}}.
    """
    result: dict[int, dict] = {}
    if not raw:
        return result
    for item in str(raw).split(","):
        parts = [p.strip() for p in item.strip().split(":")]
        if len(parts) < 2:
            continue
        try:
            src_ch = int(parts[0])
        except Exception:
            continue
        if len(parts) >= 3 and parts[1].lower() in {"chan", "channel"}:
            try:
                result[src_ch] = {"kind": "chan", "target": int(parts[2]),
                                  "tag": parts[3] if len(parts) >= 4 else None}
            except Exception:
                continue
        elif len(parts) >= 3 and parts[1].lower() in {"contact", "dm"}:
            result[src_ch] = {"kind": "contact", "target": parts[2],
                              "tag": parts[3] if len(parts) >= 4 else None}
        else:
            result[src_ch] = {"kind": "contact", "target": parts[1],
                              "tag": parts[2] if len(parts) >= 3 else None}
    return result


def _parse_simple_kv(raw: str, key_fn: Callable = str, val_fn: Callable = str) -> dict:
    """
    Parsea un mapa "k1:v1,k2:v2" con conversores opcionales.

    Args:
        raw:    Cadena cruda.
        key_fn: Función de conversión de clave (str o int).
        val_fn: Función de conversión de valor.

    Returns:
        Diccionario parseado.
    """
    out: dict = {}
    for item in (raw or "").split(","):
        token = item.strip()
        if not token or ":" not in token:
            continue
        k, v = token.split(":", 1)
        try:
            out[key_fn(k.strip())] = val_fn(v.strip())
        except Exception:
            continue
    return out


class MeshCoreSerialManager(_NodeManagerBase):
    """
    Manager de nodo MeshCore serial. Nodo de primer nivel e independiente.

    A diferencia del MeshCoreBridge de v2/v3 (puente secundario vinculado
    a Meshtastic), aquí MeshCore opera de forma autónoma: recibe mensajes
    de la red MeshCore, los publica al hub JSONL con su propio node_id, y
    acepta mensajes del bus para retransmitirlos a MeshCore.

    El routing MeshCore <-> Meshtastic lo gestiona ChannelRouter.

    La librería meshcore se importa de forma diferida; si no está instalada
    el manager arranca pero falla en start() con un mensaje claro.

    Args:
        nd:  NodeDescriptor con node_type=meshcore_serial.
             nd.port es el puerto serie de MeshCore.
        cfg: Configuración global.
        hub: Hub JSONL.
        journal: Journal JSONL.
        on_text_event: Callback de texto (misma firma que Meshtastic).
    """

    log_tag = "[meshcore]"

    def __init__(self, nd, cfg, hub, journal, on_text_event=None) -> None:
        super().__init__(nd, cfg, hub, journal, on_text_event)
        self._async_loop:   asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None
        self._async_tx_q:   asyncio.Queue | None = None
        self._engine = None
        self._channel_map   = _parse_meshcore_channel_map(nd.mc_channel_map)
        self._contact_to_ch = _parse_simple_kv(nd.mc_contact_to_ch, str, int)
        self._chanidx_to_ch = _parse_simple_kv(nd.mc_chanidx_to_ch, int, int)
        self._aliases       = _parse_simple_kv(nd.mc_aliases)
        self._recent_injected: dict[str, float] = {}
        self._available = False
        self._import_error: str | None = None
        try:
            from meshcore import MeshCore, EventType  # type: ignore
            self._MeshCore  = MeshCore
            self._EventType = EventType
            self._available = True
        except Exception as exc:
            self._MeshCore  = None
            self._EventType = None
            self._import_error = f"{type(exc).__name__}: {exc}"

    # --- Sobreescritura de start/stop: MeshCore usa asyncio en hilo propio ---

    def start(self) -> None:
        """Arranca el hilo asyncio de MeshCore."""
        if not self._available:
            log(f"{self.log_tag}[{self.nd.alias}] no disponible: {self._import_error}")
            self.state.set_status("unavailable")
            return
        if self._async_thread and self._async_thread.is_alive():
            return
        self._stop.clear()
        self._async_thread = threading.Thread(
            target=self._async_runner, name=f"mc-async-{self.nd.alias}", daemon=True
        )
        self._async_thread.start()

    def stop(self) -> None:
        """Para el manager MeshCore."""
        self._stop.set()
        if self._async_loop and self._async_loop.is_running():
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._async_thread and self._async_thread.is_alive():
            self._async_thread.join(timeout=4.0)
        self.state.set_status("stopping")
        self._iface = None

    def enqueue_send(self, item: dict) -> dict:
        """
        Inserta un mensaje en la cola persistente de salida MeshCore.

        En MeshCore no se usa el bucle `_run()` del manager base, sino un hilo
        asyncio propio. Por ello, los mensajes deben almacenarse en `_sendq`
        y ser volcados explícitamente a `_async_tx_q` cuando la sesión está
        conectada.

        Args:
            item: Dict con 'text', 'ch', 'dest', 'ack'.

        Returns:
            Dict con 'ok' y opcionalmente 'error' o 'queued'.
        """
        text = sanitize_text(str(item.get("text") or ""))
        if not text:
            return {"ok": False, "error": "empty text"}

        if self.nd.tx_block_during_cooldown:
            ok, reason = self.can_tx_now()
            if not ok and self._iface is not None:
                return {"ok": False, "error": reason}

        try:
            self._sendq.put_nowait({
                "text": text,
                "ch":   safe_int(item.get("ch", 0), 0),
                "dest": item.get("dest") or None,
                "ack":  bool(item.get("ack", False)),
            })
            self.state.set_sendq_size(self._sendq.qsize())
            return {"ok": True, "queued": True}
        except queue.Full:
            return {"ok": False, "error": "send queue full"}


    def _flush_pending_sendq_to_async(self, limit: int = 32) -> None:
        """
        Vuelca mensajes pendientes de `_sendq` a la cola asyncio `_async_tx_q`.

        Este paso es obligatorio en MeshCore porque el manager no ejecuta
        `_NodeManagerBase._run()`, por lo que `_drain_sendq_once()` nunca llega
        a llamar a `_do_send()`.

        Lógica de radio/MeshCore aplicada:
            - Si existe mapeo explícito en ``mc_channel_map`` para ``ch``, se usa.
            - Si no existe mapeo y ``dest`` tiene valor, se usa envío a contacto.
            - Si no existe mapeo ni destino, se usa ``ch`` como ``channel_idx``
            nativo de MeshCore.
            - Se antepone ``[MT]`` a los mensajes que salen desde el lado
            Meshtastic hacia MeshCore, evitando duplicarlo si ya está presente.

        Args:
            limit: Número máximo de mensajes a mover por iteración.

        Raises:
            RuntimeError: Si el loop/cola async no están listos o el volcado falla.
        """
        if self._async_loop is None or self._async_tx_q is None:
            return

        moved = 0
        max_items = max(1, int(limit))

        while moved < max_items:
            try:
                item = self._sendq.get_nowait()
            except queue.Empty:
                self.state.set_sendq_size(self._sendq.qsize())
                return

            self.state.set_sendq_size(self._sendq.qsize())

            text = sanitize_text(str(item.get("text") or ""))
            ch = safe_int(item.get("ch", 0), 0)
            dest = item.get("dest") or None

            if not text:
                continue

            clean_text = text
            if not clean_text.startswith("[MT]"):
                clean_text = f"[MT] {clean_text}"

            mapping = self._channel_map.get(ch)
            if mapping:
                target: dict | str = mapping
            elif dest:
                target = str(dest).strip()
            else:
                # Fallback correcto: usar el canal de la ruta como channel_idx nativo
                target = {"kind": "chan", "target": int(ch)}

            try:
                self._async_loop.call_soon_threadsafe(
                    self._async_tx_q.put_nowait,
                    (target, clean_text),
                )
                moved += 1
                self._emit_event({
                    "type": "tx",
                    "source": "broker",
                    "portnum": "TEXT_MESSAGE_APP",
                    "node_id": self.nd.node_id,
                    "alias": self.nd.alias,
                    "channel": ch,
                    "destination": dest,
                    "text": clean_text,
                })
            except Exception as exc:
                try:
                    self._sendq.put_nowait(item)
                except queue.Full:
                    pass
                self.state.set_sendq_size(self._sendq.qsize())
                raise RuntimeError(f"meshcore flush enqueue error: {exc}")

    def _async_runner(self) -> None:
        """Punto de entrada del hilo asyncio."""
        loop = asyncio.new_event_loop()
        self._async_loop = loop
        try:
            loop.run_until_complete(self._amain())
        except Exception as exc:
            log(f"{self.log_tag}[{self.nd.alias}] loop error: {exc}")
        finally:
            loop.close()

    async def _amain(self) -> None:
        """Bucle supervisor asyncio con backoff."""
        self._async_tx_q = asyncio.Queue()
        backoff = 2.0
        while not self._stop.is_set():
            try:
                await self._session_once()
                backoff = 2.0
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                self.state.set_connected(False, err)
                log(f"{self.log_tag}[{self.nd.alias}] error: {err}")
                await asyncio.sleep(backoff)
                backoff = min(120.0, backoff * 1.8)

    async def _meshcore_disconnect_clean(self) -> None:
        """Cierra una sesión MeshCore de forma segura para uso 24x7.

        Esta rutina evita dejar tareas asyncio internas de meshcore pendientes
        al reconectar o detener la sesión. Es idempotente: si no hay engine
        activo, no hace nada.

        Lógica:
            1. Desvincula primero las referencias locales (_engine, _iface).
            2. Intenta desconectar el engine si existe.
            3. Cede control al loop para que las cancelaciones internas se drenen.
            4. No propaga excepciones de cierre para no romper el supervisor.

        Returns:
            None.
        """
        engine = self._engine
        self._engine = None
        self._iface = None
        self._connect_started_ts = None

        if engine is None:
            return

        try:
            await engine.disconnect()
        except Exception as exc:
            log(
                f"{self.log_tag}[{self.nd.alias}] "
                f"disconnect warning: {type(exc).__name__}: {exc}"
            )

        try:
            # Dar una oportunidad al loop para drenar tareas internas canceladas.
            await asyncio.sleep(0.20)
        except Exception:
            pass   

    async def _session_once(self) -> None:
        """
        Abre sesión MeshCore, suscribe eventos y procesa cola TX.

        Lógica de radio/MeshCore aplicada:
            - RX canal MeshCore -> broker:
                1. intenta traducir channel_idx usando mc_chanidx_to_ch
                2. si no existe mapeo, usa channel_idx nativo
                3. si es contacto directo, usa mc_contact_to_ch
                4. si no hay resolución, usa mc_default_ch
            - TX broker -> MeshCore:
                la cola persistente se vuelca con _flush_pending_sendq_to_async(),
                que resuelve mc_channel_map o usa channel_idx nativo del canal de ruta.

        Raises:
            RuntimeError: Si la librería no está disponible o la conexión falla.
        """
        port, baud = self.nd.port, self.nd.mc_baud
        log(f"{self.log_tag}[{self.nd.alias}] conectando -> {port} @ {baud}")
        engine = await self._MeshCore.create_serial(port, baud, debug=False)  # type: ignore
        self._engine = engine
        self._iface = engine
        self._connect_started_ts = now_ts()
        self.state.set_connected(True)
        self.state.set_status("running")
        log(f"{self.log_tag}[{self.nd.alias}] conectado a {port}")
        self._emit_event({
            "type": "status",
            "subtype": "connected",
            "source": "meshcore",
            "conn_mode": "serial",
            "portnum": "BROKER_STATUS",
            "mc_port": port,
        })
        last_rx_ts = now_ts()

        try:
            await engine.start_auto_message_fetching()
        except Exception:
            pass

        async def _on_mc_msg(event) -> None:
            nonlocal last_rx_ts
            try:
                last_rx_ts = now_ts()
                payload = dict(getattr(event, "payload", None) or {})
                ev_type = getattr(event, "type", None)
                kind = "chan" if ev_type == self._EventType.CHANNEL_MSG_RECV else "contact"
                text = sanitize_text(str(payload.get("text") or ""))
                if not text:
                    return

                prefix = str(payload.get("pubkey_prefix") or "").strip()
                alias = str(
                    payload.get("alias")
                    or payload.get("name")
                    or self._aliases.get(prefix)
                    or ""
                ).strip()

                chan_idx = payload.get("channel_idx")
                out_ch = None

                if kind == "chan" and chan_idx is not None:
                    try:
                        chan_idx_i = int(chan_idx)
                        # 1) traducción explícita si existe
                        out_ch = self._chanidx_to_ch.get(chan_idx_i)
                        # 2) fallback a channel_idx nativo
                        if out_ch is None:
                            out_ch = chan_idx_i
                    except Exception:
                        out_ch = None

                if out_ch is None and prefix:
                    out_ch = self._contact_to_ch.get(prefix)

                if out_ch is None:
                    out_ch = self.nd.mc_default_ch

                head = "[MC]"
                if self.nd.mc_rx_prefix_style == "alias" and alias:
                    head = f"[MC:{alias}]"
                elif prefix:
                    head = f"[MC:{prefix}]"

                out_text = f"{head} {text}".strip()
                fp = f"{out_ch}|{out_text}"

                if self._recent_injected.get(fp, 0) < now_ts():
                    self._recent_injected[fp] = now_ts() + self.nd.mc_inject_dedup_sec
                    record = {
                        "ts": now_ts(),
                        "iso": iso_now(),
                        "type": "rx",
                        "source": "meshcore",
                        "portnum": "TEXT_MESSAGE_APP",
                        "node_id": self.nd.node_id,
                        "alias": self.nd.alias,
                        "channel": int(out_ch),
                        "text": out_text,
                        "pubkey_prefix": prefix,
                        "mc_alias": alias or None,
                        "mc_kind": kind,
                        "channel_idx": chan_idx,
                    }
                    self.state.note_packet()
                    self._emit_event(record)
                    if callable(self.on_text_event):
                        try:
                            self.on_text_event(record)
                        except Exception as exc:
                            log(f"{self.log_tag}[{self.nd.alias}] on_text_event error: {exc}")
            except Exception as exc:
                log(f"{self.log_tag}[{self.nd.alias}] rx error: {exc}")

        try:
            engine.subscribe(self._EventType.CONTACT_MSG_RECV, _on_mc_msg)
            engine.subscribe(self._EventType.CHANNEL_MSG_RECV, _on_mc_msg)
        except Exception as exc:
            raise RuntimeError(f"meshcore subscribe failed: {exc}")

        while not self._stop.is_set():
            silence = now_ts() - last_rx_ts
            if self.nd.mc_silence_reconnect_sec > 0 and silence > float(self.nd.mc_silence_reconnect_sec):
                raise RuntimeError(f"meshcore silent > {self.nd.mc_silence_reconnect_sec}s")

            self._flush_pending_sendq_to_async()

            try:
                target, text = await asyncio.wait_for(self._async_tx_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            for part in self._split_utf8(text, self.nd.mc_max_text_bytes):
                if isinstance(target, dict) and str(target.get("kind")) == "chan":
                    await engine.commands.send_chan_msg(int(target["target"]), part)
                else:
                    send_target = target
                    if isinstance(send_target, str):
                        try:
                            c = engine.get_contact_by_key_prefix(send_target)
                            if c:
                                send_target = c
                        except Exception:
                            pass
                    await engine.commands.send_msg(send_target, part)

                last_rx_ts = now_ts()
                await asyncio.sleep(0.15)

        await self._meshcore_disconnect_clean()
        self.state.set_connected(False, "stopped")


    def _do_send(self, *, text: str, ch: int, dest: str | None, ack: bool) -> None:
            """
            Encola un mensaje para envío por MeshCore vía asyncio.

            Sobreescribe _NodeManagerBase._do_send porque MeshCore usa asyncio
            en lugar de la API síncrona sendText de Meshtastic.

            Lógica de radio/MeshCore aplicada:
                - Si el tráfico sale desde el lado Meshtastic hacia MeshCore,
                se antepone el prefijo "[MT] " para identificar el origen.
                - Si el texto ya viene prefijado con "[MT]", no se duplica.
                - Si existe mapeo explícito en ``_channel_map`` para ``ch``, se usa.
                - Si no hay mapeo y ``dest`` está informado, se usa envío dirigido
                a contacto MeshCore.
                - Si no hay mapeo ni destino, se usa directamente ``ch`` como
                ``channel_idx`` nativo de MeshCore.

            Args:
                text: Texto a enviar.
                ch: Canal lógico/broker o channel_idx nativo según configuración.
                dest: Destino MeshCore (prefijo de contacto) o None para canal.
                ack: No usado en MeshCore SDK; se mantiene por compatibilidad de firma.

            Raises:
                RuntimeError: Si el loop asyncio no está disponible o falla la cola.
            """
            if not self._async_loop or not self._async_tx_q:
                raise RuntimeError("meshcore loop not ready")

            clean_text = sanitize_text(str(text or ""))
            if not clean_text:
                raise RuntimeError("empty text")

            # Prefijo de origen Meshtastic -> MeshCore
            if not clean_text.startswith("[MT]"):
                clean_text = f"[MT] {clean_text}"

            mapping = self._channel_map.get(ch)
            if mapping:
                target: dict | str = mapping
            elif dest:
                target = str(dest).strip()
            else:
                # Modo nativo MeshCore: si no hay mapeo, usar directamente el canal de ruta
                target = {"kind": "chan", "target": int(ch)}

            try:
                self._async_loop.call_soon_threadsafe(
                    self._async_tx_q.put_nowait,
                    (target, clean_text),
                )
            except Exception as exc:
                raise RuntimeError(f"meshcore enqueue error: {exc}")

            self._emit_event({
                "type": "tx",
                "source": "broker",
                "portnum": "TEXT_MESSAGE_APP",
                "node_id": self.nd.node_id,
                "alias": self.nd.alias,
                "channel": ch,
                "destination": dest,
                "text": clean_text,
            })
    

    # Los métodos abstractos no se usan en MeshCore (ciclo asyncio propio)
    def _connect_once(self) -> None: pass
    def _close_iface(self) -> None: self._iface = None
    def _healthcheck_link_once(self) -> None: pass

    @staticmethod
    def _split_utf8(text: str, max_bytes: int) -> list[str]:
        """
        Divide texto en fragmentos respetando el límite en bytes UTF-8.

        Args:
            text:      Texto a fragmentar.
            max_bytes: Límite máximo de bytes por fragmento.

        Returns:
            Lista de cadenas dentro del límite.
        """
        clean = sanitize_text(text)
        if not clean:
            return []
        limit = max(80, int(max_bytes))
        if len(clean.encode("utf-8", errors="ignore")) <= limit:
            return [clean]
        out: list[str] = []
        rest = clean
        while rest:
            if len(rest.encode("utf-8", errors="ignore")) <= limit:
                out.append(rest); break
            lo, hi, best = 1, len(rest), 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if len(rest[:mid].encode("utf-8", errors="ignore")) <= limit:
                    best = mid; lo = mid + 1
                else:
                    hi = mid - 1
            sp = rest.rfind(" ", 0, best + 1)
            if sp >= max(10, int(best * 0.6)):
                best = sp
            head = rest[:best].strip() or rest[:1]
            out.append(head)
            rest = rest[len(head):].strip()
        return out



# ======================================================================================
# MeshCore TCP (nodo de primer nivel vía TCP)
# ======================================================================================

class MeshCoreTcpManager(MeshCoreSerialManager):
    """
    Manager de nodo MeshCore por TCP.

    Hereda de MeshCoreSerialManager y reutiliza el ciclo asyncio, la lógica
    de routing de canales, _do_send, _split_utf8 y el manejo de eventos RX.
    La diferencia principal es el método de apertura de sesión:
    usa ``MeshCore.create_tcp(host, port)`` en lugar de ``create_serial``.

    Política de estabilidad 24x7:
        - Mantener la sesión TCP mientras el engine siga operativo.
        - Reconectar solo ante error real de conexión, suscripción,
          envío o caída efectiva de la sesión.
        - No usar la ausencia de tráfico RX como criterio único de reconexión.

    Según la API oficial de meshcore_py:
        engine = await MeshCore.create_tcp("192.168.1.100", 4000)

    El puerto TCP por defecto de MeshCore es 4000 (distinto al 4403 de Meshtastic).

    Args:
        nd:  NodeDescriptor con node_type=meshcore_tcp.
             nd.mc_tcp_host y nd.mc_tcp_port deben estar configurados.
        cfg: Configuración global del broker.
        hub: Hub JSONL compartido.
        journal: Journal JSONL compartido.
        on_text_event: Callback de texto (misma firma que los otros managers).
    """

    log_tag = "[meshcore-tcp]"

    def __init__(self, nd, cfg, hub, journal, on_text_event=None) -> None:
        super().__init__(nd, cfg, hub, journal, on_text_event)
        if not nd.mc_tcp_host:
            raise ValueError(
                f"nodo '{nd.alias}': mc_tcp_host vacío "
                f"(NODE_N_MC_TCP_HOST o MESHCORE_TCP_HOST)"
            )

    async def _session_once(self) -> None:
        """Abre sesión MeshCore por TCP, suscribe eventos y procesa cola TX.

        Política de estabilidad 24x7:
            - Mantener la sesión TCP mientras siga operativa.
            - Reconectar solo ante error real de create_tcp(), subscribe(),
              send_chan_msg(), send_msg() o cierre/caída efectiva del engine.
            - No usar el silencio RX como criterio único de desconexión.

        Raises:
            ValueError:   Si mc_tcp_host está vacío.
            RuntimeError: Si la librería no está disponible o la conexión falla.
        """
        if not self._available:
            raise RuntimeError(f"meshcore no disponible: {self._import_error}")

        host = self.nd.mc_tcp_host
        port = self.nd.mc_tcp_port

        log(f"{self.log_tag}[{self.nd.alias}] conectando -> {host}:{port}")
        engine = await self._MeshCore.create_tcp(host, port)  # type: ignore

        self._engine = engine
        self._iface = engine
        self._connect_started_ts = now_ts()
        self.state.set_connected(True)
        self.state.set_status("running")

        log(f"{self.log_tag}[{self.nd.alias}] conectado a {host}:{port}")
        self._emit_event({
            "type": "status",
            "subtype": "connected",
            "source": "meshcore",
            "conn_mode": "tcp",
            "portnum": "BROKER_STATUS",
            "mc_host": host,
            "mc_port": port,
        })

        try:
            await engine.start_auto_message_fetching()
        except Exception:
            pass

        async def _on_mc_msg(event) -> None:
            try:
                payload = dict(getattr(event, "payload", None) or {})
                ev_type = getattr(event, "type", None)
                kind = "chan" if ev_type == self._EventType.CHANNEL_MSG_RECV else "contact"
                text = sanitize_text(str(payload.get("text") or ""))
                if not text:
                    return

                prefix = str(payload.get("pubkey_prefix") or "").strip()
                alias = str(
                    payload.get("alias")
                    or payload.get("name")
                    or self._aliases.get(prefix)
                    or ""
                ).strip()

                chan_idx = payload.get("channel_idx")
                out_ch = None

                if kind == "chan" and chan_idx is not None:
                    try:
                        out_ch = self._chanidx_to_ch.get(int(chan_idx))
                    except Exception:
                        pass

                if out_ch is None and prefix:
                    out_ch = self._contact_to_ch.get(prefix)

                if out_ch is None:
                    out_ch = self.nd.mc_default_ch

                head = "[MC]"
                if self.nd.mc_rx_prefix_style == "alias" and alias:
                    head = f"[MC:{alias}]"
                elif prefix:
                    head = f"[MC:{prefix}]"

                out_text = f"{head} {text}".strip()
                fp = f"{out_ch}|{out_text}"

                if self._recent_injected.get(fp, 0) < now_ts():
                    self._recent_injected[fp] = now_ts() + self.nd.mc_inject_dedup_sec
                    record = {
                        "ts": now_ts(),
                        "iso": iso_now(),
                        "type": "rx",
                        "source": "meshcore",
                        "portnum": "TEXT_MESSAGE_APP",
                        "node_id": self.nd.node_id,
                        "alias": self.nd.alias,
                        "channel": int(out_ch),
                        "text": out_text,
                        "pubkey_prefix": prefix,
                        "mc_alias": alias or None,
                        "mc_kind": kind,
                        "channel_idx": chan_idx,
                    }
                    self.state.note_packet()
                    self._emit_event(record)
                    if callable(self.on_text_event):
                        try:
                            self.on_text_event(record)
                        except Exception as exc:
                            log(f"{self.log_tag}[{self.nd.alias}] on_text_event error: {exc}")

            except Exception as exc:
                log(f"{self.log_tag}[{self.nd.alias}] rx error: {exc}")

        try:
            engine.subscribe(self._EventType.CONTACT_MSG_RECV, _on_mc_msg)
            engine.subscribe(self._EventType.CHANNEL_MSG_RECV, _on_mc_msg)
        except Exception as exc:
            await self._meshcore_disconnect_clean()
            raise RuntimeError(f"meshcore tcp subscribe failed: {exc}")

        try:
            while not self._stop.is_set():
                if self._engine is None or self._iface is None:
                    raise RuntimeError(f"meshcore tcp session dropped ({host}:{port})")
                 
                self._flush_pending_sendq_to_async()
                
                try:
                    target, text = await asyncio.wait_for(self._async_tx_q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                for part in self._split_utf8(text, self.nd.mc_max_text_bytes):
                    try:
                        if isinstance(target, dict) and str(target.get("kind")) == "chan":
                            await engine.commands.send_chan_msg(int(target["target"]), part)
                        else:
                            send_target = target
                            if isinstance(send_target, str):
                                try:
                                    c = engine.get_contact_by_key_prefix(send_target)
                                    if c:
                                        send_target = c
                                except Exception:
                                    pass
                            await engine.commands.send_msg(send_target, part)
                    except Exception as exc:
                        raise RuntimeError(
                            f"meshcore tcp tx failed ({host}:{port}): "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                    await asyncio.sleep(0.15)
        finally:
            await self._meshcore_disconnect_clean()
            self.state.set_connected(False, "stopped")

# ======================================================================================
# Fábrica de managers
# ======================================================================================

def _build_node_manager(
    nd:            NodeDescriptor,
    cfg:           BrokerConfig,
    hub:           JsonlHub,
    journal:       JsonlJournal,
    on_text_event: Callable[[dict], None] | None,
) -> _NodeManagerBase:
    """
    Instancia el manager correcto para un NodeDescriptor.

    Args:
        nd:            Descriptor del nodo.
        cfg:           Configuración global.
        hub:           Hub JSONL.
        journal:       Journal JSONL.
        on_text_event: Callback de texto.

    Returns:
        Instancia de _NodeManagerBase.

    Raises:
        ValueError: Si node_type no es reconocido.
    """
    if nd.node_type == NODE_TYPE_MESH_SERIAL:
        return MeshtasticSerialManager(nd, cfg, hub, journal, on_text_event)
    if nd.node_type == NODE_TYPE_MESH_TCP:
        return MeshtasticTcpManager(nd, cfg, hub, journal, on_text_event)
    if nd.node_type == NODE_TYPE_MC_SERIAL:
        return MeshCoreSerialManager(nd, cfg, hub, journal, on_text_event)
    if nd.node_type == NODE_TYPE_MC_TCP:
        return MeshCoreTcpManager(nd, cfg, hub, journal, on_text_event)
    raise ValueError(f"node_type desconocido: '{nd.node_type}'")


# ======================================================================================
# NodeManager — orquestador multi-nodo
# ======================================================================================

class NodeManager:
    """
    Orquestador de todos los managers de nodo.

    Responsabilidades:
    - Instanciar y arrancar N managers.
    - Aplicar ChannelRouter en cada evento de texto recibido.
    - Exponer enqueue_send, broadcast_send y request_reconnect al ControlServer.

    Args:
        descriptors:  Lista de NodeDescriptor.
        cfg:          Configuración global.
        broker_state: Estado global agregado.
        hub:          Hub JSONL.
        journal:      Journal JSONL.
        router:       Tabla de routing.
    """

    def __init__(
        self,
        descriptors:  list[NodeDescriptor],
        cfg:          BrokerConfig,
        broker_state: BrokerState,
        hub:          JsonlHub,
        journal:      JsonlJournal,
        router:       ChannelRouter,
    ) -> None:
        self.cfg          = cfg
        self.broker_state = broker_state
        self.router       = router
        self._managers:   dict[str, _NodeManagerBase] = {}
        self._by_alias:   dict[str, _NodeManagerBase] = {}
        self._primary_id: str | None = None

        for nd in descriptors:
            mgr = _build_node_manager(nd, cfg, hub, journal, self._on_text_event)
            self._managers[nd.node_id] = mgr
            self._by_alias[nd.alias]   = mgr
            broker_state.register(nd.node_id, mgr.state)
            if nd.primary and self._primary_id is None:
                self._primary_id = nd.node_id

        if not self._primary_id and self._managers:
            first = next(iter(self._managers))
            self._primary_id = first
            log(f"[node-mgr] ningún nodo marcado como primary; usando '{first}'")

    def start(self) -> None:
        """Arranca todos los managers."""
        for mgr in self._managers.values():
            mgr.start()

    def stop(self) -> None:
        """Para todos los managers."""
        for mgr in self._managers.values():
            mgr.stop()

    def node_ids(self) -> list[str]:
        return list(self._managers.keys())

    def get_manager(self, node_id: str) -> _NodeManagerBase | None:
        return self._managers.get(node_id)

    def get_manager_by_alias(self, alias: str) -> _NodeManagerBase | None:
        return self._by_alias.get(alias)

    def enqueue_send(self, node_id: str | None, item: dict) -> dict:
        """
        Encola un mensaje en el nodo especificado (o el primario si None).

        Args:
            node_id: node_id o alias del nodo destino. None = primario.
            item:    Dict con 'text', 'ch', 'dest', 'ack'.

        Returns:
            Dict con 'ok' y opcionalmente 'error'.
        """
        target_id = node_id or self._primary_id
        if not target_id:
            return {"ok": False, "error": "no nodes configured"}
        mgr = self._managers.get(target_id) or self._by_alias.get(target_id)
        if mgr is None:
            return {"ok": False, "error": f"node not found: {target_id}"}
        return mgr.enqueue_send(item)

    def broadcast_send(self, item: dict) -> dict:
        """
        Encola un mensaje en todos los nodos activos.

        Args:
            item: Dict con 'text', 'ch', 'dest', 'ack'.

        Returns:
            Dict con 'ok' (True si al menos un nodo aceptó) y resultados por nodo.
        """
        results = {nid: mgr.enqueue_send(item) for nid, mgr in self._managers.items()}
        return {"ok": any(r.get("ok") for r in results.values()), "results": results}

    def request_reconnect(self, node_id: str | None, grace_window_s: float | None) -> dict:
        """
        Fuerza reconexión del nodo indicado, o de todos si node_id es None.

        Args:
            node_id:       node_id o alias del nodo. None = todos.
            grace_window_s: Cooldown corto para este rearme.

        Returns:
            Dict con 'ok' y lista de nodos afectados.
        """
        if node_id:
            mgr = self._managers.get(node_id) or self._by_alias.get(node_id)
            if not mgr:
                return {"ok": False, "error": f"node not found: {node_id}"}
            mgr.request_reconnect(grace_window_s)
            return {"ok": True, "affected": [node_id]}
        for mgr in self._managers.values():
            mgr.request_reconnect(grace_window_s)
        return {"ok": True, "affected": list(self._managers.keys())}

    def _on_text_event(self, record: dict) -> None:
        """
        Callback interno: aplica ChannelRouter cuando llega un mensaje de texto.

        Itera las reglas de routing y encola el mensaje en los nodos destino.
        Evita bucles de eco entre el mismo nodo origen y destino.

        Args:
            record: Evento TEXT_MESSAGE_APP con node_id, alias y channel.
        """
        src_alias = str(record.get("alias") or "")
        channel   = safe_int(record.get("channel", 0), 0)
        text      = sanitize_text(str(record.get("text") or ""))
        if not text or not src_alias:
            return
        for dst in self.router.destinations(src_alias, channel):
            if dst.alias == src_alias:
                continue  # evitar eco en el mismo nodo
            dst_mgr = self._by_alias.get(dst.alias)
            if dst_mgr is None:
                log(f"[router] destino desconocido: alias='{dst.alias}'")
                continue
            result = dst_mgr.enqueue_send({"text": text, "ch": dst.channel, "dest": None, "ack": False})
            log(f"[router] {src_alias}:{channel} -> {dst.alias}:{dst.channel} "
                f"{'OK' if result.get('ok') else 'KO'} '{text[:60]}'")


# ======================================================================================
# Servidor de control
# ======================================================================================

class ControlServer:
    """
    Puerto de control del minibroker v4.0.

    Comandos soportados:
    - PING
    - BROKER_STATUS    estado global agregado
    - NODE_LIST        lista de nodos con estado individual
    - NODE_STATUS      estado de un nodo concreto
    - FETCH_BACKLOG    backlog con filtro opcional por node_id
    - SEND_TEXT        envío a nodo concreto (node_id/alias opcional; default = primario)
    - BROADCAST_TEXT   envío a todos los nodos activos
    - FORCE_RECONNECT  reconexión de un nodo o de todos
    - ROUTE_LIST       lista de reglas de routing activas
    """

    def __init__(
        self,
        host:         str,
        port:         int,
        broker_state: BrokerState,
        journal:      JsonlJournal,
        node_mgr:     NodeManager,
        router:       ChannelRouter,
    ):
        self.host         = host
        self.port         = int(port)
        self.broker_state = broker_state
        self.journal      = journal
        self.node_mgr     = node_mgr
        self.router       = router
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None
        self._srv:    socket.socket   | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ctrl-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        srv.settimeout(1.0)
        self._srv = srv
        log(f"[ctrl] escuchando en {self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break

    def _handle(self, conn: socket.socket, addr) -> None:
        with conn:
            conn.settimeout(6.0)
            buf = b""
            try:
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65535)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
            raw = buf.decode("utf-8", errors="ignore").strip()
            if not raw:
                reply = {"ok": False, "error": "empty request"}
            else:
                try:
                    reply = self._dispatch(json.loads(raw))
                except Exception as exc:
                    reply = {"ok": False, "error": f"bad json: {exc}"}
            conn.sendall(
                (json.dumps(reply, ensure_ascii=False, default=json_default) + "\n").encode()
            )
            log(f"[ctrl] {addr} cmd={raw[:40]} ok={reply.get('ok')}")

    def _dispatch(self, obj: dict) -> dict:
        """
        Despacha un comando al componente adecuado.

        Args:
            obj: Dict con 'cmd' y opcionalmente 'params'.

        Returns:
            Dict de respuesta con al menos 'ok'.
        """
        cmd    = str(obj.get("cmd") or "").strip().upper()
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}

        if cmd == "PING":
            return {"ok": True, "cmd": cmd, "ts": now_ts()}

        if cmd == "BROKER_STATUS":
            snap = self.broker_state.snapshot()
            snap["ok"] = True
            return snap

        if cmd == "NODE_LIST":
            nodes = [self.node_mgr.get_manager(nid).state.snapshot()
                     for nid in self.node_mgr.node_ids()]
            return {"ok": True, "cmd": cmd, "nodes": nodes}

        if cmd == "NODE_STATUS":
            nid = str(params.get("node_id") or params.get("alias") or "")
            mgr = self.node_mgr.get_manager(nid) or self.node_mgr.get_manager_by_alias(nid)
            if mgr is None:
                return {"ok": False, "cmd": cmd, "error": f"node not found: {nid}"}
            snap = mgr.state.snapshot()
            snap["ok"] = True
            return snap

        if cmd == "FETCH_BACKLOG":
            limit   = safe_int(params.get("limit", 200), 200)
            since_ts = params.get("since_ts")
            portnums = params.get("portnums") if isinstance(params.get("portnums"), list) else None
            node_id  = params.get("node_id") or None
            try:
                since = float(since_ts) if since_ts is not None else None
            except Exception:
                since = None
            data = self.journal.tail(limit=limit, since_ts=since, portnums=portnums, node_id=node_id)
            return {"ok": True, "cmd": cmd, "data": data}

        if cmd == "SEND_TEXT":
            text    = sanitize_text(str(params.get("text") or ""))
            ch      = safe_int(params.get("ch", 0), 0)
            dest    = params.get("dest") or None
            ack     = safe_bool(params.get("ack", False), False)
            node_id = params.get("node_id") or params.get("alias") or None
            result  = self.node_mgr.enqueue_send(node_id, {"text": text, "ch": ch, "dest": dest, "ack": ack})
            return {"cmd": cmd, **result}

        if cmd == "BROADCAST_TEXT":
            text   = sanitize_text(str(params.get("text") or ""))
            ch     = safe_int(params.get("ch", 0), 0)
            dest   = params.get("dest") or None
            ack    = safe_bool(params.get("ack", False), False)
            result = self.node_mgr.broadcast_send({"text": text, "ch": ch, "dest": dest, "ack": ack})
            return {"cmd": cmd, **result}

        if cmd == "FORCE_RECONNECT":
            node_id = params.get("node_id") or params.get("alias") or None
            grace   = params.get("grace_window_s")
            try:
                grace = float(grace) if grace is not None else None
            except Exception:
                grace = None
            result = self.node_mgr.request_reconnect(node_id, grace)
            return {"cmd": cmd, **result}

        if cmd == "ROUTE_LIST":
            return {"ok": True, "cmd": cmd, "routes": [
                {"src_alias": r.src.alias, "src_channel": r.src.channel,
                 "dst_alias": r.dst.alias, "dst_channel": r.dst.channel}
                for r in self.router.rules
            ]}

        return {"ok": False, "error": f"unknown cmd: {cmd}"}


# ======================================================================================
# Aplicación principal
# ======================================================================================

class EmergencyMiniBroker:
    """
    Orquestador principal del minibroker v4.0.

    Detecta automáticamente si la configuración es multi-nodo (NODE_1_TYPE)
    o legacy (MESH_CONN_MODE / MESH_USB_PORT / MESH_TCP_HOST de v2/v3).
    """

    def __init__(self, cfg: BrokerConfig, descriptors: list[NodeDescriptor] | None = None):
        self.cfg          = cfg
        self.journal      = JsonlJournal(cfg.offline_log)
        self.hub          = JsonlHub(cfg.bind_host, cfg.bind_port)
        self.broker_state = BrokerState(cfg)
        self.router       = ChannelRouter.from_env()

        if descriptors is None:
            descriptors = (
                _load_node_descriptors_from_env()
                if os.getenv("NODE_1_TYPE")
                else _legacy_node_descriptor_from_env()
            )
        if not descriptors:
            raise ValueError("No hay nodos configurados. "
                             "Define NODE_1_TYPE o MESH_CONN_MODE en el entorno.")

        self.node_mgr = NodeManager(
            descriptors, cfg, self.broker_state, self.hub, self.journal, self.router
        )
        self.ctrl = ControlServer(
            cfg.ctrl_host, cfg.ctrl_port,
            self.broker_state, self.journal, self.node_mgr, self.router,
        )
        self._stopping = threading.Event()

    def start(self) -> None:
        """Arranca todos los componentes del broker."""
        self.hub.start()
        self.ctrl.start()
        self.node_mgr.start()

    def stop(self) -> None:
        """Para todos los componentes del broker."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.node_mgr.stop()
        self.ctrl.stop()
        self.hub.stop()
        self.broker_state.persist(self.cfg)


# ======================================================================================
# CLI
# ======================================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    """
    Construye el parser de argumentos CLI.

    La configuración multi-nodo se hace preferentemente por variables de
    entorno NODE_N_*. Los argumentos CLI cubren los parámetros globales del
    broker y la compatibilidad con instalaciones v2/v3.

    Returns:
        ArgumentParser configurado.
    """
    parser = argparse.ArgumentParser(
        description="MiniBroker v4.0 multi-nodo: Meshtastic Serial/TCP + MeshCore + APRS"
    )
    parser.add_argument("--bind",          default=os.getenv("BROKER_HOST", "127.0.0.1"))
    parser.add_argument("--port",  type=int, default=safe_int(os.getenv("BROKER_PORT", "8765"), 8765))
    parser.add_argument("--ctrl-host",     default=os.getenv("BROKER_CTRL_HOST", "127.0.0.1"))
    parser.add_argument("--ctrl-port", type=int, default=safe_int(os.getenv("BROKER_CTRL_PORT", "8766"), 8766))
    parser.add_argument("--data-dir",      default=os.getenv("MINIBROKER_DATA_DIR", "./data"))
    parser.add_argument("--heartbeat-sec", type=float, default=float(os.getenv("BROKER_HEARTBEAT_SEC", "15")))
    parser.add_argument("--no-heartbeat",  action="store_true",
                        default=truthy(os.getenv("BROKER_NO_HEARTBEAT", "0"), False))
    # Compat v2/v3
    compat = parser.add_argument_group("Compatibilidad v2/v3 (ignorado si NODE_1_TYPE está definido)")
    compat.add_argument("--conn-mode",  default=os.getenv("MESH_CONN_MODE", "serial"),
                        choices=["serial", "tcp"])
    compat.add_argument("--mesh-port",  default=os.getenv("MESH_USB_PORT", "/dev/ttyUSB0"))
    compat.add_argument("--mesh-baud",  type=int, default=safe_int(os.getenv("MESH_USB_BAUD", "115200"), 115200))
    compat.add_argument("--tcp-host",   default=os.getenv("MESH_TCP_HOST", ""))
    compat.add_argument("--tcp-port",   type=int, default=safe_int(os.getenv("MESH_TCP_PORT", "4403"), 4403))
    return parser


def make_config_from_args(args: argparse.Namespace) -> BrokerConfig:
    """
    Construye BrokerConfig desde los argumentos CLI.

    Propaga los argumentos legacy al entorno para que el loader los recoja.

    Args:
        args: Namespace de ArgumentParser.

    Returns:
        BrokerConfig inicializado.
    """
    if args.conn_mode and not os.getenv("MESH_CONN_MODE"):
        os.environ["MESH_CONN_MODE"] = args.conn_mode
    if args.mesh_port and not os.getenv("MESH_USB_PORT"):
        os.environ["MESH_USB_PORT"] = args.mesh_port
    if args.tcp_host and not os.getenv("MESH_TCP_HOST"):
        os.environ["MESH_TCP_HOST"] = args.tcp_host
    return BrokerConfig(
        bind_host=args.bind,
        bind_port=int(args.port),
        ctrl_host=args.ctrl_host,
        ctrl_port=int(args.ctrl_port),
        data_dir=Path(args.data_dir).resolve(),
        heartbeat_sec=float(args.heartbeat_sec),
        no_heartbeat=bool(args.no_heartbeat),
    )


def main() -> int:
    parser = build_arg_parser()
    args   = parser.parse_args()
    cfg    = make_config_from_args(args)
    app    = EmergencyMiniBroker(cfg)

    stop_event = threading.Event()

    def _handle_sig(signum, frame):
        log(f"[broker] señal {signum}, apagando…")
        stop_event.set()
        app.stop()

    signal.signal(signal.SIGINT,  _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    app.start()
    n = len(app.node_mgr.node_ids())
    log(f"[broker] v4.0 arrancado — {n} nodo(s) "
        f"hub={cfg.bind_host}:{cfg.bind_port} ctrl={cfg.ctrl_host}:{cfg.ctrl_port}")
    try:
        while not stop_event.is_set():
            app.broker_state.persist(cfg)
            time.sleep(1.0)
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
