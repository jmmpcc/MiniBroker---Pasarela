#!/usr/bin/env bash
set -u
# minibroker_usb_watchdog_meshcore Version 4.2
# Watchdog reforzado para entorno 24/7:
# - Verifica que exista el USB real y el PTY virtual
# - Retry inteligente con timeout en PTY
# - Reinicia socat si falta alguno
# - Reinicia el broker si socat ha tenido que rearmarse
# - Supervisa Direwolf (APRS)
# - Supervisa MeshCore embebido
#
# Uso manual:
#   sudo /usr/local/bin/minibroker_usb_watchdog.sh
#
# Uso por systemd timer:
#   Ejecutarlo cada minuto.

USB_REAL="/dev/ttyUSB0"
USB_VIRTUAL="/dev/ttyV0"

SOCAT_SERVICE="meshtastic-socat.service"
BROKER_SERVICE="minibroker-emergencias.service"
DIREWOLF_SERVICE="direwolf.service"
APRS_SERVICE="${APRS_SERVICE:-minibroker-aprs.service}"

LOG_TAG="minibroker-usb-watchdog"

log() {
    /usr/bin/logger -t "$LOG_TAG" "$1"
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

restart_if_exists() {
    local svc="$1"
    if /usr/bin/systemctl cat "$svc" >/dev/null 2>&1; then
        log "Reiniciando servicio asociado: $svc"
        /usr/bin/systemctl restart "$svc"
    fi
}

need_broker_restart=0

# 1) Si el USB físico no existe, registrar y salir.
#    Aquí no forzamos nada sobre hardware inexistente.
if [ ! -e "$USB_REAL" ]; then
    log "USB físico ausente: $USB_REAL"
    exit 0
fi

# 2) Si socat no está activo, levantarlo.
if ! /usr/bin/systemctl is-active --quiet "$SOCAT_SERVICE"; then
    log "socat inactivo. Reiniciando $SOCAT_SERVICE"
    /usr/bin/systemctl restart "$SOCAT_SERVICE"
    sleep 3
    need_broker_restart=1
fi

# 3) Si no existe el pseudoTTY virtual, rearmar socat con retry inteligente.
if [ ! -e "$USB_VIRTUAL" ]; then
    log "PseudoTTY ausente: $USB_VIRTUAL. Reiniciando $SOCAT_SERVICE"
    /usr/bin/systemctl restart "$SOCAT_SERVICE"
    
    # ⭐ NUEVO: Retry con timeout de 10s (polling cada 1s)
    PTY_RECOVERED=0
    for i in {1..10}; do
        sleep 1
        if [ -e "$USB_VIRTUAL" ]; then
            log "PTY recuperado tras ${i}s"
            PTY_RECOVERED=1
            break
        fi
    done
    
    # Si tras 10s no apareció, registrar fallo persistente
    if [ "$PTY_RECOVERED" -eq 0 ]; then
        log "CRÍTICO: PTY no apareció tras 10s de reiniciar socat"
    fi
    
    need_broker_restart=1
fi

# 4) Si tras rearmar sigue sin existir, dejar constancia.
if [ ! -e "$USB_VIRTUAL" ]; then
    log "Fallo persistente: no aparece $USB_VIRTUAL tras reinicio de socat"
    exit 1
fi

# 5) Si socat fue rearmado, reiniciar el broker para limpiar descriptores.
if [ "$need_broker_restart" -eq 1 ]; then
    log "Reiniciando broker: $BROKER_SERVICE"
    /usr/bin/systemctl restart "$BROKER_SERVICE"
    restart_if_exists "$APRS_SERVICE"
    exit 0
fi

# 6) Si el broker está caído, levantarlo.
if ! /usr/bin/systemctl is-active --quiet "$BROKER_SERVICE"; then
    log "Broker inactivo. Reiniciando $BROKER_SERVICE"
    /usr/bin/systemctl restart "$BROKER_SERVICE"
    exit 0
fi

# 7) Supervisión adicional del puerto físico MeshCore embebido.
#    Si desaparece el USB de MeshCore, se reinicia el broker para forzar
#    una reconstrucción limpia de la sesión embebida y de sus descriptores.
MESHCORE_ENABLE="${MESHCORE_ENABLE:-0}"
MESHCORE_SERIAL="${MESHCORE_SERIAL_PORT:-/dev/ttyUSB1}"

if [ "$MESHCORE_ENABLE" = "1" ]; then
    if [ ! -e "$MESHCORE_SERIAL" ]; then
        log "MeshCore USB ausente: $MESHCORE_SERIAL. Reiniciando broker: $BROKER_SERVICE"
        /usr/bin/systemctl restart "$BROKER_SERVICE"
        exit 0
    fi
fi

# 7.5) ⭐ NUEVO: Verificar salud de Direwolf (APRS)
APRS_GATE_ENABLED="${APRS_GATE_ENABLED:-1}"

if [ "$APRS_GATE_ENABLED" = "1" ]; then
    if ! /usr/bin/systemctl is-active --quiet "$DIREWOLF_SERVICE"; then
        log "Direwolf inactivo. Reiniciando $DIREWOLF_SERVICE"
        /usr/bin/systemctl restart "$DIREWOLF_SERVICE"
        # Dar tiempo a Direwolf para abrir puerto KISS
        sleep 2
    fi
fi

# 8) Comprobación de estado lógico del broker (anti-zombie)
STATUS_FILE="/opt/minibroker/data/broker_status.json"

# 8) Comprobación de estado lógico del broker (anti-zombie)
#    Política corregida:
#    - No reiniciar por simple ausencia de tráfico.
#    - Reiniciar solo si el fichero de estado es inválido/corrupto
#      o si el broker declara un estado imposible durante tiempo prolongado.
STATUS_FILE="/opt/minibroker/data/broker_status.json"

if [ ! -f "$STATUS_FILE" ]; then
    log "STATUS_FILE ausente: $STATUS_FILE. Reiniciando $BROKER_SERVICE"
    /usr/bin/systemctl restart "$BROKER_SERVICE"
    exit 0
fi

if ! /usr/bin/python3 - <<'PY' "$STATUS_FILE"
import json, sys
p = sys.argv[1]
with open(p, "r", encoding="utf-8") as f:
    data = json.load(f)

# Validación mínima de estructura
if not isinstance(data, dict):
    raise SystemExit(2)

status = str(data.get("status", "")).strip().lower()
if status not in {"running", "connecting", "cooldown", "disconnected"}:
    raise SystemExit(3)

nodes = data.get("nodes", [])
if nodes is not None and not isinstance(nodes, list):
    raise SystemExit(4)

raise SystemExit(0)
PY
then
    rc=$?
    log "broker_status.json inválido/corrupto (rc=$rc). Reiniciando $BROKER_SERVICE"
    /usr/bin/systemctl restart "$BROKER_SERVICE"
    exit 0
fi

# 9) ⭐ NUEVO: Supervisión extendida de MeshCore
if [ "$MESHCORE_ENABLE" = "1" ] && [ -f "$STATUS_FILE" ]; then
    meshcore_ok=$(/usr/bin/python3 - <<'PY' "$STATUS_FILE"
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    nodes = data.get("nodes") or []
    candidates = []
    for node in nodes:
        if str(node.get("type") or "").startswith("meshcore"):
            v = node.get("last_packet_ts")
            if v is not None:
                candidates.append(float(v))
    print("" if not candidates else max(candidates))
except Exception:
    print("")
PY
)
    now=$(date +%s)
    
    if [ -n "$meshcore_ok" ]; then
        meshcore_ok_int="${meshcore_ok%.*}"
        case "$meshcore_ok_int" in
            ''|*[!0-9]*)
                meshcore_ok_int=0
                ;;
        esac

        silence=$((now - meshcore_ok_int))
        MESHCORE_WATCHDOG_SILENCE_SEC="${MESHCORE_WATCHDOG_SILENCE_SEC:-${MESHCORE_SILENCE_RECONNECT_SEC:-120}}"
        if [ "$silence" -gt "$MESHCORE_WATCHDOG_SILENCE_SEC" ]; then
            log "MeshCore sin actividad >${MESHCORE_WATCHDOG_SILENCE_SEC}s. Reiniciando $BROKER_SERVICE"
            /usr/bin/systemctl restart "$BROKER_SERVICE"
            exit 0
        fi
    fi
fi

log "OK: USB real y pseudoTTY presentes; servicios activos"
exit 0
