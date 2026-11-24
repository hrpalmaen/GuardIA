# Plan de despliegue de GuardIA en la cuenta de pruebas de AWS

Fecha: 2026-11-23 (Fase B detallada el 2026-11-24) · Estado: **borrador para revisión del equipo** (no hay código todavía)

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
| P5 | **La persona de turno se configura con una variable de entorno con su correo**, porque cambia cada 15 días. El correo sirve como identificador de Teams (UPN) para la mención. El valor **no va en el repositorio**: se define al desplegar. La tabla `OnCallSchedule` queda para cuando exista un calendario | 2026-11-23 |
| P6 | **Un solo canal de Teams** para alertas y resumen diario: los dos secretos de webhook guardan la misma dirección, así que separarlos después no requiere código | 2026-11-23 |
| P7 | **Sin correo de respaldo: Teams es el único canal** (como ya lo implementa `SendEscalation`). Si Teams falla 3 veces, se registra `NOTIFICATION_FAILED`, la Lambda falla y la alarma de la DLQ avisa. Aplica también al resumen diario. SES deja de ser necesario; el heartbeat y las alarmas avisan por correo con una suscripción de SNS | 2026-11-24 |
| P8 | **`STUCK_RUN` siempre escala**, igual que `MISSED_RUN`: cancelar o relanzar una ejecución que sigue corriendo no es seguro sin hablar con el dueño | 2026-11-24 |
| P9 | **`DailySummary` (7.2 y 7.3) queda fuera de la Fase B**: no bloquea las pruebas de punta a punta | 2026-11-24 |
| P10 | Responsables de los PR de la Fase B según la tabla de la sección B6 | 2026-11-24 |
| P11 | **El agente de IA entra ahora y no al final:** la solución debe ser agéntica. Sigue la arquitectura inicial: **el agente recomienda y la política determinista decide**. El agente investiga con herramientas de solo lectura, nunca ejecuta acciones (12.2) y no tiene permisos sobre la infraestructura. Ficha T8 en `tareas-fase-b-equipo.md` | 2026-11-24 |
| P12 | **Azure OpenAI en las pruebas** (`analytics`, `env=dev`) con un despliegue de GPT-4.1 mini. La cuenta de Azure ya cuenta con la aprobación de privacidad de la compañía, así que en el demo no hace falta otra aprobación para enviarle la causa saneada. El endpoint, el despliegue y la llave van en el secreto `guardia/agent-azure-openai`, nunca en el repositorio ni en el chat | 2026-11-24 |
| P13 | **Workshop en otra cuenta de AWS con Amazon Bedrock:** entorno nuevo `env=workshop`, con simulador y `DESTROY`. El modelo se elige entre los habilitados en esa cuenta | 2026-11-24 |

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
│ Data · Flow · Detection · Observability                     │
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
| ~~SES: verificar remitente y destinatarios~~ | — | **Ya no hace falta** (P7). Las identidades ya verificadas pueden quedarse o borrarse; no hay que verificar el correo del respaldo |
| Suscribir el correo del equipo al tópico SNS de alarmas (heartbeat y DLQ). SNS envía un correo de confirmación que hay que aceptar | Equipo | Después de desplegar `GuardIA-Observability` |

**Nota sobre las credenciales de AWS (revisión del 2026-11-23, solo lectura):**

- **Ninguno de los perfiles de WSL es la cuenta de producción**, que es la cuenta donde corren los procesos según los datos originales.
- **El perfil `default`** (usuario `om-s3`) **no tiene permisos** ni para leer stacks de CloudFormation, así que `cdk bootstrap` y `cdk deploy` fallarían con él.
- **El perfil `analytics`** es otra cuenta, en `us-east-1`. Ya tiene **CDK bootstrap hecho** (stack `CDKToolkit`, versión 32) y no tiene procesos `GCRAW`.
- **Decisión:** la cuenta de pruebas es `analytics` (P1).

### 5.1 Persona de turno por variable de entorno (decisión P5)

| Variable | Obligatoria | Uso |
|---|---|---|
| `GUARDIA_ONCALL_EMAIL` | Sí | La persona de turno: mención en Teams (el correo es su UPN) |
| `GUARDIA_ONCALL_NAME` | No | El nombre que se muestra en la mención. Si falta, se toma de la parte del correo antes de la `@` |
| `GUARDIA_ONCALL_BACKUP_EMAIL` | No | El respaldo. Si falta, no hay respaldo y queda la advertencia de configuración del requisito 7.4 |

- **De dónde sale el valor:** quien despliega lo pone en su `.env` local, que ya está en `.gitignore`, o en la línea de comandos. CDK lo pasa como variable de entorno a la Lambda `ResolveOnCall`. Si falta `GUARDIA_ONCALL_EMAIL`, el despliegue falla en `cdk synth` en vez de desplegar un GuardIA que no puede avisar a nadie.
- **Cambio cada 15 días:** se edita el `.env` y se corre `cdk deploy GuardIA-Flow` (ahí vive `ResolveOnCall`, sección B3). Tarda menos de un minuto y queda registrado en CloudFormation. Una alternativa que no requiere desplegar es un parámetro de SSM, pero se mantiene la variable de entorno por decisión del equipo.
- **Código:** `ResolveOnCall` usa las variables si están definidas y, si no, la tabla `OnCallSchedule`, así que el código de Laura y sus pruebas siguen valiendo. El resultado tiene la misma forma (`primary`, `backup`, `warnings`), por lo que `SendEscalation` no cambia.
- **Requisito 7.1:** pide un calendario de rotación por fecha. Esta configuración es un calendario de una sola persona a la vez, con rotación manual; queda como decisión D19 en `design.md`.

### Fase B · GuardIA desplegable (es lo que va a producción)

#### B1 · Empaquetado de las Lambdas

| Tema | Decisión |
|---|---|
| Dependencias | **Una capa compartida** con `pydantic`, `pydantic_core`, `croniter` y `guardia_common` (11 MB). `python-dateutil` se fija en 2.9 porque la capa va antes que el runtime y botocore exige <3. Cada Lambda lleva solo su carpeta. `boto3` no se empaqueta porque viene en el runtime |
| Construcción | `pip install --platform manylinux2014_aarch64 --only-binary=:all:` con el pip local, para que `pydantic_core` salga compilado para Lambda y no para WSL. Si el pip local falla, CDK usa Docker (imagen oficial de Python 3.12) con el mismo comando, así que Docker no es obligatorio |
| Arquitectura | **arm64** (Graviton): cerca de 20 % más barato, sin cambios de código |
| `GuardiaFunction` | Una construcción de CDK que aplica a todas las Lambdas lo mismo: runtime, capa, logs JSON con retención de 30 días, timeout, DLQ y tags |

#### B2 · Configuración por entorno (D17)

- **Selección:** `cdk deploy -c env=dev --profile analytics`. `infra/config.py` define un `EnvironmentConfig` por entorno con los umbrales que cambian (en dev, recordatorios cada 2 min y la hora límite en horario laboral), si se despliega `GuardIA-Sim` y el umbral `remediation-enabled`.
- **Nombres:** los stacks y los recursos se llaman igual en los dos entornos (`GuardIA-Data`, `guardia/teams-alerts-webhook`…), porque pruebas y producción son cuentas distintas. El entorno queda en la etiqueta `guardia:environment`. `-c env` es obligatorio: sin él, `cdk synth` falla.
- **Qué pasa al borrar el stack (`retain_data`):** en dev, `DESTROY`: `cdk destroy` borra tablas, secretos y capa, porque los datos son simulados; la llave KMS se borra con la espera mínima de AWS, 7 días. En prod, `RETAIN`: la llave, las tablas (auditoría) y los secretos sobreviven a un borrado accidental del stack, y la espera de la llave es de 30 días.
- **Producción arranca en modo solo observación:** `remediation-enabled=false` por defecto; dev lo pone en `true` para probar el redrive contra el simulador.
- **`.env`:** el app de CDK lee variables de entorno, no el archivo. Antes de desplegar: `set -a; source .env; set +a`. Si falta `GUARDIA_ONCALL_EMAIL`, `cdk synth` falla.
- **Secretos:** los webhooks se crean **vacíos** desde CDK y el valor se pone una vez a mano con `aws secretsmanager put-secret-value`, así la dirección nunca pasa por el código.

#### B3 · Stacks y su orden

```
GuardIA-Data ──► GuardIA-Flow ──► GuardIA-Detection
     │                 │                 │
     └─────────────────┴────► GuardIA-Observability
```

| Stack | Contenido | Tareas |
|---|---|---|
| **Data** (ya existe) | Tablas, llave KMS, parámetros de SSM, **los secretos de los webhooks** y la regla append-only de `IncidentEvents`: ninguna Lambda recibe `UpdateItem` ni `DeleteItem` sobre esa tabla | 1.2 (cierra) |
| **Flow** | `GuardIA-Incident-Flow` y sus Lambdas: `ClassifyIncident`, `RemediationPolicy`, `RedriveOrRestart`, `PollExecution`, `ResolveOnCall`, `SendEscalation`, `ResolveOnSuccess`, más la regla de `SUCCEEDED` | 5.1, 5.4, 5.5, 6.4, 6.5 |
| **Detection** | `DiscoverProcesses` (cada 15 min), `MissedRunWatchdog` (cada 5 min) y `OpenIncident` con sus dos reglas (`STATUS_CHANGE_PATTERN` y `WATCHDOG_PATTERN`), con reintentos y DLQ | 2.2, 3.2 |
| **Observability** | Tópico SNS de alarmas con suscripción de correo, alarmas de las DLQ y de errores de las Lambdas, heartbeat y tablero | 8.1–8.3 |

El stack `GuardIA-Escalation` del primer borrador queda dentro de `GuardIA-Flow`, para evitar dependencias circulares. Detection depende de Flow porque `OpenIncident` arranca la máquina de estados.

**Permisos sobre los procesos de Genesys:** solo `DescribeExecution`, `GetExecutionHistory`, `ListExecutions`, `RedriveExecution` y `StartExecution`, limitados por **patrón de ARN** (`stateMachine:BI-*-GCRAW-*` y sus ejecuciones) y, donde Step Functions lo permita, por la etiqueta (`guardia:monitor=true`; `guardia:remediation=auto` para redrive y relanzamiento). Falta confirmar en la documentación qué acciones aceptan la condición por etiqueta. GuardIA solo escribe en sus propias tablas.

#### B4 · La máquina de estados `GuardIA-Incident-Flow`

Reemplaza al `IncidentFlow` provisional del demo de Laura, que sirve como especificación.

```
ClassifyIncident
   └─► RemediationPolicy (Lambda; función pura en guardia_common)
         ├─ recover ─► Wait (Retry-After, si aplica) ─► RedriveOrRestart
         │               └─► Wait 3 min ─► PollExecution ─┬─ SUCCEEDED ─► RESOLVED
         │                                                 └─ falló ─► (otro intento o) escalate
         ├─ observe ─► escalate         (modo solo observación: remediation-enabled=false)
         └─ escalate ─► ResolveOnCall ─► SendEscalation (solo Teams, P7)
                          └─► bucle de recordatorios:
                                waitForTaskToken (timeout = intervalo de recordatorio)
                                  ├─ ResolveOnSuccess envía el token ─► RESOLVED
                                  └─ timeout ─► ¿pasó la hora límite?
                                        ├─ no ─► recordatorio ─► vuelve a esperar
                                        └─ sí ─► SLA_BREACHED ─► espera hasta 6 h sin recordatorios
```

- **Reglas de `RemediationPolicy`:** `unknown` sin agente → escala; `MISSED_RUN` y `STUCK_RUN` → escala (P8); `escalate-only` o `remediation-enabled=false` → escala; si quedan 30 min o menos → recupera y escala en paralelo; máximo 2 intentos.
- **Recordatorios con token:** cada espera genera un token nuevo que se guarda en `Incidents`, y `ResolveOnSuccess` usa el último. Si alguna vez usa uno viejo, igual marca el incidente como resuelto y la siguiente vuelta del bucle lo ve. Es la forma más simple de tener recordatorios y cierre automático sin estados en paralelo.
- **Si Teams falla:** `SendEscalation` registra `NOTIFICATION_FAILED` y falla; el estado lo captura (`Catch`), deja el incidente así y termina con error, lo que dispara la alarma de errores de la máquina de estados.
- **Definición:** con las construcciones de Step Functions de CDK en Python, para que los ARNs se conecten solos y las pruebas de `synth` validen la estructura. Antes de desplegar se valida con `ValidateStateMachineDefinition` (solo lectura).

#### B5 · Código nuevo o ajustes

| Cambio | Dónde |
|---|---|
| `RemediationPolicy` como función pura (sale de `decide` del demo) y su Lambda | `guardia_common/policy.py`, `lambdas/remediation_policy/` |
| `STUCK_RUN` → `requires_escalation` por regla, igual que `MISSED_RUN` (P8) | `guardia_common/classification.py` |
| `ResolveOnCall` lee las variables de entorno y, si no están, usa la tabla (D19) | `lambdas/resolve_on_call/` |
| `SendEscalation`: guardar el token y el modo recordatorio. **Sin correo de respaldo** (P7) | `lambdas/send_escalation/` |
| `ResolveOnSuccess` (nueva) | `lambdas/resolve_on_success/` |
| Umbral `remediation-enabled` (modo solo observación) | `infra/config.py` |

**Pruebas:** las unitarias con moto siguen igual; se agregan pruebas de `cdk synth` por stack (recursos, permisos, reglas y la definición de la máquina de estados).

#### B6 · Orden de trabajo en PRs pequeños

Cada PR se despliega en `analytics` al terminar.

**Estado (2026-11-24):** los PR 1 y 2 y la tarea T1 (esqueleto del flujo, ver `tareas-fase-b-equipo.md`) están desplegados en `analytics`. Prueba manual del PR 2: un evento `MISSED_RUN` publicado a mano abrió el incidente, arrancó `GuardIA-Incident-Flow` y quedó clasificado como `requires_escalation` en unos 4 s; el mismo evento repetido no abrió otro. `DiscoverProcesses` y `MissedRunWatchdog` corren en la nube sin errores (todavía no hay procesos etiquetados: eso llega con `GuardIA-Sim`). El `ErrorCatalog` se carga con `scripts/load_error_catalog.py`.

| # | PR | Qué se puede probar en la nube | Responsable sugerido |
|---|---|---|---|
| 1 ✅ | Configuración por entorno, capa, `GuardiaFunction` y **primer despliegue de `GuardIA-Data`** (confirma los permisos del usuario de `analytics`) | Tablas y secretos creados | Hellen |
| 2 ✅ | `GuardIA-Flow` mínimo (solo `ClassifyIncident`) y `GuardIA-Detection` completo | Eventos de prueba enviados a mano a EventBridge | Hellen |
| 3 | **Fase C en paralelo:** `GuardIA-Sim` | La noche real en la nube, en Nivel 0 | Por definir |
| 4 | `RemediationPolicy`, regla de `STUCK_RUN` y la rama de recuperación | Redrive real contra Sim | Daniela |
| 5 | Rama de escalamiento: turno, Teams, recordatorios, `SLA_BREACHED` y `ResolveOnSuccess` | Tarjetas en el canal real | Laura |
| 6 | `GuardIA-Observability` | Alarmas, heartbeat y correo de SNS | Por definir |

**Reparto del trabajo:** cada PR pendiente tiene su ficha detallada, lista para trabajarla con Claude, en [`tareas-fase-b-equipo.md`](tareas-fase-b-equipo.md). Además hay dos tareas cortas nuevas: T0 (pendientes de repositorio) y T1 (el esqueleto del flujo, que permite hacer los PR 4, 5 y 6 en paralelo).

**Fuera de la Fase B (P9):** `DailySummary` y su envío (7.2 y 7.3), la agrupación (6.6) y el agente (4.2 y 4.3).

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
| EMT12 atascado | `STUCK_RUN` y escalamiento directo, sin redrive (P8) |
| Teams caído | 3 intentos, `NOTIFICATION_FAILED`, mensaje en la DLQ y correo de la alarma por SNS (no hay correo de respaldo, P7) |
| Hora límite vencida y poco tiempo restante | `SLA_BREACHED` y escalamiento paralelo |

El reloj de la nube es real y no se puede comprimir la noche. Por eso, en pruebas, la hora límite (`deadline-local`) y los crons se configuran para que cada escenario dure minutos y corra en horario laboral. Un script lanza cada escenario y verifica en DynamoDB el estado y el historial del incidente.

### Fase E · Paso a producción

- **Qué cambia:** no se despliega `GuardIA-Sim`, los dueños etiquetan sus procesos reales y se configuran el webhook y el calendario reales.
- **Qué hay que confirmar antes con los dueños:** los crons reales (H8), que el redrive es seguro (`guardia:remediation=auto`) y quién recibe hoy las alertas por SNS (H9).
- **Salida gradual:** primero unos días en **modo solo observación**, en el que GuardIA abre incidentes y avisa pero no hace redrive. Después se activa el redrive automático, proceso por proceso. Esto requiere un umbral `remediation-enabled` en SSM.

## 6. IA (adelantada a la Fase B por P11; el detalle está en la ficha T8)

- **Principio (arquitectura inicial):** el agente recomienda y la política determinista decide. El agente investiga con herramientas de solo lectura, nunca ejecuta acciones y no tiene permisos sobre la infraestructura (12.2, D24).
- **Interfaz:** `AgentPort` es un ciclo de herramientas que no depende del proveedor. La respuesta final es una llamada a `submit_decision`, validada como `AgentDecision`. Una respuesta inválida, más de 30 s, una acción fuera de la lista o poca confianza llevan a escalar el incidente.
- **Adaptadores:**
  - **Azure OpenAI** (GPT-4.1 mini) en `dev`/`analytics` (P12).
  - **Amazon Bedrock** (API Converse) en el workshop (P13).
- **Azure, resuelto:**
  - **Datos:** la cuenta ya tiene la aprobación de privacidad (P12). Igual se envía lo mínimo y saneado: nunca las entradas ni las salidas de los pasos.
  - **Red:** las Lambdas sin VPC ya tienen salida a internet.
  - **Credenciales:** el secreto `guardia/agent-azure-openai`, que se carga desde el `.env` con `scripts/load_agent_secret.py`.
- **Bedrock, pendiente:** en la cuenta del workshop hay que hacer `cdk bootstrap` y habilitar el acceso al modelo.

## 7. Riesgos

| Riesgo | Mitigación |
|---|---|
| El agente se equivoca o es manipulado por un texto dentro de un log (*prompt injection*) | Solo recomienda: su sugerencia pasa por todas las reglas de la política (etiqueta, intentos, ventana, espera y modo solo observación), las herramientas son de solo lectura y no eligen recursos, y la salida se valida contra un contrato cerrado |
| Azure no responde o se acaba la cuota | `InvokeAgent` nunca rompe el flujo: registra `AGENT_FAILED` y el incidente se escala sin diagnóstico de IA |
| El simulador no se comporta igual que los procesos reales en algún detalle no visible en el historial | Los supuestos quedan anotados (sección 4). El modo solo observación en producción lo detecta antes de remediar |
| Alguien despliega `GuardIA-Sim` en producción | El stack se niega a desplegarse con `env=prod` y las etiquetas lo identifican |
| Teams caído durante un incidente: sin correo de respaldo (P7), nadie recibe la tarjeta | La alarma llega al equipo por correo de SNS; el incidente queda en `NOTIFICATION_FAILED` y se puede reenviar |
| Menciones de Teams a una cuenta sin celular | Confirmar que el usuario de pruebas es una persona real con Teams móvil |
| Olvidar cambiar la persona de turno a los 15 días | Agregar al resumen diario quién está de turno y desde cuándo, para que se note si no se actualizó |
