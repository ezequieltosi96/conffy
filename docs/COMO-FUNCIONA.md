# Cómo funciona conffy?

Este documento es para entender conffy de punta a punta y poder explicarlo sin
mirar el código: qué hace cada pieza, por qué la elegimos, qué medimos y qué
pasa cuando algo se rompe.

---

## 1. El problema

Nerdearla tiene más de 30 charlas en inglés este año, muchas en simultáneo. La
transcripción en vivo se hace hoy con herramientas comerciales: son caras,
dependen de alguien operándolas a mano y no se pueden llevar fácilmente a otro
evento. Y casi todas las conferencias tienen el mismo problema.

**conffy** es la respuesta open source: toma el audio de cada escenario y le
da a cada persona del público subtítulos en vivo en su celular, en el idioma
original o traducidos. Corre 100% local en una Mac y está diseñado para
crecer a muchos escenarios en paralelo.

---

## 2. La foto grande

Pensalo como una redacción que trabaja en vivo, con tres roles:

- **El taquígrafo (Whisper)** escucha y escribe lo que se dice.
- **El traductor (Gemma)** toma cada frase terminada y la pasa al otro idioma.
- **El cartero (la API)** reparte cada frase a todos los que la están
  esperando, sin importar si son 10 o 10.000.

Entre ellos hay un **pizarrón compartido (Valkey)**: el taquígrafo y el
traductor escriben ahí, y el cartero lee de ahí. Nunca se hablan directamente.

```
audio del escenario
      │
      ▼
 ┌─────────── worker (uno por charla) ───────────┐
 │ ffmpeg → detector de voz → Whisper → Gemma    │
 └───────────────────────┬───────────────────────┘
                         ▼
                 Valkey (pizarrón)
                         │
                         ▼
          API (reparte) → nginx (portero)
                         │
                         ▼
           celulares del público / overlay de OBS
```

---

## 3. Las piezas, una por una

### 3.1 La fuente de audio

Cada charla ("sesión") tiene una fuente, que puede ser de tres tipos:

- **Un archivo** (`samples/charla.mp3`). Se lee "a velocidad real", como si
  la charla estuviera pasando ahora. Sirve para probar y para las demos.
- **Una URL**: cualquier cosa que ffmpeg sepa leer (RTMP, SRT, HLS, HTTP).
- **Un stream de OBS o vMix**, que es el caso real de una conferencia. OBS no
  "espera" a que alguien le pida el audio: lo *empuja* a un servidor. Ese
  servidor es **MediaMTX**, un programa open source que recibe el stream y lo
  deja disponible en una URL para que el worker lo lea.

Las sesiones se declaran en `config/sessions.yml`, o se crean por la API.

### 3.2 ffmpeg: el adaptador universal de audio

El audio llega en cualquier formato: mp3, video, un stream de OBS. **ffmpeg**
lo convierte siempre a lo mismo: audio crudo (PCM), 16 kHz, mono, 16 bits.
Ese es el formato que esperan el detector de voz y Whisper. Son 32.000 bytes
por segundo de audio.

Para los archivos le pasamos la opción `-re`, que significa "leé a velocidad
real". Sin ella, ffmpeg se leería una charla de una hora en segundos.

### 3.3 El detector de voz (VAD, con Silero)

Es un modelo chiquito que mira el audio en pedacitos de 32 ms y dice "acá hay
voz" o "acá hay silencio". Hace dos trabajos:

1. **Corta el audio en frases.** Whisper no escucha de forma continua:
   transcribe pedazos. Entonces hay que decidir dónde cortar, y el mejor lugar
   es donde el orador hace una pausa. Las reglas de corte son:
   - 0,6 s de silencio: fin de frase natural.
   - Si la frase ya pasó los 8 s, se corta en la primera respiración (0,2 s),
     porque hay oradores que casi no hacen pausas.
   - A los 15 s se corta sí o sí.
   - Guarda 0,3 s de audio *antes* de que empiece la voz, para no comerse la
     primera sílaba.
2. **Evita las alucinaciones.** Si a Whisper le pasás silencio, inventa frases
   como "Thank you." o "Gracias por ver el video". El detector hace que casi
   nunca le llegue silencio, y además filtramos esas frases típicas.

Consume ~2% de un núcleo de CPU por charla.

### 3.4 Whisper: el taquígrafo

**Whisper** es el modelo de reconocimiento de voz de OpenAI, con pesos
abiertos y licencia MIT. Lo corremos con **whisper.cpp**, que lo ejecuta usando
la GPU de la Mac. Usamos la variante `large-v3-turbo`, cuantizada, que pesa
unos 500 MB.

Algunos datos clave:

- **Tarda ~0,6 s por frase**, casi sin importar si la frase dura 1 o 6
  segundos. Internamente Whisper siempre procesa una ventana de 30 segundos
  (rellena con silencio lo que falta), así que el costo es casi fijo.
- **Le pasamos un "prompt":** los términos del glosario y las últimas frases
  dichas. Funciona como una pista ("en esta charla se habla de Kubernetes y de
  Goodhart") y mejora mucho el reconocimiento de nombres.
- **Un hallazgo medido:** pedirle la respuesta con timestamps por palabra
  (`verbose_json`) *duplicaba* el tiempo. Como solo usamos el texto, le pedimos
  `json` simple y pasamos de 1,15 s a 0,6 s por frase.

### 3.5 Parciales y finales: cómo se ve "en vivo"

Si esperáramos a que termine cada frase para mostrar algo, la pantalla
quedaría quieta varios segundos. Por eso hay dos tipos de subtítulo:

- **Parcial:** mientras el orador sigue hablando, cada tanto se transcribe lo
  que va de la frase y se muestra en gris e itálica. Puede cambiar.
- **Final:** cuando la frase termina, se transcribe completa. Se muestra en
  blanco y **nunca más cambia**.

Cada frase tiene un número (`seq`). La regla es simple: el parcial número 7 se
reemplaza por el final número 7. La web muestra todos los finales más, como
mucho, un parcial.

Los parciales cuestan lo mismo que un final, así que están limitados: se hacen
como mucho cada `2 × lo que tardó el último pedido a Whisper`. Si el modelo
anda lento, los parciales se espacian solos, y nunca hacen cola: si toca un
final, el final va primero. Cuando el sistema se satura, lo primero que se
pierde es texto provisorio, no frases.

### 3.6 Gemma: el traductor

**Gemma 4** es el modelo abierto de Google, el que sugieren los
organizadores. Lo corremos con **Ollama**, que lo sirve usando la GPU de la Mac.
Usamos `gemma4:e4b`, que es rápido; la variante `12b` es más precisa pero más lenta.

Cómo lo usamos:

- **Traduce solo frases finales.** Traducir parciales haría que el texto
  traducido cambie todo el tiempo, y costaría el doble.
- **Le pasamos las 3 frases anteriores como contexto**, para que entienda de
  qué se habla, con la instrucción de no traducirlas.
- **Tiene su propia cola.** Si Gemma se atrasa, la transcripción no espera.
- **El prompt usa etiquetas** (`<context>`, `<glossary>`, `<text>`) e incluye
  un ejemplo resuelto. Hizo falta porque, en la primera versión, cuando le
  llegaba un fragmento como "ask not", Gemma lo dejaba sin traducir o lo
  "completaba" mezclándolo con la frase siguiente. Con etiquetas explícitas y
  un ejemplo de fragmento traducido tal cual, el problema desapareció.
- **Tarda ~0,6 a 1,2 s por frase.**

### 3.7 El glosario

Es un archivo (`config/glossary.yml`) con dos listas:

- **`keep`**: términos que no se traducen nunca, como "Kubernetes",
  "Nerdearla" o "LLM".
- **`translate`**: traducciones fijas por idioma, como "deploy" → "despliegue".

Cada término sirve para dos cosas: se le pasa a Whisper para que lo
reconozca, y a Gemma para que lo respete. Con una charla real lo vimos en
acción: el orador dijo *Goodhart's Law*, Whisper escuchó "Good Heart's Law" y
Gemma lo tradujo como "la Ley del Buen Corazón". Al agregar el término al
glosario, las dos cosas se corrigieron.

### 3.8 El worker: el que "atiende" una charla

El worker es un proceso que se hace cargo de **una charla a la vez** y
ejecuta todo lo anterior: ffmpeg, detector, Whisper y Gemma. Publica los
resultados en Valkey.

Para no pisarse entre workers usa un **lease**, que funciona como la llave de
un aula:

- Para tomar una charla, el worker intenta quedarse con la llave en Valkey.
  Solo uno puede tenerla.
- La llave vence a los 10 segundos, así que el dueño la renueva cada 3.
- **Si el worker se muere** (se cuelga, se corta la luz, lo matan), deja de
  renovar. A los 10 segundos la llave queda libre y otro worker toma la
  charla. **Continúa la numeración y la línea de tiempo** donde quedó, así que
  el público no ve números repetidos ni saltos para atrás. Lo probamos
  matando workers con `kill -9` a mitad de una charla.
- **Si lo apagás de forma prolija** (un deploy, por ejemplo), devuelve la
  charla al instante para que otro la tome sin esperar los 10 s.
- **Si alguien detiene la charla desde la API**, el worker lo nota en la
  próxima renovación y termina ordenadamente. La escritura del estado es
  atómica, así que el worker nunca "revive" una charla que alguien detuvo.

Para tener más charlas simultáneas, se agregan workers.

### 3.9 Valkey: el pizarrón compartido

**Valkey** es la versión open source (licencia BSD) de Redis. Es una base de
datos en memoria, rapidísima. En conffy es el único punto de contacto entre
workers y API. Guarda cuatro cosas:

- **La configuración de cada charla.**
- **El estado de cada charla**: en vivo, esperando, terminada o con error,
  además de latencias y qué worker la atiende.
- **Los leases** (las llaves).
- **Los subtítulos**, en *streams*. Un stream es como una lista ordenada donde
  solo se agregan cosas al final y cada entrada tiene un ID. Hay un stream por
  charla y por idioma (`cf:s:sala-a:captions:es`).

Está configurado para escribir también a disco (AOF): si se reinicia, no se
pierde nada, o como mucho un segundo. Los streams tienen un tope de tamaño,
así que no crecen para siempre.

### 3.10 La API: el cartero

Es un servidor **FastAPI** (Python). No guarda nada propio: todo lo lee de
Valkey. Por eso se pueden correr varias copias ("réplicas") en paralelo.

Hace cuatro cosas:

- Lista, crea, arranca y detiene charlas.
- **Entrega los subtítulos en vivo** a los navegadores.
- Genera las descargas SRT, VTT y TXT.
- Responde un health check.

**¿Cómo llegan los subtítulos al celular? Por SSE (Server-Sent Events).** Es
una conexión HTTP que queda abierta y por la que el servidor va mandando
eventos. La elegimos en lugar de WebSocket por tres razones:

- Los subtítulos van en una sola dirección, así que no hace falta más.
- Pasa por proxies y nginx sin configuraciones raras.
- **El navegador reconecta solo**, y al reconectar avisa cuál fue el último
  evento que recibió (`Last-Event-ID`). La API le manda exactamente lo que se
  perdió: sin huecos y sin repetidos. Si se corta el wifi del auditorio, el
  celular se pone al día solo.

**El truco para escalar se llama fan-out.** Cada réplica de la API lee cada
stream de Valkey **una sola vez** y reparte en memoria a todos sus
espectadores de esa charla. Es como una radio: la emisora transmite una vez,
no una vez por oyente. Por eso Valkey no se entera de si hay 10 o 10.000
personas mirando.

Cuando alguien entra a mitad de charla, recibe las últimas 20 frases para
tener contexto, y a partir de ahí sigue en vivo. Si un celular no da abasto
(conexión malísima), la API lo desconecta para que no afecte al resto;
el navegador reconecta y retoma.

### 3.11 nginx: el portero

Es la única puerta de entrada (puerto 8080). Hace cuatro cosas:

- Sirve la web.
- Reparte los pedidos entre las réplicas de la API.
- Para los subtítulos en vivo, apaga el buffering (si no, nginx juntaría
  eventos y los mandaría de a tandas) y deja las conexiones abiertas hasta
  una hora.
- Limita la cantidad de pedidos por IP, pero de forma generosa: en un
  auditorio, cientos de celulares salen por la misma IP pública del wifi.

Es la misma topología que tendrías en producción.

### 3.12 La web del público

Es HTML y JavaScript sin frameworks ni compilación: un solo archivo
(`web/index.html`) que cualquiera puede leer.

- **Pantalla 1:** la lista de salas con su estado (se actualiza cada 5 s) y
  botones para elegir idioma.
- **Pantalla 2:** los subtítulos.
  - Los renglones aparecen abajo y los viejos suben y se desvanecen.
  - El renglón en curso se ve en gris.
  - Hay botones A−/A+ para el tamaño de letra, que queda guardado.
  - Cambiar de idioma cambia la URL (`?s=sala-a&lang=es`), así que se puede
    compartir con un QR.
  - Abajo están los links para descargar la transcripción.
  - Cuando la charla termina, aparece un aviso.

### 3.13 El overlay de OBS

`web/overlay.html` es una página con fondo transparente que muestra los dos
últimos renglones con letra blanca y borde negro. En OBS se agrega como
"fuente de navegador" y los subtítulos quedan **quemados en el stream**. Es uno
de los extras del enunciado.

### 3.14 Las descargas (SRT, VTT, TXT)

Como todas las frases finales quedan guardadas con sus tiempos, la transcripción
completa se arma leyendo el stream: no hay que reprocesar audio. El formato
sigue las convenciones de subtítulos (máximo ~42 caracteres por línea y 2
líneas por cartel); las frases largas se parten y el tiempo se reparte. El
mismo mecanismo sirve para subtitular un video ya grabado: se usa el archivo
como fuente y al final se descarga el SRT.

### 3.15 Los modelos corren fuera de Docker, en la Mac

Docker en macOS no puede usar la GPU. Por eso Whisper y Gemma corren
"nativos" en la Mac (con `scripts/run-host.sh`), y los contenedores les hablan
por `host.docker.internal`. En Linux con una GPU NVIDIA, los modelos irían en
contenedores. El código no cambia: solo las URLs.

### 3.16 Los "enchufes": proveedores intercambiables

El worker no sabe qué modelo usa: solo conoce dos interfaces, `Transcriber`
(audio → texto) y `Translator` (texto → texto). Cada proveedor es un
adaptador que implementa una de ellas:

- **`whispercpp`**: Whisper en la Mac.
- **`openai_compat`**: cualquier servidor que hable el protocolo de OpenAI:
  Ollama, vLLM, LM Studio y muchas nubes.
- **`replay`**: modelos *falsos* que devuelven un texto de ejemplo con
  latencias realistas. Sirven para las pruebas de carga sin GPU y para que
  cualquiera pruebe conffy sin descargar modelos.

Cambiar de modelo o de proveedor es cambiar una variable de entorno. Agregar
Gemini, por ejemplo, es escribir un adaptador nuevo sin tocar nada más.

---

## 4. El viaje de una frase, cronometrado

Supongamos que el orador dice *"Kubernetes scales the pods automatically."* y
hace una pausa.

| Tiempo | Qué pasa |
|---|---|
| 0,0 s | El orador termina la frase. Mientras hablaba, ya se venían mostrando parciales en gris. |
| +0,6 s | El detector confirma el silencio y cierra la frase. |
| +1,2 s | Whisper devuelve el texto final. El worker lo publica en Valkey. |
| +1,2 s (+10 ms) | La API lo lee una vez y lo reparte: el inglés ya está en todas las pantallas. |
| +1,2 s | En paralelo, la frase entra a la cola de traducción, con contexto y glosario. |
| +2,2 s | Gemma devuelve *"Kubernetes escala los pods automáticamente."* y se publica. |
| +2,2 s (+10 ms) | El español aparece en todas las pantallas. |

Resumen: **~1 s para el idioma original y ~2 s para la traducción**, contados
desde que el orador hace la pausa.

---

## 5. Qué pasa cuando algo falla

| Si se rompe… | Qué ve el público | Cómo se recupera |
|---|---|---|
| Un worker | Los subtítulos de esa charla se frenan | Otro worker la toma en ≤10 s y sigue donde quedó |
| Una réplica de la API | Se corta la conexión | El navegador reconecta en 2 s a otra réplica y se pone al día sin huecos |
| El wifi del celular | Nada, hasta que vuelve | Al reconectar recibe lo que se perdió |
| Whisper o Gemma | Los subtítulos se frenan | La charla sigue "viva" y los subtítulos vuelven cuando vuelve el modelo |
| Los modelos saturados | Los subtítulos se atrasan; los parciales se espacian primero | Agregar capacidad de GPU |
| Valkey se reinicia | Un corte breve | Recupera todo desde el disco (AOF) |
| La fuente de audio | La charla queda como "Interrumpida" | Se arregla la fuente y se reinicia con `POST /api/sessions/{id}/start` |

---

## 6. Lo que medimos (todo en una Mac mini M4 Pro)

### Una charla

| Qué | Cuánto |
|---|---|
| Whisper por frase | ~0,6 s |
| Gemma por frase | ~0,6–1,2 s |
| Subtítulo en inglés después de la pausa | ~1 s |
| Subtítulo en español después de la pausa | ~2 s |

### 1.500 espectadores (con modelos falsos)

Probamos 15 charlas con 100 espectadores cada una, durante 2 minutos:

- Cero conexiones fallidas o caídas.
- El subtítulo llega del worker al navegador en **8,6 ms** (mitad de los
  casos) y en menos de **20 ms** (95% de los casos).
- Cada réplica de la API usó ~1% de CPU; nginx y Valkey, menos del 2%.
- **Repartir es prácticamente gratis.**

### Cuántas charlas reales aguanta una Mac

| Charlas | Atraso de los subtítulos | Veredicto |
|---|---|---|
| 1 | 1,1 s | holgado |
| 2 | 1,4 s | **cómodo** |
| 3 | 4–5 s | al límite (se prendieron los ventiladores) |
| 4 | 5–9 s y creciendo | saturado |

Whisper y Gemma **comparten la GPU**: con 3 charlas se degradan los dos a la vez.

### Hallazgos que valen contarse

- **`verbose_json`** duplicaba el tiempo de Whisper. Con `json` bajamos de
  1,15 s a 0,6 s por frase.
- **OpenMP en contenedores:** el detector de voz usa una librería
  (ggml + OpenMP) cuyos hilos "giran en el lugar" esperando trabajo. Con
  muchos núcleos disponibles, cada worker quemaba medio núcleo sin hacer
  nada. Limitándolo a un hilo que duerme mientras espera, bajó de ~46% a ~3%
  de CPU por worker: 15 veces menos.
- **Los modelos chicos "sobre-editan" fragmentos.** Lo resolvimos con
  etiquetas y un ejemplo en el prompt.
- **El glosario corrige al mismo tiempo el reconocimiento y la traducción**
  (el caso Goodhart).

---

## 7. Cómo escala

La idea central: **los espectadores son baratos, las charlas son caras.**

- **Más espectadores:** más réplicas de la API. Cada una lee cada charla una
  sola vez. Para 1.500 personas alcanzan 2 réplicas; la tercera es por
  seguridad.
- **Más charlas:** más workers (baratos, ~3% de un núcleo cada uno) y, sobre
  todo, **más GPU** para Whisper y Gemma.

Para las 15 charlas de Nerdearla hay tres caminos:

1. **Local:** unas 8 Mac mini, a razón de 2 charlas por máquina. El audio ya
   está en el lugar.
2. **GPUs de datacenter** con servidores que agrupan pedidos: faster-whisper
   para la transcripción y vLLM para Gemma. Rinden mucho más por GPU que una
   Mac, porque la Mac atiende los pedidos de a uno.
3. **APIs en la nube** (por ejemplo Gemini): sin GPUs propias; se paga por
   minuto de audio.

En producción sería **Kubernetes**:

- la API detrás de un Ingress, con autoescalado
- los workers escalando según la **agenda del evento**: una conferencia sabe
  de antemano cuántas charlas hay a cada hora, y KEDA puede escalar siguiendo
  ese cronograma
- los modelos en un pool de GPUs
- Valkey administrado

**El código es el mismo que en docker compose.** Cambian la configuración y la
infraestructura. El detalle está en `docs/SCALING.md`.

---

## 8. Preguntas que te pueden hacer

**¿Por qué dos modelos y no uno que escuche y traduzca directo?**
Porque cada uno es muy bueno en lo suyo. Whisper es rapidísimo y preciso para
transcribir, pero solo traduce hacia inglés. Gemma traduce bien y respeta
instrucciones como "usá este glosario" o "mirá las frases anteriores". Además,
así el público elige idioma y cada modelo se puede cambiar por separado.

**¿Por qué no WebSocket?**
Los subtítulos van en una sola dirección. SSE es más simple, atraviesa proxies
sin problemas y reconecta solo, retomando desde el último evento.

**¿Por qué Valkey y no Kafka o RabbitMQ?**
Un solo componente liviano nos da cola, historial con reanudación, locks y
estado. Kafka sería un camión para llevar un paquete. Elegimos Valkey en vez
de Redis por su licencia BSD.

**¿Qué pasa con 10.000 espectadores?**
Se agregan réplicas de la API. Valkey no se entera, porque cada réplica lee
cada charla una sola vez. Con 1.500 espectadores, cada réplica usaba ~1% de CPU.

**¿Funciona sin internet?**
Sí. Los modelos corren en la Mac y la web se sirve localmente. Solo la
tipografía viene de Google Fonts, y si no carga se usa una del sistema.

**¿Qué tan preciso es?**
Con una charla real de Nerdearla, la transcripción fue prácticamente
perfecta y la traducción, buena. Los errores típicos son nombres propios, y
se corrigen con el glosario.

**¿Cuánto cuesta?**
Con modelos locales, solo el hardware: una Mac mini cada 2 charlas. Todo el
software es open source.

**¿Se puede usar con Gemini?**
Sí, por diseño: es escribir un adaptador. Está planificado.

**¿Por qué Python?**
El ecosistema de audio y modelos (ffmpeg, detectores de voz, clientes de
modelos) está en Python. La arquitectura es la misma que harías en .NET:
FastAPI equivale a Minimal APIs, cada worker a un BackgroundService, las colas
a Channels y las interfaces de proveedores a `interface`.

**¿Qué no está hecho?**
- Micrófono directo desde el navegador (el contrato ya está definido).
- Panel de producción visual (los datos ya están en la API).
- Proveedor de Gemini.
- Estabilización de parciales para que "salten" menos.

Está todo en `docs/ROADMAP.md`.

---

## 9. Glosario de términos

| Término | Qué significa |
|---|---|
| **ASR** | Automatic Speech Recognition: pasar voz a texto (Whisper). |
| **MT** | Machine Translation: traducción automática (Gemma). |
| **VAD** | Voice Activity Detection: detectar dónde hay voz y dónde silencio. |
| **PCM** | Audio "crudo", sin comprimir: una lista de números que representan la onda. |
| **16 kHz** | 16.000 muestras de audio por segundo, el formato que espera Whisper. |
| **Parcial / final** | Texto provisorio de una frase en curso / texto definitivo de una frase terminada. |
| **seq** | El número de frase; un parcial N se reemplaza por el final N. |
| **Stream (de Valkey)** | Una lista ordenada donde solo se agrega al final; cada entrada tiene un ID. |
| **Lease** | Una "llave" con vencimiento que da a un solo worker el derecho de atender una charla. |
| **Failover** | Cuando algo se cae y otra pieza toma su lugar sola. |
| **SSE** | Server-Sent Events: una conexión HTTP abierta por la que el servidor empuja eventos. |
| **Last-Event-ID** | El ID del último evento recibido, que el navegador manda al reconectar para retomar. |
| **Fan-out** | Leer algo una vez y repartirlo a muchos. |
| **Réplica** | Otra copia idéntica de un servicio, para repartir carga o por seguridad. |
| **p50 / p95 / p99** | La mitad / el 95% / el 99% de los casos estuvo por debajo de ese valor. |
| **Lag** | Cuánto atraso llevan los subtítulos respecto del audio. |
| **Prompt** | Las instrucciones y el contexto que se le pasan a un modelo. |
| **Glosario** | Los términos que el sistema tiene que reconocer y respetar. |
| **Overlay** | Una capa transparente que se superpone al video del stream. |
| **RTMP / SRT (protocolo)** | Protocolos que usa OBS para mandar un stream. |
| **SRT / VTT (archivo)** | Formatos de archivo de subtítulos. SRT protocolo y SRT archivo no tienen nada que ver. |
| **Cuantizado** | Un modelo "comprimido" para que ocupe menos y corra más rápido, con poca pérdida de calidad. |