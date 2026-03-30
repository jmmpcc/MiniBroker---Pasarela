# Guía de Implementación Multi-Nodo (1–4 nodos)

Esta guía complementa **Escenarios\_nodos.md** (escenarios de 2 nodos con rutas manuales) y **Mapeado\_Canales.md** (mapeo de canales MeshCore). Cubre el uso de `NODE_AUTO_BRIDGE`, la toma de decisiones por tipo de nodo y los escenarios de 3 y 4 nodos.

---

# **1. NODE\_AUTO\_BRIDGE: qué es y cuándo usarlo**

## **1.1 El problema previo**

Para conectar N nodos en malla bidireccional hay que definir N×(N-1) reglas `NODE_ROUTE_N`:

| Nodos activos | Reglas manuales necesarias |
|---|---|
| 1 | 0 |
| 2 | 2 |
| 3 | 6 |
| 4 | 12 |

Con cuatro nodos y canales múltiples la configuración manual se vuelve extensa y difícil de mantener.

## **1.2 La solución: NODE\_AUTO\_BRIDGE**

Añadiendo una sola línea al `.env-usb` el broker genera automáticamente todas las rutas bidireccionales entre los nodos activos (aquellos con `NODE_N_TYPE` definido):

```
NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

El log de arranque confirma cuántas reglas se han generado:

```
[router] NODE_AUTO_BRIDGE activo: 3 nodo(s), canales=[0], 6 regla(s) generada(s)
[router] auto-bridge: meshtastic-usb:0 -> meshtastic-tcp:0
[router] auto-bridge: meshtastic-tcp:0 -> meshtastic-usb:0
[router] auto-bridge: meshtastic-usb:0 -> meshcore-tcp:0
...
```

## **1.3 AUTO vs manual: cuándo usar cada uno**

| Situación | Usar |
|---|---|
| Malla simétrica todo-a-todo, mismo canal | `NODE_AUTO_BRIDGE=1` |
| Routing asimétrico (A→B pero no B→A) | `NODE_ROUTE_N` manual |
| Canal diferente en origen y destino (MT CH0 → MC canal nativo 2) | `NODE_ROUTE_N` manual |
| Malla parcial (no todos los nodos se hablan entre sí) | `NODE_ROUTE_N` manual |
| Malla simétrica + alguna excepción | `NODE_AUTO_BRIDGE=1` + reglas manuales adicionales |

Las reglas manuales `NODE_ROUTE_N` y `NODE_AUTO_BRIDGE` son **compatibles**: las manuales se procesan primero y tienen prioridad; el auto-bridge no duplica las reglas ya definidas.

---

# **2. Árbol de decisión: servicios y watchdog por tipo de nodo**

Según los nodos USB presentes en el despliegue, hay que elegir la variante correcta del servicio del broker y activar los flags correspondientes del watchdog.

| Nodos USB en el despliegue | Variante `minibroker-emergencias.service` | Servicios socat a activar | Flags watchdog |
|---|---|---|---|
| Ninguno (solo TCP) | **sin USB** | — | todos los `ENABLE=0` |
| Meshtastic USB ×1 | **Meshtastic USB** | `meshtastic-socat.service` | `MESHTASTIC_ENABLE=1` |
| Meshtastic USB ×2 | **2× Meshtastic USB** | `meshtastic-socat.service` + `meshtastic-socat-2.service` | `MESHTASTIC_ENABLE=1` y `MESHTASTIC_ENABLE_2=1` |
| MeshCore USB ×1 | **MeshCore USB** | `meshcore-socat.service` | `MESHCORE_ENABLE=1` |
| MeshCore USB ×2 | **2× MeshCore USB** | `meshcore-socat.service` + `meshcore-socat-2.service` | `MESHCORE_ENABLE=1` y `MESHCORE_ENABLE_2=1` |
| Meshtastic USB ×1 + MeshCore USB ×1 | **Meshtastic USB + MeshCore USB** | `meshtastic-socat.service` + `meshcore-socat.service` | `MESHTASTIC_ENABLE=1` y `MESHCORE_ENABLE=1` |
| Meshtastic USB ×2 + MeshCore USB ×1 | **2× Meshtastic USB + MeshCore USB** | los tres socat anteriores | `MESHTASTIC_ENABLE=1`, `MESHTASTIC_ENABLE_2=1`, `MESHCORE_ENABLE=1` |
| Meshtastic USB ×1 + MeshCore USB ×2 | **Meshtastic USB + 2× MeshCore USB** | los tres socat anteriores | `MESHTASTIC_ENABLE=1`, `MESHCORE_ENABLE=1`, `MESHCORE_ENABLE_2=1` |
| Meshtastic USB ×2 + MeshCore USB ×2 | **2× Meshtastic + 2× MeshCore USB** | los 4 socat | todos los `ENABLE=1` |

Los nodos TCP no requieren socat ni flags de watchdog: el broker se conecta directamente al host:puerto configurado.

---

# **3. Bloques .env-usb de referencia por tipo de nodo**

Sustituye `X` por el número de nodo (1, 2, 3 o 4). Solo un nodo debe tener `NODE_X_PRIMARY=1`.

## **3.1 meshtastic\_serial** (USB local vía PTY)

```
NODE_X_TYPE=meshtastic_serial
NODE_X_ALIAS=meshtastic-usb        # elige un nombre descriptivo
NODE_X_PORT=/dev/ttyV0             # /dev/ttyV1 si es el segundo Meshtastic USB
NODE_X_PRIMARY=1
NODE_X_BAUD=115200
NODE_X_USB_LOCK=1
NODE_X_DEAD_SILENCE_SEC=25
NODE_X_LINK_CHECK_SEC=1.0
NODE_X_COOLDOWN_SECS=90
```

## **3.2 meshtastic\_tcp** (nodo Meshtastic con WiFi)

```
NODE_X_TYPE=meshtastic_tcp
NODE_X_ALIAS=meshtastic-tcp        # elige un nombre descriptivo
NODE_X_HOST=192.168.X.X
NODE_X_TCP_PORT=4403
NODE_X_DEAD_SILENCE_SEC=25
NODE_X_TCP_DEAD_SILENCE_SEC=60
NODE_X_LINK_CHECK_SEC=1.0
NODE_X_COOLDOWN_SECS=90
```

## **3.3 meshcore\_serial** (MeshCore USB vía PTY)

```
NODE_X_TYPE=meshcore_serial
NODE_X_ALIAS=meshcore-usb          # elige un nombre descriptivo
NODE_X_PORT=/dev/ttyMC0            # /dev/ttyMC1 si es el segundo MeshCore USB
NODE_X_BAUD=115200
NODE_X_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_X_MC_CHANIDX_TO_CH=0:0
NODE_X_MC_RX_PREFIX_STYLE=alias
NODE_X_MC_DEFAULT_CH=0
NODE_X_MC_SILENCE_RECONNECT_SEC=120
NODE_X_DEAD_SILENCE_SEC=25
NODE_X_LINK_CHECK_SEC=1.0
NODE_X_COOLDOWN_SECS=90
```

## **3.4 meshcore\_tcp** (MeshCore remoto vía red)

```
NODE_X_TYPE=meshcore_tcp
NODE_X_ALIAS=meshcore-tcp          # elige un nombre descriptivo
NODE_X_MC_TCP_HOST=192.168.X.X
NODE_X_MC_TCP_PORT=4000
NODE_X_MC_TCP_DEAD_SILENCE_SEC=60
NODE_X_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_X_MC_CHANIDX_TO_CH=0:0
NODE_X_MC_RX_PREFIX_STYLE=alias
NODE_X_MC_DEFAULT_CH=0
NODE_X_MC_SILENCE_RECONNECT_SEC=90
NODE_X_DEAD_SILENCE_SEC=25
NODE_X_LINK_CHECK_SEC=1.0
NODE_X_COOLDOWN_SECS=90
```

---

# **4. NODE\_BRIDGE\_CHANNELS y canales MeshCore**

## **4.1 Concepto clave**

`NODE_BRIDGE_CHANNELS` opera sobre **números de canal del broker**, no sobre índices nativos de MeshCore. El broker hace la traducción en dos capas independientes:

| Dirección | Traducción | Variable |
|---|---|---|
| MeshCore → broker (RX) | `channel_idx` nativo → `MC_CHANIDX_TO_CH[idx]` (o nativo si no hay mapeo) | `NODE_X_MC_CHANIDX_TO_CH` |
| Broker → MeshCore (TX) | canal broker → `MC_CHANNEL_MAP[ch].target` (o nativo si no hay mapeo) | `NODE_X_MC_CHANNEL_MAP` |

## **4.2 Los tres casos**

### **Caso 1: canal nativo 0 ↔ broker 0 (sin mapeo necesario)**

El canal nativo de MeshCore coincide con el canal broker. No hace falta configurar nada especial.

```
NODE_X_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_X_MC_CHANIDX_TO_CH=0:0
NODE_BRIDGE_CHANNELS=0               # o simplemente omitir (default=0)
```

### **Caso 2: canal nativo 1 (ZGZ) ↔ broker 0**

El canal real de MeshCore es el 1, pero queremos que el broker lo trate como canal 0 para hacer puente con Meshtastic CH0.

```
NODE_X_MC_CHANIDX_TO_CH=1:0          # RX: MeshCore idx 1 → broker CH0
NODE_X_MC_CHANNEL_MAP=0:chan:1:ZGZ   # TX: broker CH0 → MeshCore idx 1
NODE_BRIDGE_CHANNELS=0               # auto-bridge en canal broker 0
```

### **Caso 3: múltiples canales con AUTO\_BRIDGE**

```
NODE_X_MC_CHANNEL_MAP=0:chan:0:PUBLIC,1:chan:1:PRIVADO,2:chan:2:EMERGENCIA
NODE_X_MC_CHANIDX_TO_CH=0:0,1:1,2:2
NODE_BRIDGE_CHANNELS=0,1,2
```

Esto genera 3 × N×(N-1) reglas (una por canal y por par de nodos).

## **4.3 Regla de oro**

Para no equivocarse, antes de tocar el `.env` construye una tabla:

```
Canal broker  |  MeshCore idx nativo  |  Etiqueta
      0       |           0           |  PUBLIC
      1       |           1           |  PRIVADO
      2       |           2           |  EMERGENCIA
```

y luego traduce esa tabla a `MC_CHANNEL_MAP` y `MC_CHANIDX_TO_CH`.

---

# **5. Escenarios — 3 nodos**

---

## **Escenario 3A**

## **Nodo 1 = Meshtastic USB + Nodo 2 = Meshtastic TCP + Nodo 3 = MeshCore TCP**

### **Servicio del broker**

Variante: **Meshtastic USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (Meshtastic USB)
After=network-online.target meshtastic-socat.service
Wants=network-online.target
Requires=meshtastic-socat.service

[Service]
Type=simple
User=meshnet
Group=meshnet
WorkingDirectory=/opt/minibroker
EnvironmentFile=/opt/minibroker/.env-usb
ExecStart=/opt/minibroker/venv/bin/python /opt/minibroker/emergency_broker.py --bind 127.0.0.1 --port 8765 --ctrl-host 127.0.0.1 --ctrl-port 8766 --data-dir /opt/minibroker/data
Restart=always
RestartSec=5
TimeoutStopSec=20
StartLimitBurst=0
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### **Servicios socat**

- `meshtastic-socat.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=meshtastic-tcp
NODE_2_HOST=192.168.1.50
NODE_2_TCP_PORT=4403
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_TCP_DEAD_SILENCE_SEC=60
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

Rutas generadas automáticamente (6):
`meshtastic-usb:0 ↔ meshtastic-tcp:0`, `meshtastic-usb:0 ↔ meshcore-tcp:0`, `meshtastic-tcp:0 ↔ meshcore-tcp:0`

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb,meshtastic-tcp
MESHTASTIC_CH=0
```

---

## **Escenario 3B**

## **Nodo 1 = Meshtastic USB + Nodo 2 = MeshCore USB + Nodo 3 = MeshCore TCP**

### **Servicio del broker**

Variante: **Meshtastic USB + MeshCore USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (Meshtastic USB + MeshCore USB)
After=network-online.target meshtastic-socat.service meshcore-socat.service
Wants=network-online.target
Requires=meshtastic-socat.service meshcore-socat.service
```

### **Servicios socat**

- `meshtastic-socat.service`
- `meshcore-socat.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHCORE_ENABLE=1

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshcore_serial
NODE_2_ALIAS=meshcore-usb
NODE_2_PORT=/dev/ttyMC0
NODE_2_BAUD=115200
NODE_2_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_2_MC_CHANIDX_TO_CH=0:0
NODE_2_MC_RX_PREFIX_STYLE=alias
NODE_2_MC_DEFAULT_CH=0
NODE_2_MC_SILENCE_RECONNECT_SEC=120
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb
MESHTASTIC_CH=0
```

---

## **Escenario 3C**

## **Nodo 1 = Meshtastic USB + Nodo 2 = Meshtastic USB (segundo) + Nodo 3 = MeshCore TCP**

### **Servicio del broker**

Variante: **2× Meshtastic USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (2x Meshtastic USB)
After=network-online.target meshtastic-socat.service meshtastic-socat-2.service
Wants=network-online.target
Requires=meshtastic-socat.service meshtastic-socat-2.service
```

### **Servicios socat**

- `meshtastic-socat.service`
- `meshtastic-socat-2.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHTASTIC_ENABLE_2=1
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb-1
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_serial
NODE_2_ALIAS=meshtastic-usb-2
NODE_2_PORT=/dev/ttyV1
NODE_2_BAUD=115200
NODE_2_USB_LOCK=1
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb-1,meshtastic-usb-2
MESHTASTIC_CH=0
```

---

## **Escenario 3D**

## **Nodo 1 = Meshtastic TCP + Nodo 2 = MeshCore USB + Nodo 3 = MeshCore TCP**

### **Servicio del broker**

Variante: **MeshCore USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (MeshCore USB)
After=network-online.target meshcore-socat.service
Wants=network-online.target
Requires=meshcore-socat.service
```

### **Servicios socat**

- `meshcore-socat.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=0
MESHCORE_ENABLE=1

NODE_1_TYPE=meshtastic_tcp
NODE_1_ALIAS=meshtastic-tcp
NODE_1_HOST=192.168.1.50
NODE_1_TCP_PORT=4403
NODE_1_PRIMARY=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_TCP_DEAD_SILENCE_SEC=60
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshcore_serial
NODE_2_ALIAS=meshcore-usb
NODE_2_PORT=/dev/ttyMC0
NODE_2_BAUD=115200
NODE_2_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_2_MC_CHANIDX_TO_CH=0:0
NODE_2_MC_RX_PREFIX_STYLE=alias
NODE_2_MC_DEFAULT_CH=0
NODE_2_MC_SILENCE_RECONNECT_SEC=120
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **Sin APRS** (escenario sin USB Meshtastic)

```
APRS_GATE_ENABLED=0
APRS_INBOUND_NODE_ALIASES=meshtastic-tcp
MESHTASTIC_CH=0
```

---

## **Escenario 3E**

## **Nodo 1 = Meshtastic TCP + Nodo 2 = Meshtastic TCP (segundo) + Nodo 3 = MeshCore TCP**

Despliegue completamente inalámbrico: ningún nodo conectado por USB, todos accesibles por red (WiFi o Ethernet).

### **Servicio del broker**

Variante: **sin USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (sin USB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshnet
Group=meshnet
WorkingDirectory=/opt/minibroker
EnvironmentFile=/opt/minibroker/.env-usb
ExecStart=/opt/minibroker/venv/bin/python /opt/minibroker/emergency_broker.py --bind 127.0.0.1 --port 8765 --ctrl-host 127.0.0.1 --ctrl-port 8766 --data-dir /opt/minibroker/data
Restart=always
RestartSec=5
TimeoutStopSec=20
StartLimitBurst=0
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### **Servicios socat**

- ninguno

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=0
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_tcp
NODE_1_ALIAS=meshtastic-tcp-1
NODE_1_HOST=192.168.1.50
NODE_1_TCP_PORT=4403
NODE_1_PRIMARY=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_TCP_DEAD_SILENCE_SEC=60
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=meshtastic-tcp-2
NODE_2_HOST=192.168.1.51
NODE_2_TCP_PORT=4403
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_TCP_DEAD_SILENCE_SEC=60
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-tcp-1,meshtastic-tcp-2
MESHTASTIC_CH=0
```

---

## **Escenario 3F**

## **Nodo 1 = Meshtastic TCP + Nodo 2 = MeshCore TCP + Nodo 3 = MeshCore TCP (segundo)**

Tres nodos TCP: un Meshtastic WiFi y dos MeshCore remotos (por ejemplo dos repetidores MeshCore en distintas ubicaciones).

### **Servicio del broker**

Variante: **sin USB**
(mismo bloque de servicio que 3E)

### **Servicios socat**

- ninguno

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=0
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_tcp
NODE_1_ALIAS=meshtastic-tcp
NODE_1_HOST=192.168.1.50
NODE_1_TCP_PORT=4403
NODE_1_PRIMARY=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_TCP_DEAD_SILENCE_SEC=60
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshcore_tcp
NODE_2_ALIAS=meshcore-tcp-1
NODE_2_MC_TCP_HOST=192.168.1.60
NODE_2_MC_TCP_PORT=4000
NODE_2_MC_TCP_DEAD_SILENCE_SEC=60
NODE_2_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_2_MC_CHANIDX_TO_CH=0:0
NODE_2_MC_RX_PREFIX_STYLE=alias
NODE_2_MC_DEFAULT_CH=0
NODE_2_MC_SILENCE_RECONNECT_SEC=90
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp-2
NODE_3_MC_TCP_HOST=192.168.1.61
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-tcp
MESHTASTIC_CH=0
```

---

# **6. Escenarios — 4 nodos**

---

## **Escenario 4A**

## **Nodo 1 = Meshtastic USB + Nodo 2 = Meshtastic TCP + Nodo 3 = MeshCore USB + Nodo 4 = MeshCore TCP**

### **Servicio del broker**

Variante: **Meshtastic USB + MeshCore USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (Meshtastic USB + MeshCore USB)
After=network-online.target meshtastic-socat.service meshcore-socat.service
Wants=network-online.target
Requires=meshtastic-socat.service meshcore-socat.service
```

### **Servicios socat**

- `meshtastic-socat.service`
- `meshcore-socat.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHCORE_ENABLE=1

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=meshtastic-tcp
NODE_2_HOST=192.168.1.50
NODE_2_TCP_PORT=4403
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_TCP_DEAD_SILENCE_SEC=60
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_serial
NODE_3_ALIAS=meshcore-usb
NODE_3_PORT=/dev/ttyMC0
NODE_3_BAUD=115200
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=120
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_4_TYPE=meshcore_tcp
NODE_4_ALIAS=meshcore-tcp
NODE_4_MC_TCP_HOST=192.168.1.60
NODE_4_MC_TCP_PORT=4000
NODE_4_MC_TCP_DEAD_SILENCE_SEC=60
NODE_4_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_4_MC_CHANIDX_TO_CH=0:0
NODE_4_MC_RX_PREFIX_STYLE=alias
NODE_4_MC_DEFAULT_CH=0
NODE_4_MC_SILENCE_RECONNECT_SEC=90
NODE_4_DEAD_SILENCE_SEC=25
NODE_4_LINK_CHECK_SEC=1.0
NODE_4_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

Rutas generadas automáticamente (12 = 4×3):
- `meshtastic-usb:0 ↔ meshtastic-tcp:0`
- `meshtastic-usb:0 ↔ meshcore-usb:0`
- `meshtastic-usb:0 ↔ meshcore-tcp:0`
- `meshtastic-tcp:0 ↔ meshcore-usb:0`
- `meshtastic-tcp:0 ↔ meshcore-tcp:0`
- `meshcore-usb:0 ↔ meshcore-tcp:0`

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb,meshtastic-tcp
MESHTASTIC_CH=0
```

---

## **Escenario 4B**

## **Nodo 1 = Meshtastic USB + Nodo 2 = Meshtastic USB (segundo) + Nodo 3 = MeshCore USB + Nodo 4 = MeshCore USB (segundo)**

Configuración con el máximo de nodos USB locales.

### **Servicio del broker**

Variante: **2× Meshtastic USB + 2× MeshCore USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (2x Meshtastic USB + 2x MeshCore USB)
After=network-online.target meshtastic-socat.service meshtastic-socat-2.service meshcore-socat.service meshcore-socat-2.service
Wants=network-online.target
Requires=meshtastic-socat.service meshtastic-socat-2.service meshcore-socat.service meshcore-socat-2.service
```

### **Servicios socat**

- `meshtastic-socat.service`
- `meshtastic-socat-2.service`
- `meshcore-socat.service`
- `meshcore-socat-2.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHTASTIC_ENABLE_2=1
MESHCORE_ENABLE=1
MESHCORE_ENABLE_2=1

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb-1
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_serial
NODE_2_ALIAS=meshtastic-usb-2
NODE_2_PORT=/dev/ttyV1
NODE_2_BAUD=115200
NODE_2_USB_LOCK=1
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_serial
NODE_3_ALIAS=meshcore-usb-1
NODE_3_PORT=/dev/ttyMC0
NODE_3_BAUD=115200
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=120
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_4_TYPE=meshcore_serial
NODE_4_ALIAS=meshcore-usb-2
NODE_4_PORT=/dev/ttyMC1
NODE_4_BAUD=115200
NODE_4_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_4_MC_CHANIDX_TO_CH=0:0
NODE_4_MC_RX_PREFIX_STYLE=alias
NODE_4_MC_DEFAULT_CH=0
NODE_4_MC_SILENCE_RECONNECT_SEC=120
NODE_4_DEAD_SILENCE_SEC=25
NODE_4_LINK_CHECK_SEC=1.0
NODE_4_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb-1,meshtastic-usb-2
MESHTASTIC_CH=0
```

---

## **Escenario 4C**

## **Nodo 1 = Meshtastic TCP + Nodo 2 = Meshtastic TCP (segundo) + Nodo 3 = MeshCore TCP + Nodo 4 = MeshCore TCP (segundo)**

Despliegue completamente inalámbrico con 4 nodos TCP: dos redes Meshtastic WiFi y dos redes MeshCore remotas, sin ningún USB físico en el servidor broker.

### **Servicio del broker**

Variante: **sin USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (sin USB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshnet
Group=meshnet
WorkingDirectory=/opt/minibroker
EnvironmentFile=/opt/minibroker/.env-usb
ExecStart=/opt/minibroker/venv/bin/python /opt/minibroker/emergency_broker.py --bind 127.0.0.1 --port 8765 --ctrl-host 127.0.0.1 --ctrl-port 8766 --data-dir /opt/minibroker/data
Restart=always
RestartSec=5
TimeoutStopSec=20
StartLimitBurst=0
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### **Servicios socat**

- ninguno

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=0
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_tcp
NODE_1_ALIAS=meshtastic-tcp-1
NODE_1_HOST=192.168.1.50
NODE_1_TCP_PORT=4403
NODE_1_PRIMARY=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_TCP_DEAD_SILENCE_SEC=60
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=meshtastic-tcp-2
NODE_2_HOST=192.168.1.51
NODE_2_TCP_PORT=4403
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_TCP_DEAD_SILENCE_SEC=60
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp-1
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_4_TYPE=meshcore_tcp
NODE_4_ALIAS=meshcore-tcp-2
NODE_4_MC_TCP_HOST=192.168.1.61
NODE_4_MC_TCP_PORT=4000
NODE_4_MC_TCP_DEAD_SILENCE_SEC=60
NODE_4_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_4_MC_CHANIDX_TO_CH=0:0
NODE_4_MC_RX_PREFIX_STYLE=alias
NODE_4_MC_DEFAULT_CH=0
NODE_4_MC_SILENCE_RECONNECT_SEC=90
NODE_4_DEAD_SILENCE_SEC=25
NODE_4_LINK_CHECK_SEC=1.0
NODE_4_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

Rutas generadas automáticamente (12 = 4×3):
- `meshtastic-tcp-1:0 ↔ meshtastic-tcp-2:0`
- `meshtastic-tcp-1:0 ↔ meshcore-tcp-1:0`
- `meshtastic-tcp-1:0 ↔ meshcore-tcp-2:0`
- `meshtastic-tcp-2:0 ↔ meshcore-tcp-1:0`
- `meshtastic-tcp-2:0 ↔ meshcore-tcp-2:0`
- `meshcore-tcp-1:0 ↔ meshcore-tcp-2:0`

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-tcp-1,meshtastic-tcp-2
MESHTASTIC_CH=0
```

---

## **Escenario 4D**

## **Nodo 1 = Meshtastic USB + Nodo 2 = Meshtastic TCP + Nodo 3 = MeshCore TCP + Nodo 4 = MeshCore TCP (segundo)**

Un solo nodo USB local (Meshtastic) más tres nodos remotos TCP. Escenario habitual cuando el servidor broker está junto a un radio Meshtastic USB y los demás nodos son WiFi o de red.

### **Servicio del broker**

Variante: **Meshtastic USB**

```
[Unit]
Description=MiniBroker Emergencias 24x7 (Meshtastic USB)
After=network-online.target meshtastic-socat.service
Wants=network-online.target
Requires=meshtastic-socat.service
```

### **Servicios socat**

- `meshtastic-socat.service`

### **`.env-usb`** (sección nodos + routing)

```
MESHTASTIC_ENABLE=1
MESHCORE_ENABLE=0

NODE_1_TYPE=meshtastic_serial
NODE_1_ALIAS=meshtastic-usb
NODE_1_PORT=/dev/ttyV0
NODE_1_PRIMARY=1
NODE_1_BAUD=115200
NODE_1_USB_LOCK=1
NODE_1_DEAD_SILENCE_SEC=25
NODE_1_LINK_CHECK_SEC=1.0
NODE_1_COOLDOWN_SECS=90

NODE_2_TYPE=meshtastic_tcp
NODE_2_ALIAS=meshtastic-tcp
NODE_2_HOST=192.168.1.50
NODE_2_TCP_PORT=4403
NODE_2_DEAD_SILENCE_SEC=25
NODE_2_TCP_DEAD_SILENCE_SEC=60
NODE_2_LINK_CHECK_SEC=1.0
NODE_2_COOLDOWN_SECS=90

NODE_3_TYPE=meshcore_tcp
NODE_3_ALIAS=meshcore-tcp-1
NODE_3_MC_TCP_HOST=192.168.1.60
NODE_3_MC_TCP_PORT=4000
NODE_3_MC_TCP_DEAD_SILENCE_SEC=60
NODE_3_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_3_MC_CHANIDX_TO_CH=0:0
NODE_3_MC_RX_PREFIX_STYLE=alias
NODE_3_MC_DEFAULT_CH=0
NODE_3_MC_SILENCE_RECONNECT_SEC=90
NODE_3_DEAD_SILENCE_SEC=25
NODE_3_LINK_CHECK_SEC=1.0
NODE_3_COOLDOWN_SECS=90

NODE_4_TYPE=meshcore_tcp
NODE_4_ALIAS=meshcore-tcp-2
NODE_4_MC_TCP_HOST=192.168.1.61
NODE_4_MC_TCP_PORT=4000
NODE_4_MC_TCP_DEAD_SILENCE_SEC=60
NODE_4_MC_CHANNEL_MAP=0:chan:0:PUBLIC
NODE_4_MC_CHANIDX_TO_CH=0:0
NODE_4_MC_RX_PREFIX_STYLE=alias
NODE_4_MC_DEFAULT_CH=0
NODE_4_MC_SILENCE_RECONNECT_SEC=90
NODE_4_DEAD_SILENCE_SEC=25
NODE_4_LINK_CHECK_SEC=1.0
NODE_4_COOLDOWN_SECS=90

NODE_AUTO_BRIDGE=1
NODE_BRIDGE_CHANNELS=0
```

### **APRS**

```
APRS_GATE_ENABLED=1
APRS_INBOUND_NODE_ALIASES=meshtastic-usb,meshtastic-tcp
MESHTASTIC_CH=0
```

---

# **7. APRS con múltiples nodos**

## **7.1 Qué es APRS\_INBOUND\_NODE\_ALIASES**

Esta variable define qué nodos del broker reciben los mensajes APRS entrantes (procedentes de la red RF o de APRS-IS) y los retransmiten a la malla Mesh.

Acepta una lista de aliases separados por coma, exactamente como están definidos en `NODE_N_ALIAS`.

```
APRS_INBOUND_NODE_ALIASES=meshtastic-usb,meshtastic-tcp
```

Si va vacío, el mensaje se envía al nodo `PRIMARY`.

## **7.2 Solo nodos Meshtastic reciben APRS inbound**

El bridge APRS inyecta mensajes usando la API de Meshtastic. Los nodos MeshCore no tienen esta capacidad. Si incluyes un alias MeshCore en `APRS_INBOUND_NODE_ALIASES`, el broker lo ignora silenciosamente sin producir errores.

## **7.3 Reglas prácticas**

- Con **1 nodo Meshtastic**: `APRS_INBOUND_NODE_ALIASES=alias-meshtastic`
- Con **2 nodos Meshtastic**: `APRS_INBOUND_NODE_ALIASES=alias-mt-1,alias-mt-2`
- Con **solo MeshCore**: `APRS_GATE_ENABLED=0` (APRS no puede funcionar sin Meshtastic)
- Si no hay Meshtastic y se necesita APRS, considera añadir al menos un nodo Meshtastic TCP como gateway dedicado

---

# **8. Reglas rápidas para no equivocarse**

## **Si un nodo es USB (meshtastic\_serial o meshcore\_serial)**

- En `NODE_X_PORT` va el **PTY virtual** (`/dev/ttyV0`, `/dev/ttyMC0`, etc.), no el USB real
- Requiere el servicio socat correspondiente arrancado antes que el broker
- Hay que activar el flag `MESHTASTIC_ENABLE` o `MESHCORE_ENABLE` en el watchdog

## **Si un nodo es TCP (meshtastic\_tcp o meshcore\_tcp)**

- No requiere socat ni flags de watchdog
- El parámetro de host es `NODE_X_HOST` para Meshtastic y `NODE_X_MC_TCP_HOST` para MeshCore
- El parámetro de puerto es `NODE_X_TCP_PORT` (4403 por defecto) para Meshtastic y `NODE_X_MC_TCP_PORT` (4000 por defecto) para MeshCore

## **Si usas NODE\_AUTO\_BRIDGE y hay nodos MeshCore**

- Asegúrate de que el canal broker en `NODE_BRIDGE_CHANNELS` coincide con el canal broker tras la traducción `MC_CHANIDX_TO_CH`
- Sin mapeo explícito, MeshCore usa el canal nativo como canal broker (passthrough)
- Consulta la sección 4 de esta guía o **Mapeado\_Canales.md** para escenarios con canales no alineados

## **Si APRS está activo**

- Debe haber al menos un nodo Meshtastic activo
- `APRS_INBOUND_NODE_ALIASES` solo funciona con nodos Meshtastic
- `MESHTASTIC_CH` define el canal al que se inyectan los mensajes APRS de emergencia sin etiqueta `[CHx]`

## **Si tienes un solo nodo**

- No hace falta `NODE_AUTO_BRIDGE` ni `NODE_ROUTE_N`; el nodo solo publica al hub JSONL
- Numerarlo siempre como `NODE_1`
