#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Version 2.0 emergency_aprs_bridge -> Pasarela APRS de emergencia para el MiniBroker.

Flujo soportado:
1. Broker JSONL -> APRS RF / APRS-IS
   - Detecta mensajes Mesh de emergencia con prefijo [EMER], [SOS] o [ALERTA].
   - Ejemplo: [EMER] Persona herida en monte
   - Ejemplo: [SOS] Sin cobertura movil en la zona
   - Ejemplo: [ALERTA] Corte general de energia

2. APRS RF -> Broker
   - Decodifica tramas KISS desde Dire Wolf / SoundModem.
   - Si el comentario o mensaje contiene [CHx], lo reenvía al canal x del broker.

3. APRS-IS opcional y bidireccional
   - Si hay credenciales, publica a APRS-IS los mismos mensajes de emergencia que salen por RF.
   - También puede consumir mensajes APRS-IS y reinyectarlos a Mesh si traen [CHx].

Notas:
- Esta rama elimina el canal UDP local del bot porque aquí no existe bot.
- Se conserva la idea del proyecto principal: KISS local + dedupe + control por loopback.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import threading
import time
import unicodedata
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


# ======================================================================================
# Utilidades generales
# ======================================================================================

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


def now_ts() -> float:
    return time.time()


def sanitize_text(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def _print_ts(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ======================================================================================
# Configuración
# ======================================================================================

@dataclass(slots=True)
class AprsConfig:
    broker_host: str = os.getenv("BROKER_HOST", "127.0.0.1")
    broker_port: int = safe_int(os.getenv("BROKER_PORT", "8765"), 8765)
    broker_ctrl_host: str = os.getenv("BROKER_CTRL_HOST", "127.0.0.1")
    broker_ctrl_port: int = safe_int(os.getenv("BROKER_CTRL_PORT", "8766"), 8766)

    kiss_host: str = os.getenv("KISS_HOST", "127.0.0.1")
    kiss_port: int = safe_int(os.getenv("KISS_PORT", "8100"), 8100)
    kiss_channel: int = safe_int(os.getenv("KISS_CHANNEL", "0"), 0)
    kiss_txdelay: int = safe_int(os.getenv("KISS_TXDELAY", "30"), 30)
    kiss_persist: int = safe_int(os.getenv("KISS_PERSIST", "200"), 200)
    kiss_slottime: int = safe_int(os.getenv("KISS_SLOTTIME", "10"), 10)
    kiss_txtail: int = safe_int(os.getenv("KISS_TXTAIL", "3"), 3)

    aprs_call: str = (os.getenv("APRS_CALL", "") or "").strip().upper()
    aprs_path: str = os.getenv("APRS_PATH", "WIDE1-1,WIDE2-1")
    aprs_msg_max: int = safe_int(os.getenv("APRS_MSG_MAX", "67"), 67)
    aprs_status_max: int = safe_int(os.getenv("APRS_STATUS_MAX", "67"), 67)
    aprs_gate_enabled: bool = truthy(os.getenv("APRS_GATE_ENABLED", "1"), True)
    aprs_allowed_sources: tuple[str, ...] = tuple(
        s.strip().upper() for s in (os.getenv("APRS_ALLOWED_SOURCES", "") or "").split(",") if s.strip()
    )
    emergency_prefixes: tuple[str, ...] = tuple(
        s.strip().upper() for s in (os.getenv("EMERGENCY_PREFIXES", "EMER,SOS,ALERTA") or "EMER,SOS,ALERTA").split(",") if s.strip()
    )
    emergency_allowed_channels: tuple[int, ...] = tuple(
        safe_int(s.strip(), -1) for s in (os.getenv("APRS_EMERGENCY_ALLOWED_CHANNELS", "") or "").split(",") if s.strip()
    )
    emergency_min_body_len: int = safe_int(os.getenv("APRS_EMERGENCY_MIN_BODY_LEN", "3"), 3)
    emergency_include_channel: bool = truthy(os.getenv("APRS_EMERGENCY_INCLUDE_CHANNEL", "1"), True)
    tx_min_interval_sec: float = float(os.getenv("APRS_TX_MIN_INTERVAL_SEC", "0.8") or "0.8")

    aprsis_user: str = (os.getenv("APRSIS_USER", "") or "").strip().upper()
    aprsis_passcode: str = (os.getenv("APRSIS_PASSCODE", "") or "").strip()
    aprsis_host: str = os.getenv("APRSIS_HOST", "rotate.aprs2.net")
    aprsis_port: int = safe_int(os.getenv("APRSIS_PORT", "14580"), 14580)
    aprsis_filter: str = (os.getenv("APRSIS_FILTER", "") or "").strip()

    mesh_default_channel: int = safe_int(os.getenv("MESHTASTIC_CH", "0"), 0)
    home_node_id: str = (os.getenv("HOME_NODE_ID", "") or "").strip()

    # Nodos destino para mensajes APRS inbound.
    # Formato: alias separados por coma tal como aparecen en NODE_N_ALIAS del broker.
    # Vacío → comportamiento legacy: solo el nodo PRIMARY del broker recibe el mensaje.
    # Ejemplo: APRS_INBOUND_NODE_ALIASES=local-usb,remoto-wifi,meshcore-zgz
    aprs_inbound_node_aliases: tuple[str, ...] = tuple(
        s.strip() for s in (os.getenv("APRS_INBOUND_NODE_ALIASES", "") or "").split(",") if s.strip()
    )

    # Si 1, los mensajes APRS que contengan un prefijo de emergencia reconocido
    # ([EMER], [SOS], [ALERTA] u otros en EMERGENCY_PREFIXES) se inyectan a Mesh
    # aunque no lleven etiqueta [CHx], usando mesh_default_channel como canal.
    # Si 0, el comportamiento es el original: sin [CHx] el mensaje se descarta.
    aprs_inbound_emergency_no_chtag: bool = truthy(
        os.getenv("APRS_INBOUND_EMERGENCY_NO_CHTAG", "1"), True
    )

    data_dir: Path = Path(os.getenv("MINIBROKER_DATA_DIR", "./data")).resolve()

    def __post_init__(self) -> None:
        self.kiss_channel = max(0, min(15, int(self.kiss_channel)))
        self.data_dir.mkdir(parents=True, exist_ok=True)


# ======================================================================================
# Cliente de control al broker
# ======================================================================================

class BrokerControlClient:
    """Cliente JSONL sencillo para el puerto de control del minibroker."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)

    def request(self, cmd: str, params: dict | None = None, timeout: float = 6.0) -> dict:
        payload = {"cmd": str(cmd).upper(), "params": params or {}}
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                sock.sendall(data)
                sock.settimeout(timeout)
                buffer = b""
                while not buffer.endswith(b"\n"):
                    chunk = sock.recv(65535)
                    if not chunk:
                        break
                    buffer += chunk
            raw = buffer.decode("utf-8", errors="ignore").strip()
            return json.loads(raw) if raw else {"ok": False, "error": "empty reply"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def send_text(self, *, ch: int, text: str, dest: str | None = None, ack: bool = False) -> dict:
        """Envía texto al nodo PRIMARY del broker (comportamiento legacy)."""
        return self.request(
            "SEND_TEXT",
            {"text": text, "ch": int(ch), "dest": dest, "ack": safe_bool(ack, False)},
        )

    def send_text_to_node(
        self,
        *,
        ch: int,
        text: str,
        node_alias: str,
        dest: str | None = None,
        ack: bool = False,
    ) -> dict:
        """Envía texto a un nodo concreto identificado por su alias.

        Args:
            ch:         Canal Meshtastic destino.
            text:       Texto a enviar.
            node_alias: Alias del nodo broker destino (NODE_N_ALIAS).
            dest:       NodeId Meshtastic destino (None = broadcast).
            ack:        Si True solicita acuse de recibo.

        Returns:
            Dict con 'ok' y opcionalmente 'error'.
        """
        return self.request(
            "SEND_TEXT",
            {"text": text, "ch": int(ch), "dest": dest, "ack": safe_bool(ack, False), "alias": node_alias},
        )

    def broadcast_text(
        self,
        *,
        ch: int,
        text: str,
        dest: str | None = None,
        ack: bool = False,
    ) -> dict:
        """Envía texto a TODOS los nodos activos del broker via BROADCAST_TEXT.

        Args:
            ch:   Canal Meshtastic destino.
            text: Texto a enviar.
            dest: NodeId Meshtastic destino (None = broadcast).
            ack:  Si True solicita acuse de recibo.

        Returns:
            Dict con 'ok' y 'results' por nodo.
        """
        return self.request(
            "BROADCAST_TEXT",
            {"text": text, "ch": int(ch), "dest": dest, "ack": safe_bool(ack, False)},
        )


# ======================================================================================
# KISS / AX.25 helpers
# ======================================================================================

_FEND = 0xC0
_FESC = 0xDB
_TFEND = 0xDC
_TFESC = 0xDD


def kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == _FEND:
            out.extend([_FESC, _TFEND])
        elif b == _FESC:
            out.extend([_FESC, _TFESC])
        else:
            out.append(b)
    return bytes(out)


def kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == _FESC and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == _TFEND:
                out.append(_FEND)
                i += 2
                continue
            if nxt == _TFESC:
                out.append(_FESC)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def kiss_wrap(ax25_frame: bytes, *, port: int, cmd: int = 0x00) -> bytes:
    typ = ((int(port) & 0x0F) << 4) | (cmd & 0x0F)
    return bytes([_FEND]) + bytes([typ]) + kiss_escape(ax25_frame) + bytes([_FEND])


def kiss_iter_frames_from_buffer(buf: bytearray) -> list[bytes]:
    out: list[bytes] = []
    while True:
        try:
            start = buf.index(_FEND)
        except ValueError:
            break
        try:
            end = buf.index(_FEND, start + 1)
        except ValueError:
            if start > 0:
                del buf[:start]
            break
        raw = bytes(buf[start + 1:end])
        del buf[:end + 1]
        if not raw:
            continue
        if (raw[0] & 0x0F) != 0x00:
            continue
        out.append(kiss_unescape(raw[1:]))
    return out


def _call_ssid_parts(call: str) -> tuple[str, int]:
    s = (call or "").strip().upper()
    if "-" in s:
        base, ssid_text = s.split("-", 1)
        try:
            ssid = int(ssid_text)
        except Exception:
            ssid = 0
    else:
        base = s
        ssid = 0
    return base[:6].ljust(6), max(0, min(15, ssid))


def _addr_field(call: str, *, last: bool) -> bytes:
    base, ssid = _call_ssid_parts(call)
    out = bytearray(7)
    for idx, ch in enumerate(base.encode("ascii", "ignore")[:6]):
        out[idx] = (ch << 1) & 0xFE
    out[6] = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
    return bytes(out)


def build_ax25_ui(*, dest: str, src: str, path: list[str], payload: bytes) -> bytes:
    hops = [dest, src] + list(path or [])
    addr_bytes = [_addr_field(hop, last=(idx == len(hops) - 1)) for idx, hop in enumerate(hops)]
    return b"".join(addr_bytes) + b"\x03\xF0" + payload


def _decode_addr(addr7: bytes) -> tuple[str, int, bool]:
    call = "".join(chr((addr7[i] >> 1) & 0x7F) for i in range(6)).strip()
    ssid = (addr7[6] >> 1) & 0x0F
    last = bool(addr7[6] & 0x01)
    return call, ssid, last


def parse_ax25_ui(frame: bytes) -> dict | None:
    try:
        view = memoryview(frame)
        addrs = []
        off = 0
        while True:
            if off + 7 > len(view):
                return None
            raw = bytes(view[off:off + 7])
            off += 7
            addrs.append(_decode_addr(raw))
            if raw[6] & 0x01:
                break
        if off + 2 > len(view):
            return None
        control = view[off]
        pid = view[off + 1]
        off += 2
        if control != 0x03 or pid != 0xF0:
            return None

        info_raw = bytes(view[off:])
        info = info_raw.decode("utf-8", errors="ignore")
        dest_call = f"{addrs[0][0]}-{addrs[0][1]}" if addrs else ""
        src_call = f"{addrs[1][0]}-{addrs[1][1]}" if len(addrs) > 1 else ""
        path = [f"{call}-{ssid}" for (call, ssid, _last) in addrs[2:]] if len(addrs) > 2 else []

        out = {"dest": dest_call, "src": src_call, "path": path, "info": info, "info_raw": info_raw}
        if info.startswith(">"):
            out["type"] = "status"
            out["text"] = info[1:].strip()
            return out
        if info.startswith(":") and len(info) >= 11 and ":" in info[10:]:
            msg_dest = info[1:10].strip()
            rest = info[10:]
            if rest.startswith(":"):
                rest = rest[1:]
            out["type"] = "message"
            out["msg_dest"] = msg_dest
            out["text"] = re.sub(r"\{[ -~]{1,5}\}$", "", rest).strip()
            return out
        return out
    except Exception:
        return None


# ======================================================================================
# APRS text helpers
# ======================================================================================

_APRS_ALLOWED = {chr(c) for c in range(32, 127)}
_EMERGENCY_PREFIX_RE = re.compile(r"^\s*\[([A-Z0-9_\-]{2,16})\]\s*(.+?)\s*$", re.IGNORECASE)
_CH_TAG_RE = re.compile(r"\[(?:CH|CANAL)\s*([0-9]{1,2})(?:\s*\+\s*([0-9]{1,4}))?\]", re.IGNORECASE)


def aprs_ascii(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", str(text))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii", "ignore")
    safe = "".join(ch if ch in _APRS_ALLOWED else "?" for ch in ascii_only)
    return " ".join(safe.split())


def split_by_words(text: str, max_len: int) -> list[str]:
    text = sanitize_text(text)
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current = ""
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        candidate = current + token
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current.strip():
            parts.append(current.strip())
        current = token.strip()
        while len(current) > max_len:
            parts.append(current[:max_len])
            current = current[max_len:]
    if current.strip():
        parts.append(current.strip())
    return parts


def build_aprs_status_chunks(text: str, limit: int) -> list[bytes]:
    base = split_by_words(aprs_ascii(text), int(limit))
    if not base:
        return []
    if len(base) == 1:
        return [(">" + base[0]).encode("ascii", errors="ignore")]
    suffix_len = len(f" ({len(base)}/{len(base)})")
    refined = split_by_words(aprs_ascii(text), max(1, int(limit) - suffix_len))
    total = len(refined)
    return [(f">{part} ({idx}/{total})").encode("ascii", errors="ignore") for idx, part in enumerate(refined, 1)]


def build_aprs_message_chunks(dest_call: str, text: str, limit: int) -> list[bytes]:
    target = ((dest_call or "").upper().strip() + " " * 9)[:9]
    base = split_by_words(aprs_ascii(text), max(1, int(limit) - len(" (99/99)")))
    if not base:
        return []
    if len(base) == 1:
        return [f":{target}:{base[0]}".encode("ascii", errors="ignore")]
    total = len(base)
    return [f":{target}:{part} ({idx}/{total})".encode("ascii", errors="ignore") for idx, part in enumerate(base, 1)]


def extract_channel_tag(text: str, default_channel: int) -> tuple[int | None, int | None, str]:
    clean = sanitize_text(text)
    if not clean:
        return None, None, ""
    match = _CH_TAG_RE.search(clean)
    if not match:
        return None, None, clean
    try:
        channel = max(0, min(15, int(match.group(1))))
    except Exception:
        channel = int(default_channel)
    delay_min = None
    if match.group(2):
        try:
            delay_min = max(0, int(match.group(2)))
        except Exception:
            delay_min = None
    stripped = (clean[:match.start()] + " " + clean[match.end():]).strip()
    stripped = re.sub(r"\s{2,}", " ", stripped)
    return channel, delay_min, stripped


def add_aprs_src_prefix(src: str, msg: str) -> str:
    source = (src or "").strip().upper()
    text = sanitize_text(msg)
    if not source:
        return text
    if text.upper().startswith(source + ":"):
        return text
    if text.startswith(":"):
        return f"{source}: {text}"
    return f"{source}: {text}"

def mesh_display_id(node_id: Any) -> str:
    """
    Normaliza el identificador Mesh a formato visual corto tipo '!6985aba0'.
    Si no hay valor, devuelve '!?'.

    Uso:
        did = mesh_display_id(source_event.get("from"))
    """
    raw = "" if node_id is None else str(node_id).strip()
    if not raw:
        return "!?"
    if raw.startswith("!"):
        return raw
    if len(raw) >= 8:
        return f"!{raw[-8:]}"
    return f"!{raw}"


def mesh_display_name(alias: Any, node_id: Any) -> str:
    """
    Devuelve el nombre visual a usar en APRS para el origen Mesh.

    Regla:
      - si hay alias válido -> alias
      - si no -> !nodeid corto

    Uso:
        shown = mesh_display_name(source_event.get("from_alias"), source_event.get("from"))
    """
    if isinstance(alias, str):
        a = alias.strip()
        if a:
            return a
    return mesh_display_id(node_id)

def parse_emergency_mesh_text(text: str, allowed_tags: tuple[str, ...]) -> tuple[str | None, str]:
    """
    Detecta si un texto Mesh es una alerta de emergencia válida.

    Devuelve:
    - etiqueta normalizada: EMER / SOS / ALERTA, o None si no coincide.
    - cuerpo limpio del mensaje sin el prefijo.
    """
    clean = sanitize_text(text)
    if not clean:
        return None, ""
    match = _EMERGENCY_PREFIX_RE.match(clean)
    if not match:
        return None, clean
    tag = (match.group(1) or "").strip().upper()
    body = sanitize_text(match.group(2) or "")
    allowed = {str(x).strip().upper() for x in (allowed_tags or ()) if str(x).strip()}
    return (tag if tag in allowed else None), body


def format_mesh_emergency_for_aprs(*, emergency_tag: str, body: str, source_event: dict, include_channel: bool = True) -> str:
    """
    Construye el texto final que se enviará a APRS.

    Formato de producción:
    [TAG] <alias_o_id> CH<canal>: <texto>

    Reglas:
    - si existe alias del nodo emisor, se usa alias
    - si no existe alias, se usa !id corto
    - el texto se normaliza a ASCII seguro APRS y se compactan espacios
    """
    tag = (emergency_tag or "EMER").strip().upper()
    body_clean = aprs_ascii(sanitize_text(body))
    origin = source_event.get("from_display") or mesh_display_name(source_event.get("from_alias"), source_event.get("from"))
    origin = aprs_ascii(origin)
    try:
        channel = max(0, min(15, int(source_event.get("channel", 0))))
    except Exception:
        channel = 0

    prefix = f"[MT:{tag}]"
    context = f"{origin} CH{channel}" if include_channel else f"{origin}"
    if body_clean:
        return sanitize_text(f"{prefix} {context}: {body_clean}")
    return sanitize_text(f"{prefix} {context}")


# ======================================================================================
# APRS-IS opcional
# ======================================================================================

class AprsIsClient:
    """
    Cliente APRS-IS persistente.

    Requiere aprslib instalada.
    - connect() abre la sesión.
    - send_line() publica una línea TNC2.
    - start_consumer() arranca un hilo que recibe tramas y las entrega a un callback.
    """

    def __init__(self, cfg: AprsConfig):
        self.cfg = cfg
        self._client = None
        self._available = False
        self._last_error: str | None = None
        self._lock = threading.Lock()
        self._rx_thread: threading.Thread | None = None
        self._stop = threading.Event()
        try:
            import aprslib  # type: ignore
            self._aprslib = aprslib
            self._available = True
        except Exception as exc:
            self._aprslib = None
            self._last_error = f"{type(exc).__name__}: {exc}"

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.aprsis_user and self.cfg.aprsis_passcode and self._available)

    def connect(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._client is not None:
                return
            client = self._aprslib.IS(self.cfg.aprsis_user, passwd=self.cfg.aprsis_passcode, host=self.cfg.aprsis_host, port=self.cfg.aprsis_port)
            client.connect()
            if self.cfg.aprsis_filter:
                try:
                    client.sendall(f"filter {self.cfg.aprsis_filter}")
                except Exception:
                    pass
            self._client = client
            _print_ts(f"[aprs-is] conectado como {self.cfg.aprsis_user} -> {self.cfg.aprsis_host}:{self.cfg.aprsis_port}")

    def send_line(self, line: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.connect()
            with self._lock:
                assert self._client is not None
                self._client.sendall(str(line).rstrip("\r\n") + "\n")
            return True
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._client = None
            _print_ts(f"[aprs-is] TX error: {self._last_error}")
            return False

    def start_consumer(self, callback: Callable[[dict], None]) -> None:
        if not self.enabled:
            return
        if self._rx_thread and self._rx_thread.is_alive():
            return
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._consumer_loop, args=(callback,), name="aprsis-rx", daemon=True)
        self._rx_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=2.0)

    def _consumer_loop(self, callback: Callable[[dict], None]) -> None:
        while not self._stop.is_set():
            try:
                self.connect()
                assert self._client is not None
                self._client.consumer(callback, raw=False, immortal=False)
                if not self._stop.is_set():
                    raise RuntimeError("APRS-IS consumer finalizado inesperadamente")
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    try:
                        if self._client is not None and hasattr(self._client, "close"):
                            self._client.close()
                    except Exception:
                        pass
                    self._client = None
                _print_ts(f"[aprs-is] RX error: {self._last_error}")
                time.sleep(5.0)


# ======================================================================================
# Pasarela principal
# ======================================================================================

class EmergencyAprsBridge:
    """Pasarela completa APRS <-> MiniBroker."""

    def __init__(self, cfg: AprsConfig):
        self.cfg = cfg
        self.ctrl = BrokerControlClient(cfg.broker_ctrl_host, cfg.broker_ctrl_port)
        self.aprsis = AprsIsClient(cfg)
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dedup: dict[str, float] = {}
        self._dedup_ttl = float(os.getenv("APRS_DEDUP_TTL", "20") or "20")
        self._inbound_dedup: dict[str, float] = {}
        self._inbound_dedup_ttl = float(os.getenv("APRS_INBOUND_DEDUP_TTL", "30") or "30")
        self._aprs_rx_log = cfg.data_dir / "aprs_rx.jsonl"
        self._aprs_tx_log = cfg.data_dir / "aprs_tx.jsonl"
        self._log_rotate_max_bytes = safe_int(os.getenv("APRS_LOG_ROTATE_MAX_BYTES", "2097152"), 2097152)
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._last_tx_ts = 0.0
        self._tx_lock = asyncio.Lock()
        self._kiss_tx_sock: socket.socket | None = None
        self._kiss_tx_sock_lock = threading.Lock()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        tasks = [
            asyncio.create_task(self.task_broker_to_aprs(), name="broker-to-aprs"),
            asyncio.create_task(self.task_kiss_rx_to_mesh(), name="kiss-to-mesh"),
        ]
        if self.aprsis.enabled:
            self.aprsis.start_consumer(self._on_aprsis_packet)
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.aprsis.stop()

    def stop(self) -> None:
        self._stop.set()
        self._close_kiss_tx_sock()

    def _close_kiss_tx_sock(self) -> None:
        """Cierra el socket TX persistente de KISS de forma segura."""
        with self._kiss_tx_sock_lock:
            if self._kiss_tx_sock is not None:
                try:
                    self._kiss_tx_sock.close()
                except Exception:
                    pass
                self._kiss_tx_sock = None

    def _rotate_jsonl_if_needed(self, path: Path) -> None:
        """Rota un JSONL simple cuando supera el tamaño máximo configurado."""
        try:
            if not path.exists():
                return
            if path.stat().st_size < self._log_rotate_max_bytes:
                return
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists():
                try:
                    rotated.unlink()
                except Exception:
                    pass
            path.replace(rotated)
        except Exception as exc:
            _print_ts(f"[log] rotate error {path.name}: {type(exc).__name__}: {exc}")


    def _append_jsonl(self, path: Path, obj: dict) -> None:
        try:
            self._rotate_jsonl_if_needed(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception as exc:
            _print_ts(f"[log] append error {path.name}: {type(exc).__name__}: {exc}")

    def _dedup_key(self, dest: str, text: str) -> str:
        return f"{(dest or 'broadcast').strip().upper()}|{sanitize_text(text)}"

    def _dedup_seen(self, dest: str, text: str) -> bool:
        now = now_ts()
        key = self._dedup_key(dest, text)
        expiry = self._dedup.get(key)
        if expiry and expiry >= now:
            return True
        self._dedup[key] = now + self._dedup_ttl
        stale = [k for k, exp in self._dedup.items() if exp < now]
        for k in stale:
            self._dedup.pop(k, None)
        return False

    def _inbound_seen(self, *, source: str, src: str, ch: int | None, text: str) -> bool:
        now = now_ts()
        key = f"{source}|{(src or '').strip().upper()}|{ch}|{sanitize_text(text)}"
        expiry = self._inbound_dedup.get(key)
        if expiry and expiry >= now:
            return True
        self._inbound_dedup[key] = now + self._inbound_dedup_ttl
        stale = [k for k, exp in self._inbound_dedup.items() if exp < now]
        for k in stale:
            self._inbound_dedup.pop(k, None)
        return False

    async def task_broker_to_aprs(self) -> None:
        """
        Lee el stream JSONL del broker y reenvía a APRS únicamente
        los textos Mesh marcados explícitamente como emergencia.

        Prefijos admitidos al inicio del mensaje:
        - [EMER]
        - [SOS]
        - [ALERTA]

        Todos los envíos Mesh -> APRS de este minibroker salen como
        broadcast/status APRS y, si APRS-IS está disponible, también
        se publican allí.
        """
        backoff = 2.0
        while not self._stop.is_set():
            try:
                _print_ts(f"[broker->aprs] conectando a {self.cfg.broker_host}:{self.cfg.broker_port}")
                reader, writer = await asyncio.open_connection(self.cfg.broker_host, self.cfg.broker_port)
                _print_ts("[broker->aprs] conectado")
                backoff = 2.0
                try:
                    while not self._stop.is_set():
                        raw = await reader.readline()
                        if not raw:
                            raise ConnectionError("broker closed stream")
                        try:
                            obj = json.loads(raw.decode("utf-8", errors="ignore"))
                        except Exception:
                            continue
                        parsed = self._extract_broker_text_event(obj)
                        if not parsed:
                            continue
                        emergency_tag, emergency_body = parse_emergency_mesh_text(parsed["text"], self.cfg.emergency_prefixes)
                        if not emergency_tag or not emergency_body:
                            continue
                        if len(sanitize_text(emergency_body)) < max(1, int(self.cfg.emergency_min_body_len)):
                            continue
                        if self.cfg.emergency_allowed_channels and int(parsed["channel"]) not in set(self.cfg.emergency_allowed_channels):
                            continue
                        payload_text = format_mesh_emergency_for_aprs(
                            emergency_tag=emergency_tag,
                            body=emergency_body,
                            source_event=parsed,
                            include_channel=bool(self.cfg.emergency_include_channel),
                        )
                        await self._send_outbound_aprs(dest_token="broadcast", payload_text=payload_text, source_event=parsed)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _print_ts(f"[broker->aprs] error: {type(exc).__name__}: {exc} ; reintento en {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)

    def _extract_broker_text_event(self, obj: dict) -> dict | None:
        """
        Normaliza eventos del broker.

        Soporta:
        - eventos planos
        - eventos envueltos
        - alias del emisor si viene en:
            * summary.from_alias
            * packet.from_alias
            * raw.from_alias

        Devuelve un dict homogéneo para el pipeline broker -> APRS.

        Uso:
            parsed = self._extract_broker_text_event(obj)
        """
        packet = obj.get("packet") if isinstance(obj.get("packet"), dict) else obj
        summary = obj.get("summary") if isinstance(obj.get("summary"), dict) else {}
        decoded = packet.get("decoded") if isinstance(packet.get("decoded"), dict) else {}

        portnum = str(
            summary.get("portnum")
            or decoded.get("portnum")
            or packet.get("portnum")
            or obj.get("portnum")
            or ""
        ).upper()

        text = summary.get("text") or decoded.get("text") or packet.get("text") or obj.get("text")
        if portnum != "TEXT_MESSAGE_APP" or not isinstance(text, str) or not text.strip():
            return None

        channel = summary.get("channel")
        if channel is None:
            channel = packet.get("channel", packet.get("channelIndex", obj.get("channel")))

        from_id = (
            summary.get("from")
            or packet.get("from")
            or packet.get("fromId")
            or obj.get("from")
            or obj.get("fromId")
        )

        from_alias = (
            summary.get("from_alias")
            or packet.get("from_alias")
            or obj.get("from_alias")
        )
        from_display = (
            summary.get("from_display")
            or packet.get("from_display")
            or obj.get("from_display")
        )

        return {
            "text": text.strip(),
            "channel": safe_int(channel, self.cfg.mesh_default_channel),
            "from": from_id,
            "from_alias": (str(from_alias).strip() if from_alias is not None else None),
            "from_display": (str(from_display).strip() if from_display is not None else None),
            "raw": obj,
        }
    
    def _build_mesh_origin_prefix(self, source_event: dict) -> str:
        """
        Construye el prefijo visual del origen Mesh para incrustarlo en APRS.

        Formato:
          - alias si existe
          - si no, id corto !xxxxxxxx

        Ejemplos:
          N1-Zgz-Romareda
          !6985aba0
        """
        return mesh_display_name(
            source_event.get("from_alias"),
            source_event.get("from"),
        )    



    async def _throttle_tx(self) -> None:
        """Limita la cadencia de transmisión APRS para no inundar RF/APRS-IS."""
        async with self._tx_lock:
            wait = max(0.0, float(self.cfg.tx_min_interval_sec) - (now_ts() - self._last_tx_ts))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_tx_ts = now_ts()

    async def _send_outbound_aprs(self, *, dest_token: str, payload_text: str, source_event: dict) -> None:
        """
        Envía un mensaje hacia APRS RF y, si está activo, también hacia APRS-IS.

        Puntos corregidos para producción:
        - el texto ya debe venir formateado con alias/id y canal
        - se aplica deduplicación antes del envío
        - el envío RF usa argumentos por nombre para evitar fallo con to_thread
        - APRS-IS reutiliza exactamente la misma carga útil publicada por RF
        """
        dest_norm = "broadcast" if dest_token.lower() in {"broadcast", "all"} else dest_token.upper()
        origin_display = self._build_mesh_origin_prefix(source_event)
        ch = safe_int(source_event.get("channel"), self.cfg.mesh_default_channel)
        final_payload_text = aprs_ascii(sanitize_text(payload_text))
        if not final_payload_text:
            _print_ts("[broker->aprs] omitido: texto vacío tras sanitizar")
            return

        if self._dedup_seen(dest_norm, final_payload_text):
            _print_ts(f"[broker->aprs] duplicado omitido dest={dest_norm}")
            return

        if dest_norm == "broadcast":
            payloads = build_aprs_status_chunks(final_payload_text, self.cfg.aprs_status_max)
            rf_dest = "APRS"
        else:
            payloads = build_aprs_message_chunks(dest_norm, final_payload_text, self.cfg.aprs_msg_max)
            rf_dest = dest_norm

        ok_rf = True
        for payload in payloads:
            await self._throttle_tx()
            ok = await asyncio.to_thread(self._tx_rf_payload, payload=payload, dest_hdr=rf_dest)
            ok_rf = ok_rf and ok
            await asyncio.sleep(0.12)

        ok_is = True
        if self.aprsis.enabled:
            for payload in payloads:
                try:
                    line = self._payload_to_tnc2_line(payload=payload, dest_hdr=rf_dest)
                    if line:
                        sent = await asyncio.to_thread(self.aprsis.send_line, line)
                        ok_is = ok_is and sent
                except Exception as exc:
                    ok_is = False
                    _print_ts(f"[broker->aprs] APRS-IS error: {type(exc).__name__}: {exc}")

        ok_all = ok_rf and ok_is
        self._append_jsonl(
            self._aprs_tx_log,
            {
                "ts": now_ts(),
                "type": "mesh_to_aprs",
                "dest": dest_norm,
                "rf_dest": rf_dest,
                "text": final_payload_text,
                "from": source_event.get("from"),
                "from_alias": source_event.get("from_alias"),
                "from_display": origin_display,
                "channel": ch,
                "rf_ok": ok_rf,
                "is_ok": (ok_is if self.aprsis.enabled else None),
                "ok": ok_all,
            },
        )

        _print_ts(
            f"[broker->aprs] TX {'OK' if ok_all else 'KO'} "
            f"dest={dest_norm} from={origin_display} ch={ch} text='{final_payload_text[:120]}'"
        )

    def _payload_to_tnc2_line(self, *, payload: bytes, dest_hdr: str) -> str:
        """
        Convierte una carga APRS ya preparada al formato TNC2 para APRS-IS.
        """
        try:
            info = payload.decode("ascii", errors="ignore")
        except Exception:
            info = ""
        if not info:
            return ""
        return f"{self.cfg.aprsis_user}>{dest_hdr},TCPIP*:{info}"


    def _send_aprsis(self, *, dest_norm: str, text: str) -> bool:
        if not self.aprsis.enabled:
            return False
        if dest_norm == "broadcast":
            line = f"{self.cfg.aprsis_user}>APRS,TCPIP*:>{aprs_ascii(text)}"
            return self.aprsis.send_line(line)
        body = aprs_ascii(text)
        msgid = f"{{{int(now_ts()) % 100:02d}}}"
        target = (dest_norm[:9]).ljust(9, " ")
        line = f"{self.cfg.aprsis_user}>APRS,TCPIP*::{target}:{body[:max(1, self.cfg.aprs_msg_max - len(msgid))]}{msgid}"
        return self.aprsis.send_line(line)

    def _tx_aprs_payload(self, *, payload: bytes, dest_hdr: str) -> bool:
        """Envía un payload APRS por KISS reutilizando un socket persistente.

        Args:
            payload: Carga APRS ya codificada.
            dest_hdr: Destino AX.25/APRS (ej. APRS o indicativo destino).

        Returns:
            True si el envío ha sido aceptado por el socket local KISS.
        """
        path = [p.strip() for p in (self.cfg.aprs_path or "").split(",") if p.strip()]
        ax25 = build_ax25_ui(dest=dest_hdr, src=self.cfg.aprs_call, path=path, payload=payload)
        kiss = kiss_wrap(ax25, port=self.cfg.kiss_channel)

        with self._kiss_tx_sock_lock:
            try:
                if self._kiss_tx_sock is None:
                    sock = socket.create_connection((self.cfg.kiss_host, self.cfg.kiss_port), timeout=3.0)
                    sock.settimeout(3.0)
                    self._kiss_init(sock)
                    self._kiss_tx_sock = sock
                self._kiss_tx_sock.sendall(kiss)
                return True
            except Exception as exc:
                _print_ts(f"[aprs-rf] TX error: {type(exc).__name__}: {exc}")
                try:
                    if self._kiss_tx_sock is not None:
                        self._kiss_tx_sock.close()
                except Exception:
                    pass
                self._kiss_tx_sock = None
                return False


    def _kiss_param_frame(self, cmd_id: int, value: bytes) -> bytes:
        typ = ((self.cfg.kiss_channel & 0x0F) << 4) | (cmd_id & 0x0F)
        return bytes([_FEND]) + bytes([typ]) + kiss_escape(value) + bytes([_FEND])

    def _kiss_init(self, sock: socket.socket) -> None:
        try:
            sock.sendall(self._kiss_param_frame(0x01, bytes([max(0, min(255, self.cfg.kiss_txdelay))])))
            sock.sendall(self._kiss_param_frame(0x02, bytes([max(1, min(255, self.cfg.kiss_persist))])))
            sock.sendall(self._kiss_param_frame(0x03, bytes([max(1, min(255, self.cfg.kiss_slottime))])))
            sock.sendall(self._kiss_param_frame(0x04, bytes([max(0, min(255, self.cfg.kiss_txtail))])))
        except Exception:
            pass

    async def task_kiss_rx_to_mesh(self) -> None:
        """
        Escucha tramas KISS desde Dire Wolf / SoundModem y las inyecta en Mesh cuando llevan [CHx].
        """
        backoff = 2.0
        while not self._stop.is_set():
            try:
                _print_ts(f"[aprs-rf] conectando KISS {self.cfg.kiss_host}:{self.cfg.kiss_port}")
                reader, writer = await asyncio.open_connection(self.cfg.kiss_host, self.cfg.kiss_port)
                _print_ts("[aprs-rf] KISS conectado")
                backoff = 2.0
                buf = bytearray()
                try:
                    while not self._stop.is_set():
                        chunk = await reader.read(4096)
                        if not chunk:
                            raise ConnectionError("KISS peer closed")
                        buf.extend(chunk)
                        for frame in kiss_iter_frames_from_buffer(buf):
                            pkt = parse_ax25_ui(frame)
                            if not pkt:
                                continue
                            await self._handle_aprs_packet(pkt, source="rf")
                finally:
                    writer.close()
                    await writer.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _print_ts(f"[aprs-rf] RX error: {type(exc).__name__}: {exc} ; reintento en {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)

    async def _handle_aprs_packet(self, pkt: dict, *, source: str) -> None:
        """Procesa una trama APRS recibida (RF o APRS-IS) e inyecta a Mesh si procede.

        Lógica de canal:
        - Si el texto lleva etiqueta [CHx] se usa ese canal.
        - Si no lleva [CHx] pero aprs_inbound_emergency_no_chtag=True y el texto
          contiene un prefijo de emergencia reconocido ([EMER]/[SOS]/[ALERTA] etc.),
          se usa mesh_default_channel.
        - En cualquier otro caso sin [CHx] el mensaje se descarta.

        Lógica de routing a nodos:
        - Si aprs_inbound_node_aliases está configurado, el mensaje se envía
          individualmente a cada alias listado mediante SEND_TEXT con alias=<nodo>.
        - Si la lista está vacía, se usa el comportamiento legacy: SEND_TEXT sin
          node_id, que enruta al nodo PRIMARY del broker.

        Args:
            pkt:    Dict con campos 'src', 'dest', 'path', 'type', 'text'/'info'
                    tal como devuelve parse_ax25_ui o aprslib.
            source: 'rf' o 'is' — origen de la trama para logging y dedup.
        """
        src = str(pkt.get("src") or "").strip().upper()

        # Filtro 1: origen permitido
        if self.cfg.aprs_allowed_sources and src not in set(self.cfg.aprs_allowed_sources):
            return

        # Filtro 2: anti-eco (no procesar tramas propias)
        if src and src in {self.cfg.aprs_call.upper(), self.cfg.aprsis_user.upper()}:
            return

        text = sanitize_text(str(pkt.get("text") or pkt.get("info") or ""))
        if not text:
            return

        # Paso A: intentar extraer etiqueta [CHx]
        ch, delay_min, stripped = extract_channel_tag(text, self.cfg.mesh_default_channel)

        # Paso B: si no hay [CHx], comprobar prefijo de emergencia como fallback
        is_emergency_fallback = False
        if ch is None and self.cfg.aprs_inbound_emergency_no_chtag:
            emerg_tag, emerg_body = parse_emergency_mesh_text(text, self.cfg.emergency_prefixes)
            if emerg_tag and emerg_body:
                ch = self.cfg.mesh_default_channel
                stripped = text          # conservamos el texto completo con el prefijo
                delay_min = None
                is_emergency_fallback = True
                _print_ts(
                    f"[aprs->{source}->mesh] prefijo [{emerg_tag}] sin [CHx]: "
                    f"usando canal defecto CH{ch} src={src}"
                )

        # Dedup inbound (después de resolver ch para que la clave sea estable)
        if self._inbound_seen(source=source, src=src, ch=ch, text=stripped or text):
            _print_ts(f"[aprs->{source}->mesh] duplicado omitido src={src} ch={ch}")
            return

        # Log de recepción
        self._append_jsonl(self._aprs_rx_log, {
            "ts": now_ts(),
            "source": source,
            "src": src,
            "dest": pkt.get("dest"),
            "path": pkt.get("path"),
            "type": pkt.get("type"),
            "text": text,
            "channel": ch,
            "emergency_fallback": is_emergency_fallback,
        })

        # Gate: si gate deshabilitado, sin canal resuelto o sin texto útil → descartar
        if not self.cfg.aprs_gate_enabled or ch is None or not stripped:
            return

        msg_mesh = add_aprs_src_prefix(src, stripped)

        # Envío diferido (solo para mensajes con [CHx+Nmin], no aplica al fallback)
        if delay_min and delay_min > 0:
            node_aliases = list(self.cfg.aprs_inbound_node_aliases)
            asyncio.create_task(
                self._delayed_mesh_send(ch=ch, text=msg_mesh, delay_min=delay_min, node_aliases=node_aliases)
            )
            _print_ts(
                f"[aprs->{source}->mesh] programado CH{ch} +{delay_min}m "
                f"nodos={node_aliases or ['PRIMARY']} <- {src}: {msg_mesh[:120]}"
            )
            return

        # Envío inmediato a nodos configurados
        self._dispatch_to_nodes(ch=ch, text=msg_mesh, source=source, src=src)

        # Eco opcional al nodo home (sin cambios respecto al comportamiento previo)
        if self.cfg.home_node_id:
            eco = self.ctrl.send_text(
                ch=ch, text=f"[APRS eco de {src}] {msg_mesh}", dest=self.cfg.home_node_id, ack=False
            )
            _print_ts(f"[aprs->mesh ECO] -> {self.cfg.home_node_id}: {'OK' if eco.get('ok') else 'KO'}")

    def _dispatch_to_nodes(self, *, ch: int, text: str, source: str, src: str) -> None:
        """Envía un mensaje Mesh a los nodos destino configurados.

        Si aprs_inbound_node_aliases tiene alias, itera cada uno y llama
        SEND_TEXT con ese alias.  Si la lista está vacía usa el comportamiento
        legacy: SEND_TEXT sin node_id → nodo PRIMARY del broker.

        Args:
            ch:     Canal Meshtastic destino.
            text:   Texto ya formateado (con prefijo de origen).
            source: 'rf' o 'is' — solo para logging.
            src:    Indicativo APRS origen — solo para logging.
        """
        aliases = list(self.cfg.aprs_inbound_node_aliases)

        if not aliases:
            # Comportamiento legacy: PRIMARY
            res = self.ctrl.send_text(ch=ch, text=text, dest=None, ack=False)
            _print_ts(
                f"[aprs->{source}->mesh] CH{ch} nodo=PRIMARY <- {src}: "
                f"{'OK' if res.get('ok') else 'KO'} {text[:120]}"
            )
            return

        for alias in aliases:
            res = self.ctrl.send_text_to_node(ch=ch, text=text, node_alias=alias, dest=None, ack=False)
            _print_ts(
                f"[aprs->{source}->mesh] CH{ch} nodo={alias} <- {src}: "
                f"{'OK' if res.get('ok') else 'KO'} {text[:120]}"
            )

    async def _delayed_mesh_send(
        self,
        *,
        ch: int,
        text: str,
        delay_min: int,
        node_aliases: list[str] | None = None,
    ) -> None:
        """Envío diferido a Mesh tras un retardo en minutos.

        Respeta la misma lógica de routing multi-nodo que _dispatch_to_nodes.

        Args:
            ch:           Canal Meshtastic destino.
            text:         Texto a enviar.
            delay_min:    Retardo en minutos antes del envío.
            node_aliases: Lista de alias de nodos destino.  None o vacío → PRIMARY.
        """
        await asyncio.sleep(max(0, int(delay_min)) * 60)
        aliases = list(node_aliases or [])
        if not aliases:
            res = self.ctrl.send_text(ch=ch, text=text, dest=None, ack=False)
            _print_ts(
                f"[aprs->mesh delayed] CH{ch} nodo=PRIMARY: "
                f"{'OK' if res.get('ok') else 'KO'} {text[:120]}"
            )
            return
        for alias in aliases:
            res = self.ctrl.send_text_to_node(ch=ch, text=text, node_alias=alias, dest=None, ack=False)
            _print_ts(
                f"[aprs->mesh delayed] CH{ch} nodo={alias}: "
                f"{'OK' if res.get('ok') else 'KO'} {text[:120]}"
            )

    def _on_aprsis_packet(self, pkt: dict) -> None:
        """
        Callback bloqueante desde el hilo consumer de aprslib.
        Siempre reinyecta el trabajo en el loop principal del bridge.
        """
        loop = self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._handle_aprs_packet(pkt, source="is"), loop)
            return
        try:
            asyncio.run(self._handle_aprs_packet(pkt, source="is"))
        except Exception as exc:
            _print_ts(f"[aprs-is] callback error: {type(exc).__name__}: {exc}")


# ======================================================================================
# CLI
# ======================================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pasarela APRS de emergencia para MiniBroker")
    parser.add_argument("--broker-host", default=os.getenv("BROKER_HOST", "127.0.0.1"), help="Host loopback del broker")
    parser.add_argument("--broker-port", type=int, default=safe_int(os.getenv("BROKER_PORT", "8765"), 8765), help="Puerto JSONL del broker")
    parser.add_argument("--broker-ctrl-port", type=int, default=safe_int(os.getenv("BROKER_CTRL_PORT", "8766"), 8766), help="Puerto de control del broker")
    parser.add_argument("--kiss-host", default=os.getenv("KISS_HOST", "127.0.0.1"), help="Host KISS de Dire Wolf o SoundModem")
    parser.add_argument("--kiss-port", type=int, default=safe_int(os.getenv("KISS_PORT", "8100"), 8100), help="Puerto KISS")
    parser.add_argument("--aprs-call", default=os.getenv("APRS_CALL", ""), help="Indicativo APRS local")
    parser.add_argument("--aprsis-user", default=os.getenv("APRSIS_USER", ""), help="Indicativo APRS-IS")
    parser.add_argument("--aprsis-passcode", default=os.getenv("APRSIS_PASSCODE", ""), help="Passcode APRS-IS")
    return parser


def make_config_from_args(args: argparse.Namespace) -> AprsConfig:
    cfg = AprsConfig(
        broker_host=args.broker_host,
        broker_port=int(args.broker_port),
        broker_ctrl_port=int(args.broker_ctrl_port),
        kiss_host=args.kiss_host,
        kiss_port=int(args.kiss_port),
        aprs_call=(args.aprs_call or "").strip().upper(),
        aprsis_user=(args.aprsis_user or "").strip().upper(),
        aprsis_passcode=(args.aprsis_passcode or "").strip(),
    )
    return cfg


async def async_main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    cfg = make_config_from_args(args)
    bridge = EmergencyAprsBridge(cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        _print_ts("[aprs] apagando…")
        bridge.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):  # type: ignore[name-defined]
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(bridge.run(), name="aprs-bridge")
    await stop_event.wait()
    await runner
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    import signal
    raise SystemExit(main())
