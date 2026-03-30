# MiniBroker v4.2 — Guía Completa de Instalación y Operación 24/7

> **Proyecto:** MiniBroker — Broker de mensajería mesh para comunicaciones de emergencia  
> **Versión documentada:** 4.2.0 multi-nodo  
> **Tecnologías:** Meshtastic · MeshCore · APRS RF · APRS-IS · Python 3.10+ · systemd  
> **Audiencia:** Radioaficionados, operadores de emergencias, integradores de red mesh  

## Atribución obligatoria

Este proyecto ha sido desarrollado por **J.M.M (EB2EAS)**.

Cualquier uso, redistribución, modificación, integración o creación de
derivados deberá incluir una referencia clara y visible al autor original
y un enlace al repositorio principal.

Ejemplo recomendado:
“Basado en el proyecto desarrollado por J.M.M (EB2EAS)”.

## Condición para forks y proyectos derivados

Cualquier fork o proyecto derivado que emplee una parte sustancial de este
código deberá incluir en su README un apartado visible reconociendo al autor
original: “Proyecto basado en el trabajo de J.M.M (EB2EAS)”.

Esta condición forma parte de la licencia MIT utilizada en este repositorio.
---

### 🌐 Guía ESCENARIOS configuración nodos

> 🔄 Documentación exponiendo los escenarios supuestos de configuración de los nodos (Meshtastic y Meshcore)  
> incluyendo configuración, ejemplos, variables de entorno y modos de operación.

📘 **[Abrir guía completa (.md) → Escenarios_nodos.md](./Documentos/Escenarios_nodos.md)**

📘 **[Abrir guía completa (.pdf) → Escenarios_nodos.pdf](./Documentos/Escenarios_nodos.pdf)**

📘 **[Mapeo de canales (.md) → Mapeado_Canales.md](./Documentos/Mapeado_Canales.md)**

📘 ** IMPORTANTE: [Guia MULTI NODO (.md) → Guia_Multi_Nodo.md](./Documentos/Guia_Multi_Nodo.md)**


---


## Tabla de contenidos

1. [Arquitectura general](#1-arquitectura-general)
2. [Requisitos previos](#2-requisitos-previos)
3. [Instalación del sistema](#3-instalación-del-sistema)
4. [Configuración del fichero .env](#4-configuración-del-fichero-env)
5. [Servicios systemd](#5-servicios-systemd)
6. [Watchdog 24/7](#6-watchdog-247)
7. [Dire Wolf — TNC por software](#7-dire-wolf--tnc-por-software)
8. [Casos de uso y flujo de mensajes](#8-casos-de-uso-y-flujo-de-mensajes)
9. [Mensajes de emergencia: comportamiento detallado](#9-mensajes-de-emergencia-comportamiento-detallado)
10. [Puerto de control — comandos JSONL](#10-puerto-de-control--comandos-jsonl)
11. [Operación 24/7 — procedimientos](#11-operación-247--procedimientos)
12. [Resolución de problemas](#12-resolución-de-problemas)
13. [Referencia rápida de variables de entorno](#13-referencia-rápida-de-variables-de-entorno)

---

## 1. Arquitectura general

```
┌─────────────────────────────────────────────────────────────────┐
│                    HOST (Raspberry Pi / Linux)                   │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐    │
│  │  Nodo A      │   │  Nodo B      │   │  Nodo C          │    │
│  │ Meshtastic   │   │ Meshtastic   │   │  MeshCore        │    │
│  │ serial/TCP   │   │  TCP         │   │  serial/TCP      │    │
│  └──────┬───────┘   └──────┬───────┘   └────────┬─────────┘    │
│         │                  │                     │              │
│         └──────────────────┴─────────────────────┘             │
│                            │  on_text_event                     │
│                    ┌───────▼────────┐                           │
│                    │  NodeManager   │                           │
│                    │ ChannelRouter  │                           │
│                    └───────┬────────┘                           │
│              ┌─────────────┴──────────────┐                     │
│              │                            │                     │
│       ┌──────▼───────┐           ┌────────▼────────┐           │
│       │  JsonlHub    │           │  ControlServer  │           │
│       │  port 8765   │           │   port 8766     │           │
│       └──────┬───────┘           └─────────────────┘           │
│              │ suscriptor                                        │
│       ┌──────▼──────────────┐                                   │
│       │  APRS Bridge        │                                   │
│       │ emergency_aprs_     │                                   │
│       │ bridge.py           │                                   │
│       └──────┬──────────────┘                                   │
│              │ KISS TCP                                          │
│       ┌──────▼───────┐       ┌─────────────────┐               │
│       │  Dire Wolf   │       │  APRS-IS         │               │
│       │  port 8100   │       │ rotate.aprs2.net │               │
│       └──────┬───────┘       └─────────────────┘               │
│              │ USB Audio                                         │
└──────────────┼─────────────────────────────────────────────────┘
               │ RF 144.800 MHz
          ═════╧══════
          Red APRS
```

### Componentes

| Componente | Archivo | Función |
|---|---|---|
| Broker multi-nodo | `emergency_broker.py` | Gestiona N nodos, routing entre ellos, hub JSONL, control |
| Pasarela APRS | `emergency_aprs_bridge.py` | Puente bidireccional Mesh ↔ APRS RF e APRS-IS |
| TNC software | Dire Wolf | Modem AFSK 1200 baud, KISS TCP, VOX |
| Puente serial | socat | `/dev/ttyUSB0` → `/dev/ttyV0` (PTY virtual) |
| Watchdog | `minibroker_usb_watchdog.sh` | Supervisión USB, socat y broker cada 60 s |


### Estados internos del broker

El broker opera con estados internos que determinan su comportamiento:

- CONNECTING: intento activo de conexión
- RUNNING: conexión establecida
- COOLDOWN: pausa tras fallo
- RECONNECT_PENDING: reconexión programada

Variables implicadas:
- _connecting (lock crítico)
- _cooldown_until
- _reconnect_event

Problema crítico conocido:
Si `_connecting` no se libera correctamente → bloqueo del puerto USB.

---

### Gestión de reconexión por tipo de nodo

#### USB (serial)
- Exclusivo (no admite múltiples accesos)
- Requiere liberación completa del descriptor
- Riesgo: "device busy"

#### TCP
- Reintentos seguros
- No bloqueante
- Timeout controlado

---

### Protección avanzada USB

Diagnóstico:
```
lsof /dev/ttyUSB0
fuser /dev/ttyUSB0
```

Mitigación:
- Reinicio de socat antes que broker
- Validación de descriptor libre
- Espera activa antes de reconectar

---

### Watchdog avanzado (comportamiento real)

Extensión lógica:

1. Verificar existencia de USB
2. Verificar descriptor libre
3. Verificar socat
4. Verificar PTY
5. Verificar broker activo
6. Verificar tráfico real
7. Reinicio selectivo

---

### Backpressure y cola de envío

El sistema usa `_sendq`:

- Límite: BROKER_SENDQ_MAX
- Persistencia en desconexión
- Reinyección tras reconexión

Riesgo:
- Saturación → latencia
- Solución: limitar entradas y priorizar

---

### Persistencia y backlog

- JSONL como fuente de verdad
- Relectura tras reinicio
- Evita pérdida de mensajes

---

### Multi-nodo real (failover)

Escenario:

- Nodo PRIMARY cae
- Nodo secundario toma control

Requiere:
- Detección de caída
- Reasignación de destino
- Evitar duplicados

---

### Anti-loop avanzado

Mecanismos:

- TTL de mensajes
- Hash de contenido
- Identificación de origen

Bloqueos:
- Mensajes #BBS
- Mensajes internos

---

### Triple bridge (arquitectura real)

Modos:

- brokerhub
- A→B
- B→A

Reglas:

- No replicar BBS
- No eco
- Delay configurable

---

### Diagnóstico avanzado

Herramientas:

```
ss -tulnp
lsof
fuser
journalctl
```

Análisis:

- Puerto ocupado
- Conexión zombie
- Reconexión en bucle

---

### Escenarios de fallo

#### USB desconectado
- watchdog detecta
- espera reconexión
- reinicia broker

#### TCP caído
- cooldown progresivo
- reconexión automática

#### socat caído
- reinicio inmediato
- reinicio broker asociado

---

## 2. Requisitos previos

### Hardware mínimo

- Raspberry Pi 2, 3, 4 (recomendado) o equivalente con Linux
- Nodo Meshtastic con firmware ≥ 2.3 (serial USB o WiFi)
- Interfaz de radio para APRS (opcional): radio VHF + adaptador USB audio
- Almacenamiento: tarjeta SD clase A1 ≥ 16 GB o SSD USB

### Software

```bash
# Sistema base
sudo apt update && sudo apt install -y python3 python3-pip python3-venv \
    git socat bc

# Dire Wolf (compilar desde fuente o paquete si disponible)
sudo apt install -y direwolf

# Verificar versión Python (mínimo 3.10)
python3 --version
```

### Usuario del sistema

```bash
sudo useradd -r -s /bin/false -d /opt/minibroker meshnet
sudo usermod -aG dialout meshnet   # acceso a puertos serie
sudo usermod -aG audio  meshnet   # acceso a audio para Dire Wolf
```

---

## 3. Instalación del sistema

### 3.1 Estructura de directorios

```bash
sudo mkdir -p /opt/minibroker/data
sudo mkdir -p /opt/minibroker/venv

# Copiar scripts del proyecto
sudo cp emergency_broker.py       /opt/minibroker/
sudo cp emergency_aprs_bridge.py  /opt/minibroker/
sudo cp .env-usb                  /opt/minibroker/

sudo chown -R meshnet:meshnet /opt/minibroker
```

### 3.2 Entorno virtual Python

```bash
sudo -u meshnet python3 -m venv /opt/minibroker/venv

sudo -u meshnet /opt/minibroker/venv/bin/pip install --upgrade pip
sudo -u meshnet /opt/minibroker/venv/bin/pip install \
    meshtastic \
    pyserial \
    requests
    # meshcore si se usa nodo MeshCore:
    # pip install meshcore
```

### 3.3 Watchdog

```bash
sudo cp minibroker_usb_watchdog.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/minibroker_usb_watchdog.sh
```

### 3.4 Dire Wolf

```bash
sudo cp direwolf.conf /etc/direwolf.conf
# Editar MYCALL, ADEVICE y coordenadas del beacon antes de arrancar
```

Identificar el dispositivo de audio USB:

```bash
aplay -l | grep -i usb
# Anotar el CARD number y actualizar direwolf.conf:
# ADEVICE plughw:CARD=Device,DEV=0
```

### 3.5 Servicios systemd

```bash
sudo cp meshtastic-socat.service         /etc/systemd/system/
sudo cp direwolf.service                 /etc/systemd/system/
sudo cp minibroker-emergencias.service   /etc/systemd/system/
sudo cp minibroker-aprs.service          /etc/systemd/system/
sudo cp minibroker-usb-watchdog.service  /etc/systemd/system/
sudo cp minibroker-usb-watchdog.timer    /etc/systemd/system/

sudo systemctl daemon-reload

# Habilitar arranque automático
sudo systemctl enable meshtastic-socat.service
sudo systemctl enable direwolf.service
sudo systemctl enable minibroker-emergencias.service
sudo systemctl enable minibroker-aprs.service
sudo systemctl enable minibroker-usb-watchdog.timer
```

---

## 4. Configuración del fichero .env

El fichero `/opt/minibroker/.env-usb` controla toda la configuración del sistema.  
Copia y adapta la plantilla a tu instalación:

```bash
sudo cp env-usb /opt/minibroker/.env-usb
sudo chown meshnet:meshnet /opt/minibroker/.env-usb
sudo chmod 640 /opt/minibroker/.env-usb
```

### 4.1 Parámetros globales del broker

```ini
########################################
# HUB JSONL (escucha suscriptores externos)
########################################
BROKER_HOST=127.0.0.1
BROKER_PORT=8765

# Puerto de control (comandos SEND_TEXT, BROADCAST_TEXT, etc.)
BROKER_CTRL_HOST=127.0.0.1
BROKER_CTRL_PORT=8766

########################################
# TIMING Y RECONEXIÓN
########################################
BROKER_HEARTBEAT_SEC=15          # Latido al hub cada N segundos
BROKER_RECONNECT_MIN_SEC=2       # Espera mínima entre reintentos
BROKER_RECONNECT_MAX_SEC=60      # Espera máxima entre reintentos
BROKER_SENDQ_MAX=500             # Mensajes máximos en cola de envío por nodo

########################################
# GUARDAS 24/7
########################################
BROKER_COOLDOWN_SECS=90              # Pausa tras desconexión
BROKER_FORCE_RECONNECT_GRACE_SEC=2   # Cooldown corto tras reconexión forzada
BROKER_EARLY_DROP_WINDOW=20          # Segundos: sesión "muy corta" → escala cooldown
BROKER_EARLY_DROP_ESCALATE_TO=180    # Cooldown escalado si la sesión cae pronto
BROKER_TX_BLOCK_DURING_COOLDOWN=1    # Bloquear TX mientras cooldown activo
BROKER_NO_HEARTBEAT=0                # 0 = heartbeat activo

########################################
# DATOS Y LOGS
########################################
MINIBROKER_DATA_DIR=/opt/minibroker/data
```

### 4.2 Configuración de nodos

El broker v4.0 soporta hasta N nodos simultáneos de cuatro tipos:

| Tipo | Descripción |
|---|---|
| `meshtastic_serial` | Meshtastic por USB/PTY local |
| `meshtastic_tcp` | Meshtastic por API TCP WiFi |
| `meshcore_serial` | MeshCore por puerto serie |
| `meshcore_tcp` | MeshCore por TCP |

#### Nodo 1 — Meshtastic USB (más común)

```ini
NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=local-usb          # Nombre interno, usado en routing y logs
NODE_1_PORT=/dev/ttyV0          # PTY creado por socat
NODE_1_PRIMARY=1                # Este nodo es el destino por defecto
# NODE_1_BAUD=115200
# NODE_1_DEAD_SILENCE_SEC=25    # Reiniciar si no hay paquetes en N segundos
# NODE_1_LINK_CHECK_SEC=1.0
# NODE_1_COOLDOWN_SECS=90
```

#### Nodo 2 — Meshtastic TCP (nodo remoto con WiFi)

```ini
NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=remoto-wifi
NODE_2_HOST=192.168.1.50
NODE_2_TCP_PORT=4403            # Puerto por defecto del firmware Meshtastic
# NODE_2_TCP_DEAD_SILENCE_SEC=60
```

#### Nodo 3 — MeshCore Serial

```ini
NODE_3_TYPE=meshcore_serial
NODE_3_ALIAS=meshcore-zgz
NODE_3_PORT=/dev/ttyUSB1
NODE_3_BAUD=115200

# Mapa de canales MeshCore → canal lógico del broker
# Formato: broker_ch:tipo:mc_channel_idx:nombre
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC,2:chan:1:ZGZ

# Traducción chan_idx → canal broker
NODE_3_MC_CHANIDX_TO_CH=0:0,1:2

# Mapa pubkey_prefix → canal broker (contactos directos)
# NODE_3_MC_CONTACT_TO_CH=abcdef123456:0

# Alias para mostrar en el prefijo [MC:alias]
# NODE_3_MC_ALIASES=abcdef123456:PEPE
NODE_3_MC_RX_PREFIX_STYLE=alias    # alias | prefix
NODE_3_MC_DEFAULT_CH=0
# NODE_3_MC_SILENCE_RECONNECT_SEC=120
```

#### Nodo 4 — MeshCore TCP

```ini
NODE_4_TYPE=meshcore_tcp
NODE_4_ALIAS=meshcore-remoto
NODE_4_MC_TCP_HOST=192.168.1.60
NODE_4_MC_TCP_PORT=4000             # Puerto por defecto de MeshCore (≠ 4403)
# NODE_4_MC_TCP_DEAD_SILENCE_SEC=60
NODE_4_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_4_MC_CHANIDX_TO_CH=0:0
NODE_4_MC_RX_PREFIX_STYLE=alias
NODE_4_MC_DEFAULT_CH=0
```

> **Compatibilidad v2/v3:** Si no se define `NODE_1_TYPE`, el broker arranca en  
> modo legacy usando `MESH_CONN_MODE` / `MESH_USB_PORT` / `MESH_TCP_HOST`.  
> Instalaciones antiguas no necesitan cambios.

### 4.3 Routing entre nodos

Sin reglas de routing los mensajes solo se publican en el hub JSONL. Para que los mensajes crucen de un nodo a otro hay que definir las rutas explícitamente:

```ini
########################################
# ROUTING ENTRE NODOS
# Formato: src:alias-origen:canal,dst:alias-destino:canal
########################################

# Ejemplo: bridge bidireccional local-usb ↔ remoto-wifi en canal 0
NODE_ROUTE_1=src:local-usb:0,dst:remoto-wifi:0
NODE_ROUTE_2=src:remoto-wifi:0,dst:local-usb:0

# Ejemplo: MeshCore ↔ Meshtastic
NODE_ROUTE_3=src:meshcore-zgz:0,dst:local-usb:0
NODE_ROUTE_4=src:local-usb:0,dst:meshcore-zgz:0

# Ejemplo: canal 2 de MeshCore → canal 0 de Meshtastic (cambio de canal)
NODE_ROUTE_5=src:meshcore-zgz:2,dst:local-usb:0
```

**Reglas del router:**
- El alias debe coincidir exactamente con `NODE_N_ALIAS`.
- El router evita el eco (no envía de vuelta al nodo origen).
- La cola de envío persiste si el nodo destino está caído; los mensajes se reenvían al reconectar.

### 4.4 Configuración APRS

```ini
########################################
# APRS CONFIG
########################################

# Activar/desactivar la pasarela completa
APRS_GATE_ENABLED=1

# KISS — Dire Wolf
KISS_HOST=127.0.0.1
KISS_PORT=8100

# Indicativo propio (SSID -10 recomendado para digipeaters/gateways)
APRS_CALL=EA2XXX-10

# Ruta APRS (digipeater path)
APRS_PATH=WIDE1-1,WIDE2-1

# APRS-IS (dejar vacío para deshabilitar)
APRSIS_USER=EA2XXX
APRSIS_PASSCODE=12345               # Calcular en aprs.do/apps/passcode/
APRSIS_HOST=rotate.aprs2.net
APRSIS_PORT=14580
APRSIS_FILTER=                      # Filtro APRS-IS (vacío = sin filtro)

# Prefijos de emergencia reconocidos (en mayúsculas, sin corchetes)
EMERGENCY_PREFIXES=EMER,SOS,ALERTA

# Canales Mesh permitidos para emitir alertas por APRS (vacío = todos)
APRS_EMERGENCY_ALLOWED_CHANNELS=0,1,2

# Longitud mínima del cuerpo del mensaje para ser considerado emergencia
APRS_EMERGENCY_MIN_BODY_LEN=3

# Incluir número de canal Mesh en el mensaje APRS saliente
APRS_EMERGENCY_INCLUDE_CHANNEL=1

# Indicativos APRS autorizados para pasar al Mesh (vacío = todos)
APRS_ALLOWED_SOURCES=

# Intervalo mínimo entre transmisiones RF (segundos)
APRS_TX_MIN_INTERVAL_SEC=15

# Canal Mesh por defecto cuando no hay etiqueta [CHx] en el mensaje APRS
MESHTASTIC_CH=0

# NodeId Meshtastic para envío de eco unicast (vacío = sin eco)
HOME_NODE_ID=
```

### 4.5 APRS inbound → nodos Mesh

```ini
########################################
# APRS INBOUND → MESH
########################################

# Alias de nodos que recibirán los mensajes APRS entrantes.
# Formato: alias separados por coma, igual que NODE_N_ALIAS.
# Vacío → solo el nodo PRIMARY recibe el mensaje.
APRS_INBOUND_NODE_ALIASES=local-usb

# Si 1: mensajes APRS con prefijo [EMER]/[SOS]/[ALERTA] se inyectan
# a Mesh aunque no lleven etiqueta [CHx]. Se usa MESHTASTIC_CH.
# Si 0: sin [CHx] el mensaje se descarta.
APRS_INBOUND_EMERGENCY_NO_CHTAG=1
```

---

## 5. Servicios systemd

### Dependencias y orden de arranque

```
meshtastic-socat.service
        ↓
minibroker-emergencias.service
        ↓
direwolf.service
        ↓
minibroker-aprs.service
```

### Contenido de los servicios

#### meshtastic-socat.service

Crea el puente USB → PTY virtual que permite al broker Meshtastic leer `/dev/ttyV0` aunque el dispositivo real sea `/dev/ttyUSB0`:

```ini
[Unit]
Description=Socat bridge Meshtastic USB -> PTY
After=local-fs.target
Wants=local-fs.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
ExecStartPre=/bin/sleep 2
ExecStart=/usr/bin/socat -d -d -d \
    PTY,link=/dev/ttyV0,raw,echo=0,waitslave,mode=660,group=dialout \
    FILE:/dev/ttyUSB0,raw,echo=0,b115200,nonblock
Restart=always
RestartSec=2
KillMode=process
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
```

> Si el nodo Meshtastic está conectado en un puerto distinto a `/dev/ttyUSB0`,  
> cambiar la ruta `FILE:/dev/ttyUSBx` en `ExecStart`.

#### minibroker-emergencias.service

```ini
[Unit]
Description=MiniBroker Emergencias USB 24x7
After=network-online.target meshtastic-socat.service
Wants=network-online.target
Requires=meshtastic-socat.service

[Service]
Type=simple
User=meshnet
Group=meshnet
WorkingDirectory=/opt/minibroker
EnvironmentFile=/opt/minibroker/.env-usb
ExecStart=/opt/minibroker/venv/bin/python /opt/minibroker/emergency_broker.py \
    --mesh-port /dev/ttyV0 \
    --bind 127.0.0.1 \
    --port 8765 \
    --ctrl-host 127.0.0.1 \
    --ctrl-port 8766 \
    --data-dir /opt/minibroker/data
Restart=always
RestartSec=5
TimeoutStopSec=20
StartLimitBurst=0
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

#### direwolf.service

```ini
[Unit]
Description=Dire Wolf APRS TNC
After=network.target sound.target

[Service]
Type=simple
User=meshnet
ExecStart=/usr/local/bin/direwolf -c /etc/direwolf.conf -t 0
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

#### minibroker-aprs.service

```ini
[Unit]
Description=MiniBroker APRS Bridge
After=network.target direwolf.service minibroker-emergencias.service
Requires=direwolf.service minibroker-emergencias.service

[Service]
Type=simple
User=meshnet
Group=meshnet
WorkingDirectory=/opt/minibroker
EnvironmentFile=/opt/minibroker/.env-usb
ExecStart=/opt/minibroker/venv/bin/python /opt/minibroker/emergency_aprs_bridge.py
Restart=always
RestartSec=3
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
```

### Gestión de servicios

```bash
# Arrancar todos
sudo systemctl start meshtastic-socat
sudo systemctl start minibroker-emergencias
sudo systemctl start direwolf
sudo systemctl start minibroker-usb-watchdog.timer

# Activar APRS (si forma parte del sistema)
sudo systemctl start minibroker-aprs

# Ver estado
sudo systemctl status meshtastic-socat minibroker-emergencias direwolf

# Comprobar APRS (si forma parte del sistema y está activo)
sudo systemctl status minibroker-aprs

# Logs en tiempo real
sudo journalctl -u minibroker-emergencias -f
sudo journalctl -u minibroker-aprs -f
sudo journalctl -u direwolf -f

# Verificación completa tras la activación
systemctl list-units --type=service | grep -E "minibroker|direwolf|socat"

# Validación funcional mínima del broker (tramas que se están recibiendo)
nc 127.0.0.1 8765

# Reiniciar solo el broker (sin tocar socat ni Dire Wolf)
sudo systemctl restart minibroker-emergencias

# Recargar tras cambios en el .env
sudo systemctl daemon-reload
sudo systemctl restart minibroker-emergencias minibroker-aprs
```

---

## 6. Watchdog 24/7

El watchdog se ejecuta cada 60 segundos mediante un timer systemd y realiza las siguientes comprobaciones en orden:

1. **USB físico presente** (`/dev/ttyUSB0`). Si no existe, sale sin hacer nada.
2. **socat activo**. Si no está activo, lo reinicia y marca que el broker también necesita reinicio.
3. **PTY virtual presente** (`/dev/ttyV0`). Si falta, reinicia socat.
4. **Si socat fue reiniciado**, reinicia también el broker para limpiar descriptores de fichero.
5. **Broker activo**. Si está caído, lo levanta.
6. **USB de MeshCore presente** (si `MESHCORE_ENABLE=1`). Si desaparece, reinicia el broker.
7. **Validación del estado lógico**: lee `broker_status.json` y reinicia solo si el fichero está ausente, corrupto o contiene un estado inválido. No reinicia por simple ausencia de tráfico, ya que un nodo TCP puede permanecer sano aunque no haya mensajes durante largos intervalos.

```ini
# minibroker-usb-watchdog.timer
[Timer]
OnBootSec=45           # Primera ejecución 45 s después del arranque
OnUnitActiveSec=60     # Después, cada 60 s

[Install]
WantedBy=timers.target
```

Variables de entorno del watchdog (desde el mismo `.env-usb`):

```ini
MESHCORE_ENABLE=1                      # 1 = vigilar USB de MeshCore
MESHCORE_SERIAL_PORT=/dev/ttyUSB1      # Puerto USB de MeshCore
```

Ver estado del watchdog:

```bash
sudo systemctl status minibroker-usb-watchdog.timer
sudo journalctl -t minibroker-usb-watchdog
```

---

## 7. Dire Wolf — TNC por software

`/etc/direwolf.conf`:

```
MYCALL EA2XXX-10               # Indicativo del gateway APRS

# Dispositivo de audio USB (identificar con: aplay -l)
ADEVICE plughw:CARD=Device,DEV=0

CHANNEL 0
MODEM 1200                     # 1200 baud AFSK (estándar APRS)

# KISS TCP — requerido por la pasarela
KISSPORT 8100

# PTT por VOX (sin GPIO, solo audio)
PTT VOX

# Parámetros CSMA — ajustados para VOX
TXDELAY  30                    # Retardo preámbulo (× 10 ms = 300 ms)
TXTAIL    5                    # Cola post-transmisión (× 10 ms)
SLOTTIME 10                    # Slot CSMA (× 10 ms)
PERSIST  63                    # Persistencia CSMA (0-255, ≈63/256 ≈ 25%)

ARATE 48000                    # Tasa de muestreo

# Beacon de posición (opcional)
PBEACON delay=1 every=30 symbol="R&" \
    lat=41.6488 long=-0.8891 \
    comment="MiniBroker USB APRS"
```

**Ajuste de niveles de audio con alsamixer:**

```bash
alsamixer -c <número_de_tarjeta>
# F6 → seleccionar la tarjeta USB
# Subir "Speaker" (TX) y "Mic" (RX) a ~70%
# Ajustar hasta que Dire Wolf decodifique tramas sin errores CRC
```

**Verificar decodificación:**

```bash
sudo journalctl -u direwolf -f
# Deben aparecer líneas con indicativos decodificados, p.ej.:
# EA5ABC-7>APRS,WIDE1-1:[Mensaje]
```

---

## 8. Casos de uso y flujo de mensajes

### Caso 0. Inclusión de canales en la pasarela y modo de funcinamiento.


### Caso 1 — Nodo A ↔ Nodo B, sin pasarela APRS

**Configuración mínima:**

```ini
NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=local-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1

NODE_2_TYPE=meshtastic_tcp      # o meshcore_serial / meshcore_tcp
NODE_2_ALIAS=remoto-wifi
NODE_2_HOST=192.168.1.50
NODE_2_TCP_PORT=4403

NODE_ROUTE_1=src:local-usb:0,dst:remoto-wifi:0
NODE_ROUTE_2=src:remoto-wifi:0,dst:local-usb:0
```

**Flujo de un mensaje:**

```
Usuario en red de local-usb
        │
        ▼ paquete Meshtastic
MeshtasticSerialManager._on_receive
        │ on_text_event(record)
        ▼
NodeManager._on_text_event
        │ ChannelRouter.destinations("local-usb", ch=0)
        │ → [RouteDestination(alias="remoto-wifi", channel=0)]
        ▼
MeshtasticTcpManager.enqueue_send
        │ _sendq → _do_send → iface.sendText(...)
        ▼
Red de remoto-wifi
```

**Puntos clave:**
- El broker es **agnóstico al contenido**. Un `[EMER]` viaja exactamente igual que "Buenos días".
- Sin regla `NODE_ROUTE_N` el mensaje llega al `JsonlHub` (puerto 8765) pero no cruza a ningún nodo.
- Si el nodo destino está caído, el mensaje queda en `_sendq` y se reenvía al reconectar.

### Caso 2 — Nodo A ↔ Nodo B + pasarela APRS activa

**Configuración adicional al Caso 1:**

```ini
APRS_GATE_ENABLED=1
APRS_CALL=EA2XXX-10
APRS_INBOUND_NODE_ALIASES=local-usb,remoto-wifi
APRS_INBOUND_EMERGENCY_NO_CHTAG=1
```

**Flujos simultáneos e independientes:**

```
Mensaje normal entre nodos:
  Nodo A → ChannelRouter → Nodo B    (el bridge lo lee del Hub y lo descarta)

Mensaje [EMER] de Nodo A:
  Nodo A → ChannelRouter → Nodo B    (routing normal)
              │
              └─ Hub JSONL → bridge → detección [EMER] → RF + APRS-IS

Trama APRS recibida por RF con [CH0]:
  Dire Wolf KISS → bridge → SEND_TEXT alias=local-usb  → Nodo A TX
                          → SEND_TEXT alias=remoto-wifi → Nodo B TX
```

**Puntos clave:**
- El bridge es un **suscriptor independiente** del hub. No interfiere con el routing entre nodos.
- Solo los mensajes con prefijo `[EMER]`, `[SOS]` o `[ALERTA]` salen por RF.
- Los mensajes APRS entrantes llegan a **todos los nodos** listados en `APRS_INBOUND_NODE_ALIASES`.

### Caso 3 — Solo Nodo A + pasarela APRS

**Configuración:**

```ini
NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=local-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1

# Sin NODE_ROUTE_N — no hay segundo nodo

APRS_GATE_ENABLED=1
APRS_CALL=EA2XXX-10
APRS_INBOUND_NODE_ALIASES=local-usb
APRS_INBOUND_EMERGENCY_NO_CHTAG=1
```

**Flujo:**

```
Mensaje normal "Hola":
  Nodo A → Hub → bridge → DESCARTADO (sin prefijo de emergencia)

Mensaje "[EMER] Persona herida en CH 3":
  Nodo A → Hub → bridge → parse [EMER] → RF 144.800 MHz
                                       → APRS-IS (si configurado)

Trama APRS recibida "[CH0] EA5ABC-7: Activación emergencia":
  RF → Dire Wolf KISS → bridge → extract [CH0] → SEND_TEXT local-usb CH0
                                               → Nodo A retransmite en Mesh

Trama APRS recibida "[EMER] Herido sin [CHx]":
  RF → bridge → no hay [CHx] → APRS_INBOUND_EMERGENCY_NO_CHTAG=1
             → usar MESHTASTIC_CH=0 → SEND_TEXT local-usb CH0
             → Nodo A retransmite en Mesh
```

### Caso 4 — Nodo A + pasarela APRS (detalle completo de todos los eventos)

| # | Evento | Flujo | Resultado |
|---|--------|-------|-----------|
| ① | Mensaje normal desde Mesh | Nodo A → Hub → bridge | Bridge descarta (sin prefijo) |
| ② | `[EMER] xyz` desde Mesh | Nodo A → Hub → bridge → RF | Emite por RF y APRS-IS |
| ③ | Trama APRS RF con `[CH0]` | RF → KISS → bridge → ctrl:8766 | Nodo A TX en Mesh CH0 |
| ④ | Trama APRS RF `[EMER]` sin `[CHx]` | RF → KISS → bridge → fallback CH0 | Nodo A TX emergencia CH0 |
| ⑤ | Trama APRS con `HOME_NODE_ID` configurado | RF → bridge → SEND_TEXT dest=home_id | Eco unicast al nodo fijo |
| ⑥ | Trama desde APRS-IS con `[CH0]` | APRS-IS → bridge → ctrl:8766 | Nodo A TX en Mesh CH0 |

**Advertencia sobre bucles:**

Si el Nodo A retransmite un `[EMER]` recibido por APRS, el broker lo publica en el hub y el bridge podría volver a emitirlo por RF. El sistema tiene dos defensas:

1. `_dedup_seen` con TTL (evita procesar la misma trama dos veces).
2. Filtro anti-eco: descarta tramas cuyo `src` == `APRS_CALL` o `APRSIS_USER`.

Para máxima seguridad en despliegues críticos: dejar `HOME_NODE_ID` vacío si no se necesita el eco unicast.

---

## 9. Mensajes de emergencia: comportamiento detallado

### 9.1 Formato de los prefijos

Los mensajes de emergencia se identifican por prefijos entre corchetes al inicio del texto:

```
[EMER] Descripción de la emergencia
[SOS] Descripción
[ALERTA] Descripción
```

El prefijo puede ir en mayúsculas o minúsculas. Los prefijos reconocidos se configuran en:

```ini
EMERGENCY_PREFIXES=EMER,SOS,ALERTA
```

### 9.2 Flujo saliente: Mesh → APRS RF

```
Texto Mesh: "[EMER] Persona herida en el monte, coordenadas 41.64N 0.89W"
                │
                ▼ parse_emergency_mesh_text
         tag=EMER, body="Persona herida en el monte..."
                │
                ▼ format_mesh_emergency_for_aprs
         "EMER CH0 [local-usb] Persona herida en el monte..."
                │
                ▼ _send_outbound_aprs (dedup + throttle)
                │
         ┌──────┴──────┐
         │             │
         ▼             ▼
    KISS TX       APRS-IS TCP
    (RF local)   (red global)
```

**Reglas de filtrado saliente:**
- El canal del mensaje debe estar en `APRS_EMERGENCY_ALLOWED_CHANNELS` (vacío = todos).
- Entre transmisiones RF debe haber al menos `APRS_TX_MIN_INTERVAL_SEC` segundos.
- Se aplica deduplicación para evitar retransmitir el mismo mensaje varias veces.

### 9.3 Flujo entrante: APRS RF → Mesh

```
Trama APRS recibida por RF:
"EA5ABC-7>APRS,WIDE1-1,WIDE2-1:>[CH0] Activación grupo emergencias"
        │
        ▼ parse_ax25_ui (KISS TCP desde Dire Wolf)
        │
        ▼ _handle_aprs_packet
        │
        ├─ Filtro origen (APRS_ALLOWED_SOURCES)
        ├─ Anti-eco (src == APRS_CALL → descartar)
        ├─ Dedup inbound (TTL 30 s)
        │
        ▼ extract_channel_tag
        │
        ├─ [CH0] encontrado → ch=0, stripped="Activación grupo emergencias"
        │
        │   (si no hay [CHx] y APRS_INBOUND_EMERGENCY_NO_CHTAG=1)
        └─ prefijo [EMER]/[SOS]/[ALERTA] → ch=MESHTASTIC_CH
        │
        ▼ add_aprs_src_prefix
        "EA5ABC-7: Activación grupo emergencias"
        │
        ▼ _dispatch_to_nodes
        │
        ├─ SEND_TEXT alias=local-usb  → enqueue → Nodo A TX CH0
        └─ SEND_TEXT alias=remoto-wifi → enqueue → Nodo B TX CH0
```

### 9.4 Envío con retardo (coordinar activaciones)

El texto APRS puede incluir un retardo en minutos con la etiqueta `[CHx+Nm]`:

```
[CH0+5] Activar protocolo en 5 minutos
```

Esto programa el envío a Mesh 5 minutos después de recibir la trama. Útil para coordinar activaciones simultáneas desde diferentes estaciones APRS.

---

## 10. Puerto de control — comandos JSONL

El `ControlServer` escucha en `BROKER_CTRL_HOST:BROKER_CTRL_PORT` (por defecto `127.0.0.1:8766`). Acepta un objeto JSON por línea y devuelve la respuesta.

### Enviar un mensaje a un nodo concreto

```bash
echo '{"cmd":"SEND_TEXT","params":{"text":"Prueba desde terminal","ch":0,"alias":"local-usb"}}' \
    | nc 127.0.0.1 8766
```

### Enviar a todos los nodos (broadcast)

```bash
echo '{"cmd":"BROADCAST_TEXT","params":{"text":"[EMER] Alerta general","ch":0}}' \
    | nc 127.0.0.1 8766
```

### Consultar estado del broker

```bash
echo '{"cmd":"BROKER_STATUS"}' | nc 127.0.0.1 8766
```

### Listar nodos y su estado

```bash
echo '{"cmd":"NODE_LIST"}' | nc 127.0.0.1 8766
```

### Estado de un nodo concreto

```bash
echo '{"cmd":"NODE_STATUS","params":{"alias":"local-usb"}}' | nc 127.0.0.1 8766
```

### Forzar reconexión de un nodo

```bash
echo '{"cmd":"FORCE_RECONNECT","params":{"alias":"remoto-wifi","grace_window_s":2}}' \
    | nc 127.0.0.1 8766
```

### Ver reglas de routing activas

```bash
echo '{"cmd":"ROUTE_LIST"}' | nc 127.0.0.1 8766
```

### Obtener backlog de mensajes

```bash
# Últimos 50 mensajes de texto de todos los nodos
echo '{"cmd":"FETCH_BACKLOG","params":{"limit":50,"portnum":"TEXT_MESSAGE_APP"}}' \
    | nc 127.0.0.1 8766

# Solo del nodo local-usb
echo '{"cmd":"FETCH_BACKLOG","params":{"limit":20,"node_id":"local-usb"}}' \
    | nc 127.0.0.1 8766
```

### Respuestas de ejemplo

```json
// BROKER_STATUS
{
  "ok": true,
  "uptime_sec": 3600,
  "nodes": {
    "local-usb": {"status": "running", "packets_rx": 142, "sendq": 0},
    "remoto-wifi": {"status": "cooldown", "cooldown_remaining": 45}
  }
}

// SEND_TEXT
{"ok": true, "queued": true}

// Error
{"ok": false, "error": "node not found: nodo-inexistente"}
```

---

## 11. Operación 24/7 — procedimientos

### Primer arranque

```bash
# 1. Verificar que el nodo Meshtastic está conectado
ls -l /dev/ttyUSB0

# 2. Arrancar socat primero
sudo systemctl start meshtastic-socat
sleep 3
ls -l /dev/ttyV0    # debe aparecer el symlink

# 3. Arrancar el broker
sudo systemctl start minibroker-emergencias
sleep 5

# 4. Verificar que el broker está vivo
echo '{"cmd":"PING"}' | nc 127.0.0.1 8766

# 5. Arrancar Dire Wolf
sudo systemctl start direwolf
# Verificar que no hay errores de audio
sudo journalctl -u direwolf -n 20

# 6. Arrancar el bridge APRS
sudo systemctl start minibroker-aprs

# 7. Activar el watchdog timer
sudo systemctl start minibroker-usb-watchdog.timer

# 8. Verificar estado global
sudo systemctl status meshtastic-socat minibroker-emergencias direwolf minibroker-aprs
```

### Verificación de operación correcta

```bash
# Logs del broker (ver conexiones y mensajes)
sudo journalctl -u minibroker-emergencias -f

# Líneas esperadas al arrancar:
# [broker] v4.0 arrancado — 2 nodo(s) hub=127.0.0.1:8765 ctrl=127.0.0.1:8766
# [mesh-serial][local-usb] conectado a /dev/ttyV0
# [mesh-tcp][remoto-wifi] conectado a 192.168.1.50:4403

# Logs del bridge APRS
sudo journalctl -u minibroker-aprs -f

# Líneas esperadas:
# [aprs-rf] KISS conectado
# [broker->aprs] conectado al hub 127.0.0.1:8765

# Probar envío desde terminal
echo '{"cmd":"SEND_TEXT","params":{"text":"Test 24/7","ch":0}}' | nc 127.0.0.1 8766
```

### Monitorización continua

```bash
# Script de monitorización rápida (ejecutar como cron cada 5 min)
#!/bin/bash
STATUS=$(echo '{"cmd":"BROKER_STATUS"}' | nc -w 2 127.0.0.1 8766 2>/dev/null)
if [ -z "$STATUS" ]; then
    logger -t minibroker-monitor "ALERTA: broker no responde"
    systemctl restart minibroker-emergencias
fi
```

### Rotación de logs

```bash
# /etc/logrotate.d/minibroker
/opt/minibroker/data/*.jsonl {
    daily
    rotate 7
    compress
    missingok
    notifempty
    postrotate
        systemctl reload minibroker-emergencias 2>/dev/null || true
    endscript
}
```

### Actualización de código

```bash
# Parar los servicios que usan el código
sudo systemctl stop minibroker-aprs minibroker-emergencias

# Actualizar los archivos
sudo cp emergency_broker.py      /opt/minibroker/
sudo cp emergency_aprs_bridge.py /opt/minibroker/
sudo chown meshnet:meshnet /opt/minibroker/*.py

# Reiniciar
sudo systemctl start minibroker-emergencias
sleep 5
sudo systemctl start minibroker-aprs

# Verificar
echo '{"cmd":"PING"}' | nc 127.0.0.1 8766
```

---

## 12. Resolución de problemas

### El broker no conecta al nodo Meshtastic serial

```bash
# Verificar que existe el dispositivo físico
ls -l /dev/ttyUSB0

# Verificar que socat está activo y el PTY existe
systemctl status meshtastic-socat
ls -l /dev/ttyV0

# Verificar permisos
ls -l /dev/ttyV0   # debe tener grupo dialout
id meshnet         # debe estar en dialout

# Reiniciar socat manualmente
sudo systemctl restart meshtastic-socat
sleep 3
sudo systemctl restart minibroker-emergencias
```

### El broker no conecta al nodo Meshtastic TCP

```bash
# Verificar conectividad
ping 192.168.1.50
nc -z -w 3 192.168.1.50 4403 && echo "OK" || echo "NO CONECTA"

# Ver logs de reconexión
sudo journalctl -u minibroker-emergencias | grep "remoto-wifi"
# Buscar: "cooldown", "reconnect", "connected"
```

### Dire Wolf no decodifica tramas

```bash
# Ver si recibe audio
sudo journalctl -u direwolf -f
# Debe mostrar "Audio input level" y decodificaciones

# Ajustar nivel de entrada
alsamixer
# Subir "Mic" o "Capture" de la tarjeta USB

# Verificar identificador correcto de la tarjeta
arecord -l | grep -i usb
# Actualizar ADEVICE en /etc/direwolf.conf
```

### La pasarela APRS no emite por RF

```bash
# Verificar que el bridge está conectado al broker y a Dire Wolf
sudo journalctl -u minibroker-aprs -f
# Buscar: "[aprs-rf] KISS conectado" y "[broker->aprs] conectado"

# Enviar una emergencia de prueba manualmente
echo '{"cmd":"SEND_TEXT","params":{"text":"[EMER] TEST 73","ch":0}}' | nc 127.0.0.1 8766
# El bridge debe detectarla y emitirla

# Verificar que Dire Wolf recibe el frame KISS
sudo journalctl -u direwolf | grep "Xmit"
```

### Mensajes APRS entrantes no llegan a Mesh

```bash
# Verificar que el bridge recibe tramas KISS
sudo journalctl -u minibroker-aprs | grep "aprs-rf->mesh"

# Verificar que el texto lleva [CHx] o prefijo de emergencia
# Líneas de log esperadas:
# [aprs-rf->mesh] CH0 nodo=local-usb <- EA5ABC-7: OK Mensaje...

# Si el mensaje tiene prefijo de emergencia pero no [CHx]:
# Verificar APRS_INBOUND_EMERGENCY_NO_CHTAG=1

# Verificar que el alias en APRS_INBOUND_NODE_ALIASES coincide con NODE_N_ALIAS
echo '{"cmd":"NODE_LIST"}' | nc 127.0.0.1 8766
```

### Broker en estado zombie (proceso activo pero sin tráfico)

```bash
# El watchdog lo detecta por broker_status.json
cat /opt/minibroker/data/broker_status.json | python3 -m json.tool

# Forzar reinicio manual
sudo systemctl restart minibroker-emergencias

# Verificar que el watchdog timer está activo
systemctl list-timers | grep watchdog
```

---

## 13. Referencia rápida de variables de entorno

### Broker global

| Variable | Defecto | Descripción |
|---|---|---|
| `BROKER_HOST` | `127.0.0.1` | IP de escucha del hub JSONL |
| `BROKER_PORT` | `8765` | Puerto hub JSONL |
| `BROKER_CTRL_HOST` | `127.0.0.1` | IP de escucha del control |
| `BROKER_CTRL_PORT` | `8766` | Puerto de control |
| `BROKER_HEARTBEAT_SEC` | `15` | Intervalo de latido |
| `BROKER_RECONNECT_MIN_SEC` | `2` | Backoff mínimo |
| `BROKER_RECONNECT_MAX_SEC` | `60` | Backoff máximo |
| `BROKER_COOLDOWN_SECS` | `90` | Pausa tras desconexión |
| `BROKER_TX_BLOCK_DURING_COOLDOWN` | `1` | Bloquear TX en cooldown |
| `MINIBROKER_DATA_DIR` | `./data` | Directorio de datos y logs |

### Nodos

| Variable | Descripción |
|---|---|
| `NODE_N_TYPE` | `meshtastic_serial` \| `meshtastic_tcp` \| `meshcore_serial` \| `meshcore_tcp` |
| `NODE_N_ALIAS` | Nombre interno del nodo (usado en routing) |
| `NODE_N_PORT` | Puerto serie (`/dev/ttyV0`) |
| `NODE_N_HOST` | IP del nodo TCP |
| `NODE_N_TCP_PORT` | Puerto TCP Meshtastic (4403) |
| `NODE_N_PRIMARY` | `1` = nodo por defecto para SEND_TEXT sin alias |
| `NODE_N_MC_TCP_HOST` | IP del nodo MeshCore TCP |
| `NODE_N_MC_TCP_PORT` | Puerto MeshCore TCP (4000) |
| `NODE_N_MC_CHANNEL_MAP` | Mapa de canales MeshCore |
| `NODE_ROUTE_N` | Regla de routing `src:alias:ch,dst:alias:ch` |

### APRS

| Variable | Defecto | Descripción |
|---|---|---|
| `APRS_GATE_ENABLED` | `1` | Activar pasarela |
| `APRS_CALL` | — | Indicativo APRS propio (ej. `EA2XXX-10`) |
| `APRS_PATH` | `WIDE1-1,WIDE2-1` | Ruta digipeater |
| `KISS_HOST` | `127.0.0.1` | IP de Dire Wolf |
| `KISS_PORT` | `8100` | Puerto KISS |
| `APRSIS_USER` | — | Indicativo APRS-IS |
| `APRSIS_PASSCODE` | — | Passcode APRS-IS |
| `APRSIS_HOST` | `rotate.aprs2.net` | Servidor APRS-IS |
| `EMERGENCY_PREFIXES` | `EMER,SOS,ALERTA` | Prefijos de emergencia |
| `APRS_EMERGENCY_ALLOWED_CHANNELS` | vacío | Canales Mesh permitidos para salida RF |
| `APRS_TX_MIN_INTERVAL_SEC` | `0.8` | Intervalo mínimo entre TX |
| `APRS_ALLOWED_SOURCES` | vacío | Indicativos APRS autorizados (vacío = todos) |
| `MESHTASTIC_CH` | `0` | Canal Mesh por defecto |
| `HOME_NODE_ID` | vacío | NodeId para eco unicast (vacío = sin eco) |
| `APRS_INBOUND_NODE_ALIASES` | vacío | Nodos que reciben APRS entrante |
| `APRS_INBOUND_EMERGENCY_NO_CHTAG` | `1` | Inyectar emergencias sin `[CHx]` |

### Watchdog

| Variable | Defecto | Descripción |
|---|---|---|
| `MESHCORE_ENABLE` | `1` | Vigilar USB de MeshCore |
| `MESHCORE_SERIAL_PORT` | `/dev/ttyUSB1` | Puerto USB de MeshCore |

---

*Documento generado a partir del código fuente del proyecto MiniBroker v4.0.*  
*Última actualización: sincronizada con `emergency_broker.py` y `emergency_aprs_bridge.py` v4.0.*
