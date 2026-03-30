Se hace repitiendo el mismo patrón por **cada canal que quieras puentear**.

## **Esquema**

Hay tres piezas por cada canal:

### **1\. Mapeo broker/Meshtastic → canal real de MeshCore**

NODE\_4\_MC\_CHANNEL\_MAP=6:chan:2:MESH2CORE,7:chan:3:EMERGENCIA,8:chan:4:LOGISTICA

Formato:

canal\_broker:chan:channel\_idx\_meshcore:ETIQUETA

Ejemplo:

* broker CH6 → MeshCore channel\_idx 2  
* broker CH7 → MeshCore channel\_idx 3  
* broker CH8 → MeshCore channel\_idx 4

---

### **2\. Mapeo canal real de MeshCore → broker/Meshtastic**

NODE\_4\_MC\_CHANIDX\_TO\_CH=2:6,3:7,4:8

Ejemplo:

* MeshCore 2 → broker CH6  
* MeshCore 3 → broker CH7  
* MeshCore 4 → broker CH8

---

### **3\. Rutas entre nodos**

Debes crear una pareja de rutas por cada canal.

## **Ejemplo completo con 3 canales**

### **Objetivo**

* Meshtastic CH6 ⇄ MeshCore canal 2  
* Meshtastic CH7 ⇄ MeshCore canal 3  
* Meshtastic CH8 ⇄ MeshCore canal 4

### **Configuración**

NODE\_4\_MC\_CHANNEL\_MAP=6:chan:2:MESH2CORE,7:chan:3:EMERGENCIA,8:chan:4:LOGISTICA  
NODE\_4\_MC\_CHANIDX\_TO\_CH=2:6,3:7,4:8

NODE\_ROUTE\_1=src:local-usb:6,dst:meshcore-remoto:2  
NODE\_ROUTE\_2=src:meshcore-remoto:6,dst:local-usb:6

NODE\_ROUTE\_3=src:local-usb:7,dst:meshcore-remoto:3  
NODE\_ROUTE\_4=src:meshcore-remoto:7,dst:local-usb:7

NODE\_ROUTE\_5=src:local-usb:8,dst:meshcore-remoto:4  
NODE\_ROUTE\_6=src:meshcore-remoto:8,dst:local-usb:8  
---

## **Regla que debes seguir siempre**

### **Ida: Meshtastic → MeshCore**

En `dst:meshcore-remoto:X` pones el **canal real de MeshCore** al que quieres transmitir.

Ejemplo:

NODE\_ROUTE\_3=src:local-usb:7,dst:meshcore-remoto:3

Eso manda CH7 de Meshtastic al canal real 3 de MeshCore.

### **Vuelta: MeshCore → Meshtastic**

En `src:meshcore-remoto:X` pones el **canal broker ya traducido**, no el canal real MeshCore.

Si has definido:

NODE\_4\_MC\_CHANIDX\_TO\_CH=3:7

entonces la vuelta debe ser:

NODE\_ROUTE\_4=src:meshcore-remoto:7,dst:local-usb:7

No:

src:meshcore-remoto:3

porque al entrar desde MeshCore el broker ya lo habrá convertido a CH7.

---

## **Plantilla general**

Si quieres añadir un canal nuevo:

### **Supón:**

* Meshtastic usa CH`M`  
* MeshCore usa canal real `R`

Entonces:

NODE\_4\_MC\_CHANNEL\_MAP=... ,M:chan:R:NOMBRE  
NODE\_4\_MC\_CHANIDX\_TO\_CH=... ,R:M

NODE\_ROUTE\_X=src:local-usb:M,dst:meshcore-remoto:R  
NODE\_ROUTE\_Y=src:meshcore-remoto:M,dst:local-usb:M  
---

## **Ejemplo rápido**

### **Quieres añadir:**

* Meshtastic CH9  
* MeshCore canal real 5

Añades:

NODE\_4\_MC\_CHANNEL\_MAP=6:chan:2:MESH2CORE,7:chan:3:EMERGENCIA,8:chan:4:LOGISTICA,9:chan:5:SANIDAD  
NODE\_4\_MC\_CHANIDX\_TO\_CH=2:6,3:7,4:8,5:9

NODE\_ROUTE\_7=src:local-usb:9,dst:meshcore-remoto:5  
NODE\_ROUTE\_8=src:meshcore-remoto:9,dst:local-usb:9  
---

## **Método correcto para no equivocarte**

Por cada canal, piensa así:

### **Canal A**

* Meshtastic CH6  
* MeshCore 2

6 \-\> 2  
2 \-\> 6  
local-usb:6 \-\> meshcore-remoto:2  
meshcore-remoto:6 \-\> local-usb:6

### **Canal B**

* Meshtastic CH7  
* MeshCore 3

7 \-\> 3  
3 \-\> 7  
local-usb:7 \-\> meshcore-remoto:3  
meshcore-remoto:7 \-\> local-usb:7

### **Canal C**

* Meshtastic CH8  
* MeshCore 4

8 \-\> 4  
4 \-\> 8  
local-usb:8 \-\> meshcore-remoto:4  
meshcore-remoto:8 \-\> local-usb:8  
---

## **Recomendación práctica**

Mantén siempre una tabla antes de tocar el `.env`:

Meshtastic CH6 \<-\> MeshCore 2  
Meshtastic CH7 \<-\> MeshCore 3  
Meshtastic CH8 \<-\> MeshCore 4  
Meshtastic CH9 \<-\> MeshCore 5

y luego traduces esa tabla a:

* `NODE_4_MC_CHANNEL_MAP`  
* `NODE_4_MC_CHANIDX_TO_CH`  
* dos `NODE_ROUTE` por canal

---

## **Ejemplo final listo para varios canales**

NODE\_4\_MC\_CHANNEL\_MAP=6:chan:2:MESH2CORE,7:chan:3:EMERGENCIA,8:chan:4:LOGISTICA,9:chan:5:SANIDAD  
NODE\_4\_MC\_CHANIDX\_TO\_CH=2:6,3:7,4:8,5:9

NODE\_ROUTE\_1=src:local-usb:6,dst:meshcore-remoto:2  
NODE\_ROUTE\_2=src:meshcore-remoto:6,dst:local-usb:6

NODE\_ROUTE\_3=src:local-usb:7,dst:meshcore-remoto:3  
NODE\_ROUTE\_4=src:meshcore-remoto:7,dst:local-usb:7

NODE\_ROUTE\_5=src:local-usb:8,dst:meshcore-remoto:4  
NODE\_ROUTE\_6=src:meshcore-remoto:8,dst:local-usb:8

NODE\_ROUTE\_7=src:local-usb:9,dst:meshcore-remoto:5  
NODE\_ROUTE\_8=src:meshcore-remoto:9,dst:local-usb:9  
 