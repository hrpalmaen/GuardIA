# Plan de despliegue de GuardIA en la cuenta de pruebas de AWS

Fecha: 2026-11-23 · Estado: **borrador para revisión del equipo** (no hay código todavía)

## 1. Objetivo y restricciones

Llevar GuardIA, que hoy corre en local con moto, a una cuenta real de AWS para probarlo de punta a punta antes de producción.

| Restricción | Consecuencia en el plan |
|---|---|
| Desde la cuenta de pruebas **no se puede acceder a datos productivos** | Los procesos de Genesys se simulan en la nube a partir de los fixtures saneados (`tests/fixtures/executions/`), como en el demo local |
| **No existe un entorno de pruebas** de los pipelines de extracción | GuardIA despliega su propio simulador, `GuardIA-Sim`, que solo existe en pruebas |
| Cuenta de pruebas en **us-east-1** (Norte de Virginia) | Es la misma región de los procesos reales; los ARNs de los fixtures ya son de `us-east-1` |

**Principio:** el código y los stacks de GuardIA son **los mismos** en pruebas y en producción. Lo único que cambia entre los dos es la configuración (SSM y secretos) y la presencia de `GuardIA-Sim`.

## 2. Decisiones tomadas

| # | Decisión | Fecha |
|---|---|---|
| P1 | Cuenta de pruebas: la del perfil de AWS `analytics`, en `us-east-1`. Ya tiene CDK bootstrap, así que se despliega con `--profile analytics` | 2026-11-23 |
| P2 | **Teams real**: se crea un canal nuevo para GuardIA, con webhook de Workflows | 2026-11-23 |
| P3 | **IA después.** La primera versión escala los errores desconocidos directamente a una persona. Cuando se agregue, el agente queda detrás de una interfaz con dos adaptadores posibles: Bedrock o un modelo en Azure (Claude en Azure AI Foundry u OpenAI) | 2026-11-23 |
| P4 | Las máquinas de estado del simulador se **reconstruyen desde los historiales de los fixtures**, sin pedir las definiciones reales a los dueños | 2026-11-23 |
| P5 | **La persona de turno se configura con una variable de entorno con su correo**, porque cambia cada 15 días. El correo sirve como identificador de Teams (UPN) para la mención y como destinatario del correo de respaldo. El valor **no va en el repositorio**: se define al desplegar. La tabla `OnCallSchedule` queda para cuando exista un calendario | 2026-11-23 |
| P6 | **Un solo canal de Teams** para alertas y resumen diario: los dos secretos de webhook guardan la misma dirección, así que separarlos después no requiere código | 2026-11-23 |

## 3. Arquitectura en la cuenta de pruebas

```
┌──────────────── GuardIA-Sim (solo pruebas) ────────────────┐
│ BI-EMT12/EMT22/EMTSAS-GCRAW-Orchestrator-ConversationsDetail│
│   misma forma que las reales, etiquetas guardia:*           │
│   reglas de EventBridge con cron                            │
│   Lambdas de reemplazo: responden o fallan según escenario  │
│ Tabla/parámetro de escenarios  ·  tópico SNS de pruebas     │
└──────────────┬──────────────────────────────────────────────┘
               │ eventos reales de Step Functions
┌──────────────▼──────────── GuardIA (igual que producción) ─┐
│ Data · Detection · Flow · Escalation · Observability        │
└──────────────┬──────────────────────────────────────────────┘
               │ webhook (Secrets Manager)
         Canal de Teams de GuardIA (real)
```

GuardIA no distingue el simulador de los procesos reales: ve ejecuciones reales de AWS, con historial real, y su redrive funciona de verdad. Lo único falso es lo que hacen por dentro las Lambdas de los pasos.

## 4. Máquinas de estado simuladas (decisión P4)

### Lo que muestran los historiales

| Fixture | Recorrido | Cómo falla |
|---|---|---|
| EMTSAS 18-sep | `IngestionControllerTask` → `NotifyIngestionError` → `FailIngestionController` | La Lambda devuelve `Exception` ("500 Server Error después de 3 intentos"; los 3 intentos ocurren dentro de la Lambda, no son reintentos de Step Functions). Un estado `Fail` termina con `IngestionControllerError` |
| EMT12 23-sep | `IngestionControllerTask` → `CheckTasksToProcess` → `FetchRawDataMap` (Map con `FetchRawConversationsTask` → `CheckRecordsProcessed` → `ProcessConversationSplitterTask`, en bucle) → `UpdateCheckpointTask` | Varias iteraciones del Map terminan con `Runtime.OutOfMemory`, el Map falla y `UpdateCheckpointTask` termina con `States.Runtime`, porque falta un JSONPath (defecto H10 del proceso real) |
| EMT22 13-nov y éxitos | El mismo recorrido, con `SkipSplitterNoRecords` cuando no hay registros | Termina en `SUCCEEDED` |

### Cómo se reconstruyen

- **Una definición ASL por organización** con los mismos nombres de estados, el mismo encadenamiento, el Map, las decisiones y la notificación SNS. Se genera con un script a partir del historial, y no se escribe a mano.
- **Las Lambdas de reemplazo**, una por tipo de paso, compartida por las 3 organizaciones:
  - **En el camino normal** devuelven la forma de salida mínima que las decisiones necesitan (hay tareas, hay registros o no hay). El saneamiento quitó el input y el output de los estados, así que los contenidos no se reproducen.
  - **Cuando el escenario lo pide, fallan con el error real:**
    - `Exception` con el mensaje saneado del 500.
    - **Un `Runtime.OutOfMemory` de verdad:** la Lambda de 128 MB reserva memoria hasta agotarla. Así el error lo produce Lambda y no es un texto inventado.
  - **`States.Runtime`** se reproduce con el mismo defecto del JSONPath en `UpdateCheckpointTask`.
- **Escenarios:** se guardan en una tabla `SimScenarios` (o en parámetros de SSM), por ejemplo "la próxima ejecución de EMTSAS falla 1 vez con 500 y después funciona". La Lambda de reemplazo consulta cuántas veces le toca fallar, así que el **redrive** de GuardIA da resultados reales: falla otra vez, o termina bien.
- **Escenarios sin fallas de Lambda:**
  - **MISSED_RUN:** deshabilitar por un rato la regla de cron.
  - **STUCK_RUN:** el paso hace una espera larga.

### Supuestos (no vienen del historial y quedan anotados)

- **Reintentos de Step Functions:** los historiales no muestran que haya `Retry` en los estados. Se asume que no hay, y en los fixtures los reintentos visibles son internos de la Lambda.
- **Concurrencia del Map y timeouts:** valores razonables, porque no afectan lo que GuardIA observa.
- **Crons (H8):** EMT12 cada hora a las :30, EMT22 y EMTSAS a las :00. En pruebas se pueden reprogramar para que los escenarios caigan en horario laboral.

## 5. Fases

### Fase A · Prerrequisitos (sin código)

| Tarea | Responsable | Estado |
|---|---|---|
| Cuenta de pruebas en `us-east-1` | — | ✅ |
| `cdk bootstrap` en la cuenta | Equipo | ✅ Ya existe en la cuenta `analytics` (stack `CDKToolkit`, versión 32) |
| Perfil para desplegar | Equipo | ✅ `analytics`. Falta confirmar en el primer `cdk deploy` que el usuario tiene todos los permisos |
| Crear **un solo canal** de Teams y su flujo de Workflows "cuando se recibe una solicitud de webhook, publicar en un canal". Las alertas y el resumen diario usan el mismo webhook | Equipo | Decidido; falta crearlo |
| Persona de turno y respaldo: los correos van en `GUARDIA_ONCALL_EMAIL` y `GUARDIA_ONCALL_BACKUP_EMAIL` (sección 5.1) | Equipo | ✅ Definidos |
| Confirmar que Docker funciona en los WSL de quien despliega (lo usa el empaquetado de Lambdas) | Equipo | ✅ |
| SES: verificar el remitente (`GUARDIA_SES_SENDER`) y **todos** los destinatarios. La cuenta está en sandbox (máximo 200 correos al día y solo a correos verificados) | Equipo | El remitente y el correo de turno ya están verificados; **falta verificar el correo del respaldo** |

**Nota sobre las credenciales de AWS (revisión del 2026-11-23, solo lectura):**

- **Ninguno de los perfiles de WSL es la cuenta de producción**, que es la cuenta donde corren los procesos según los datos originales.
- **El perfil `default`** (usuario `om-s3`) **no tiene permisos** ni para leer stacks de CloudFormation, así que `cdk bootstrap` y `cdk deploy` fallarían con él.
- **El perfil `analytics`** es otra cuenta, en `us-east-1`. Ya tiene **CDK bootstrap hecho** (stack `CDKToolkit`, versión 32) y no tiene procesos `GCRAW`.
- **Decisión:** la cuenta de pruebas es `analytics` (P1).

**Nota sobre el remitente de SES:**

- **El riesgo:** si el remitente es una dirección `@gmail.com`, SES la puede verificar, pero los correos salen de servidores que no son de Google. No pasan la validación de dominio de Gmail (DMARC) y es probable que lleguen a spam o los rechacen.
- **Para las pruebas:** sirve como remitente una dirección corporativa ya verificada.
- **Para producción:** conviene verificar un dominio o subdominio corporativo en SES, con DKIM.
- **En todo caso:** el correo es solo el respaldo de Teams.
- **Decisión (2026-11-23):** por ahora se mantiene el remitente de Gmail, aceptando el riesgo de que los correos lleguen a spam. En la Fase D se revisa que lleguen; si no llegan, se cambia `GUARDIA_SES_SENDER` sin tocar código.

### 5.1 Persona de turno por variable de entorno (decisión P5)

| Variable | Obligatoria | Uso |
|---|---|---|
| `GUARDIA_ONCALL_EMAIL` | Sí | La persona de turno: mención en Teams (el correo es su UPN) y correo de respaldo |
| `GUARDIA_ONCALL_NAME` | No | El nombre que se muestra en la mención. Si falta, se toma de la parte del correo antes de la `@` |
| `GUARDIA_ONCALL_BACKUP_EMAIL` | No | El respaldo. Si falta, no hay respaldo y queda la advertencia de configuración del requisito 7.4 |
| `GUARDIA_SES_SENDER` | Sí | El remitente del correo de respaldo; debe estar verificado en SES |

- **De dónde sale el valor:** quien despliega lo pone en su `.env` local, que ya está en `.gitignore`, o en la línea de comandos. CDK lo pasa como variable de entorno a la Lambda `ResolveOnCall`. Si falta `GUARDIA_ONCALL_EMAIL`, el despliegue falla en `cdk synth` en vez de desplegar un GuardIA que no puede avisar a nadie.
- **Cambio cada 15 días:** se edita el `.env` y se corre `cdk deploy GuardIA-Escalation`. Tarda menos de un minuto y queda registrado en CloudFormation. Una alternativa que no requiere desplegar es un parámetro de SSM, pero se mantiene la variable de entorno por decisión del equipo.
- **Código:** `ResolveOnCall` usa las variables si están definidas y, si no, la tabla `OnCallSchedule`, así que el código de Laura y sus pruebas siguen valiendo. El resultado tiene la misma forma (`primary`, `backup`, `warnings`), por lo que `SendEscalation` no cambia.
- **Requisito 7.1:** pide un calendario de rotación por fecha. Esta configuración es un calendario de una sola persona a la vez, con rotación manual; queda como decisión D19 en `design.md`.

### Fase B · GuardIA desplegable (es lo que va a producción)

1. **Empaquetado de Lambdas:** una capa compartida con `pydantic`, `croniter` y `guardia_common`, más el código de cada Lambda. Se prueba con `cdk synth`.
2. **Configuración por entorno:** un contexto de CDK `env=dev|prod` que define los prefijos de nombres, los umbrales de SSM, los nombres de los secretos y si se despliega `GuardIA-Sim`. En pruebas se acortan los tiempos (por ejemplo, recordatorios cada 2 min).
3. **Stacks nuevos:**
   - `GuardIA-Detection`: el descubrimiento cada 15 min (2.2), el watchdog cada 5 min y las reglas `STATUS_CHANGE_PATTERN` y `WATCHDOG_PATTERN` hacia `OpenIncident` (3.2).
   - `GuardIA-Flow`: la máquina de estados real `GuardIA-Incident-Flow` (5.5), que reemplaza al `IncidentFlow` provisional del demo, más `RemediationPolicy` (5.1) y la espera por `Retry-After` (5.4).
   - `GuardIA-Escalation`: `ResolveOnSuccess` (6.4), los recordatorios y `SLA_BREACHED` (6.5) y el secreto del webhook.
   - `GuardIA-Observability`: las DLQ, las alarmas de las Lambdas, el heartbeat (8.x) y el tablero.
4. **Permisos mínimos:**
   - **Escritura:** GuardIA solo escribe en sus propias tablas.
   - **Sobre los procesos:** solo `DescribeExecution`, `GetExecutionHistory`, `ListExecutions`, `RedriveExecution` y `StartExecution`, limitados a recursos con la etiqueta `guardia:monitor=true`.
   - **Auditoría:** `IncidentEvents` es de solo agregar (pendiente de 1.2).
5. **Pruebas:** las pruebas unitarias con moto siguen igual y las de `cdk synth` se extienden a cada stack.

### Fase C · Simulador en la nube (solo pruebas)

1. Un script que genera las 3 definiciones ASL a partir de los historiales de los fixtures.
2. El stack `GuardIA-Sim` con las máquinas de estado, sus etiquetas `guardia:*`, las reglas de cron, las Lambdas de reemplazo, la tabla de escenarios y el tópico SNS de pruebas.
3. Un comando para activar escenarios, por ejemplo `python scripts/sim.py falla EMTSAS 500 --veces 1`.
4. **"Teams caído":** el simulador cambia por un rato el secreto del webhook a una dirección inválida y luego lo restaura. GuardIA no se modifica para esto.
5. La persona de turno se toma de `GUARDIA_ONCALL_EMAIL` (sección 5.1); no hay que cargar calendario.
6. **Protección:** el stack `GuardIA-Sim` se niega a desplegarse si `env=prod`.

### Fase D · Pruebas de punta a punta en la cuenta (tareas 9.1 a 9.4)

| Escenario | Qué se verifica |
|---|---|
| EMTSAS falla con 500 y el redrive funciona | Incidente abierto en ≤ 2 min, redrive real, `RESOLVED` sin escalar |
| EMT12 sin memoria | Causa raíz `Runtime.OutOfMemory`, tarjeta en el canal real con mención, recordatorio, cierre automático cuando la siguiente ejecución termina bien |
| EMT22 no arranca | `MISSED_RUN` y escalamiento |
| EMT12 atascado | `STUCK_RUN` |
| Teams caído | 3 intentos, `NOTIFICATION_FAILED` y alarma |
| Hora límite vencida y poco tiempo restante | `SLA_BREACHED` y escalamiento paralelo |

El reloj de la nube es real y no se puede comprimir la noche. Por eso, en pruebas, la hora límite (`deadline-local`) y los crons se configuran para que cada escenario dure minutos y corra en horario laboral. Un script lanza cada escenario y verifica en DynamoDB el estado y el historial del incidente.

### Fase E · Paso a producción

- **Qué cambia:** no se despliega `GuardIA-Sim`, los dueños etiquetan sus procesos reales y se configuran el webhook y el calendario reales.
- **Qué hay que confirmar antes con los dueños:** los crons reales (H8), que el redrive es seguro (`guardia:remediation=auto`) y quién recibe hoy las alertas por SNS (H9).
- **Salida gradual:** primero unos días en **modo solo observación**, en el que GuardIA abre incidentes y avisa pero no hace redrive. Después se activa el redrive automático, proceso por proceso. Esto requiere un umbral `remediation-enabled` en SSM.

## 6. IA (después de la primera versión)

- **Interfaz:** `AgentPort` recibe el contexto ya saneado del incidente y devuelve un `AgentDecision` validado (el contrato de `design.md`). Cualquier respuesta que no cumpla el contrato se rechaza y el incidente se escala.
- **Adaptadores:** `BedrockAgent` y `AzureAgent`. Para la clasificación y el diagnóstico conviene un modelo general (Claude o GPT); los modelos de código como Codex no son los adecuados.
- **Si es Azure, antes hay que resolver:**
  - **Datos:** la aprobación de que la causa del error, ya recortada y sin tokens (H3 y H4), salga de AWS.
  - **Red:** la salida a internet de la Lambda.
  - **Credenciales:** la llave del modelo en Secrets Manager.

## 7. Riesgos

| Riesgo | Mitigación |
|---|---|
| El simulador no se comporta igual que los procesos reales en algún detalle no visible en el historial | Los supuestos quedan anotados (sección 4). El modo solo observación en producción lo detecta antes de remediar |
| Alguien despliega `GuardIA-Sim` en producción | El stack se niega a desplegarse con `env=prod` y las etiquetas lo identifican |
| Menciones de Teams a una cuenta sin celular | Confirmar que el usuario de pruebas es una persona real con Teams móvil |
| Olvidar cambiar la persona de turno a los 15 días | Agregar al resumen diario quién está de turno y desde cuándo, para que se note si no se actualizó |
| SES en sandbox | Verificar destinatarios. El correo es solo respaldo de Teams |
