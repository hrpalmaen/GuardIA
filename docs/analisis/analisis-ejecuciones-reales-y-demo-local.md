# Análisis — Ejecuciones reales y plan de demo local

**Fecha:** 2026-11-16
**Alcance:** estado del repositorio GuardIA, análisis de las salidas reales de Step Functions (carpeta `step/`, fuera del repo) y plan para construir la solución completa en local antes de desplegar en AWS.

> Este documento registra hallazgos. Las decisiones que cambien el diseño se trasladan a `.kiro/specs/guardia/design.md` y `tasks.md` cuando se aprueben (regla de `structure.md`).

---

## 1. Estado actual del repositorio

La especificación (requisitos, diseño, plan de tareas) está completa. La implementación va en ~15 %.

| Tarea | Estado | Notas |
|---|---|---|
| 1.1 Proyecto CDK | Hecho (parcial) | `infra/app.py` + `DataStack` solo con parámetros SSM |
| 1.2 Tablas DynamoDB | Pendiente | Solo existen en el demo con moto (`scripts/demo_open_and_classify.py`) |
| 1.3 `guardia_common` | Parcial | `models`, `audit`, `logger`, `config`, `schedule` (plazo SLA), `incidents`, `classification`. Falta `AgentDecision`, `policy`, `teams_client` |
| 1.4 `ErrorCatalog` | Hecho | 4 entradas (429, 401/403, 404, TIMED_OUT) + loader idempotente |
| 3.1 `OpenIncident` | Hecho | Idempotente por `execution_arn`; enriquece solo con `DescribeExecution` |
| 4.1 `ClassifyIncident` | Hecho | Regex sobre `kind + error + cause` |
| 2.x, 3.2–3.4, 4.2–4.3, 5.x, 6.x, 7.x, 8.x, 9.x | Pendiente | |

**Puntos fuertes a conservar:** handlers delgados con un `Deps` inyectable (permite cambiar AWS por adaptadores locales sin tocar la lógica), funciones puras en `guardia_common`, auditoría append-only, logger que no registra causas ni secretos.

**Limpieza pendiente:** coexisten `kiro/` y `.kiro/`; difieren en `specs/guardia/tasks.md` y `steering/structure.md`. La fuente de verdad es `.kiro/`.

---

## 2. Datos reales disponibles

Para cada ejecución hay 4 archivos: `estado_final.json` (salida de `DescribeExecution`), `historial_completo.json` (`GetExecutionHistory`), `errores.json` (eventos de falla filtrados) y `states.csv` (entrada/salida de estados).

| Organización | Inicio (UTC) | Inicio (Bogotá) | Estado | Duración | Iteraciones Map |
|---|---|---|---|---|---|
| EMT12 | 2026-11-13 16:30 | 11:30 | SUCCEEDED | 2 min 23 s | 58 |
| EMT22 | 2026-11-13 16:00 | 11:00 | SUCCEEDED | 15 s | 3 |
| EMTSAS | 2026-11-13 16:00 | 11:00 | SUCCEEDED | 53 s | 5 |
| **EMT12** | 2026-09-23 22:30 | 17:30 | **FAILED** | 6 min 56 s | 69 (falla la 32) |
| **EMTSAS** | 2026-09-18 05:00 | **00:00** | **FAILED** | 7 s | 0 |

- El juego `BI-EMT12-..._20261113_163551_*` está vacío (4 archivos de 0 bytes).
- Todo es del proceso **conversations** (`*-GCRAW-Orchestrator-ConversationsDetail`). **No hay datos del proceso master.**
- No hay ejemplos reales de 429, 403, 404, `TIMED_OUT`, `ABORTED`, ejecución perdida ni ejecución atascada.

### 2.1 Estructura de la máquina de estados (igual para las 3 organizaciones)

```
IngestionControllerTask ──(catch)──> NotifyIngestionError (SNS) ──> FailIngestionController
        │
CheckTasksToProcess (Choice)
        │
FetchRawDataMap (Map) ──(catch)──> NotifyMapError (SNS) ──> UpdateCheckpointTask
   └─ FetchRawConversationsTask → CheckRecordsProcessed → ProcessConversationSplitterTask | SkipSplitterNoRecords
        │
UpdateCheckpointTask (Lambda UpdateCheckpointStatus)
```

Lambdas por organización: `<ORG>-GCRAW-IngestionController`, `-FetchRawConversations`, `-ProcessConversationSplitter`, `-UpdateCheckpointStatus`. Tópico SNS de alertas existente: `<ORG>-GCRAW-Alerts`.

Input programado (igual en todas): `{"source":"scheduled","intervalStart":"","intervalEnd":"",...}`. La ejecución de las 16:30 UTC procesa el intervalo 13:00–13:59 UTC.

### 2.2 Falla A — EMT12, 23-sep: OOM oculto tras un error de JSONPath

1. `FetchRawConversationsTask` falla 6 veces con `Runtime.OutOfMemory` ("Runtime exited with error: signal: killed"); hay reintentos internos (143 `TaskScheduled` frente a 139 `TaskStateEntered`).
2. Falla la iteración 32 del Map → `MapStateFailed` → el catch publica en SNS `"Map FAILED: Runtime.OutOfMemory"`.
3. El catch continúa a `UpdateCheckpointTask`, que falla con `States.Runtime`: *"The JSONPath '$.mapResults[0].messagesSent' ... could not be found in the input"*. Es un defecto del ASL: la rama de error no produce `mapResults`.
4. Error final de la ejecución: `States.Runtime`. Causa: **22 KB**, que incluye el input completo con tokens de acceso de Genesys.

### 2.3 Falla B — EMTSAS, 18-sep 00:00 Bogotá: Genesys 500

1. `IngestionControllerTask` falla con `Exception`: *"Falló consulta de hits para [INBOUND] después de 3 intentos: 500 Server Error: Internal Server Error for url: https://api.cac1.pure.cloud/api/v2/analytics/conversations/details/query"*.
2. El catch publica en SNS y pasa a `FailIngestionController`.
3. Error final: `IngestionControllerError`, causa genérica `"IngestionController failed"`.
4. `redriveStatus = NOT_REDRIVABLE` — *"Execution's redrivable period has ended"*.

---

## 3. Hallazgos que impactan el diseño

| # | Hallazgo | Impacto | Propuesta |
|---|---|---|---|
| H1 | **Ninguna falla real coincide con el catálogo actual** (verificado contra `data/error_catalog.json`) | Las dos irían a `unknown` → agente Bedrock. El catálogo se pensó para 429/403/404 y las fallas reales son OOM, 5xx y defecto de ASL | Ampliar el catálogo (ver H6) |
| H2 | **El error final oculta la causa raíz.** `DescribeExecution` devuelve `States.Runtime` o `IngestionControllerError`; la causa real solo está en `GetExecutionHistory` (primer `TaskFailed` / `LambdaFunctionFailed`) | `OpenIncident` clasifica sobre información pobre | Nueva función pura `extract_root_cause(history)` en `guardia_common`; `OpenIncident` guarda `root_error` y `root_cause`, y la clasificación usa ambas |
| H3 | **`cause` de hasta 22 KB** con el input completo | Riesgo de falsos positivos en regex (p. ej. `"totalHits":403` coincidiría con `\b40[13]\b`); costo y fuga de datos hacia Bedrock y Teams | Recortar y sanear la causa antes de clasificar, invocar al agente o publicar; quitar JSON de input embebido |
| H4 | **Secretos en los archivos**: `accessToken`/`accessTokens` de Genesys (hasta 1 120 apariciones por historial) e ID de cuenta AWS | No pueden entrar al repo; además GuardIA los recibirá en producción vía `cause` e historial | Script de saneamiento para fixtures; redacción de tokens en la librería antes de persistir `cause` en `Incidents` o enviarla fuera |
| H5 | **Redrive no siempre aplica** (`NOT_REDRIVABLE` por periodo vencido) | Caso real para el camino alterno de 5.1 | `RedriveOrRestart` consulta `redriveStatus` y relanza con el input original si no es redrivable |
| H6 | **Tipos de error nuevos** | Requieren decisión de negocio | Propuesta abajo |
| H7 | **Historial insuficiente** (1–2 ejecuciones por organización) | No hay mediana de duración | Aplica la regla de 3.3 (50 % de la periodicidad) hasta tener 5 ejecuciones |
| H8 | **Cron real desconocido.** Las horas sugieren ejecuciones a :00 (EMT22, EMTSAS) y :30 (EMT12), posiblemente horarias, no cada 2 h como dice la especificación | `MissedRunWatchdog` necesita el cron real | Confirmar las reglas de EventBridge por organización |
| H9 | **El proceso ya alerta por SNS** (`<ORG>-GCRAW-Alerts`) | No sustituye a GuardIA (no remedia, no escala con mención ni recuerda), pero conviene saber quién lo recibe | Documentar en el runbook; no se modifica (regla de no tocar los procesos) |
| H10 | **Defecto en el ASL del proceso** (H2, falla A) | Fuera del alcance de GuardIA | Reportarlo a los dueños del proceso |

### H6 — Propuesta de entradas nuevas para el catálogo (pendiente de aprobación)

| Patrón | Clasificación | Acción | Justificación |
|---|---|---|---|
| Genesys `5\d\d` / `Server Error` / `Internal Server Error` | `auto_recoverable` | `redrive` | Falla transitoria del proveedor; el proceso ya reintentó 3 veces internamente, un relanzamiento minutos después suele funcionar. Requiere que el proceso sea idempotente (`guardia:remediation=auto`) |
| `Runtime.OutOfMemory` | `requires_escalation` | `escalate` | Con el mismo input volverá a fallar; requiere subir memoria o cambiar el particionado |
| `States.Runtime` + `JSONPath ... could not be found` | `requires_escalation` | `escalate` | Defecto de la definición del proceso |

Orden de evaluación: la clasificación usa primero la causa raíz (H2). En la falla A, la causa raíz es OOM → escala, aunque el error final sea `States.Runtime`.

---

## 4. ¿Es posible una demo con estos datos?

**Sí**, combinando reproducción de datos reales con escenarios sintéticos.

**Reproducción:** un simulador local carga los historiales saneados y:

- Emite el evento `Step Functions Execution Status Change` desplazando las horas a una "noche de demo".
- Responde `DescribeExecution` y `GetExecutionHistory` desde los archivos.
- Cuando GuardIA hace redrive o relanza, responde con una ejecución exitosa real de la misma organización.

**Sintéticos** (para lo que no hay datos): 429 con `Retry-After`, 403, `MISSED_RUN`, `STUCK_RUN`, 3 organizaciones con la misma causa (agrupación), falla a las 05:40 (escalación en paralelo), incidente sin resolver hasta las 06:00 (`SLA_BREACHED`) y proceso master.

### Narrativa propuesta

| Hora (Bogotá) | Evento | Comportamiento esperado de GuardIA |
|---|---|---|
| 00:00 | EMTSAS falla — Genesys 500 (real) | Causa raíz del historial → catálogo 5xx → redrive no aplica → relanza → SUCCEEDED (reproducción) → resuelto en Nivel 1 |
| 02:30 | EMT12 falla — OOM tras `States.Runtime` (real) | Causa raíz OOM → escala; tarjeta en Teams con mención y diagnóstico → recordatorios cada 10 min → el ingeniero relanza → SUCCEEDED → `RESOLVED` en el mismo hilo |
| 03:00 | EMT22 exitoso (real) | Ningún incidente ni notificación (2.2) |
| 03:40 | 429 en master (sintético) | Espera `Retry-After` ≤ 600 s → redrive → resuelto |
| 04:10 | 3 organizaciones con 403 (sintético) | Un único mensaje agrupado, manejo individual |
| 04:30 | Master no arranca (sintético) | `MISSED_RUN` → escala |
| 05:40 | Error desconocido (sintético) | Agente diagnostica; quedan ≤ 30 min → escala en paralelo |
| 06:00 | Incidente sin resolver | `SLA_BREACHED`, alerta a turno y respaldo, cesan los recordatorios |
| 06:15 | — | Resumen matutino en Teams |

---

## 5. Arquitectura local

Principio: **puertos y adaptadores con reloj virtual.** Los requisitos dependen del tiempo (2 min, 10 min, 20 min, 06:00); sin un reloj simulado la demo tardaría horas.

| Componente AWS | Sustituto local |
|---|---|
| DynamoDB, SSM, Secrets Manager, Tagging API | moto en memoria (ya se usa); opcional DynamoDB Local / LocalStack en Docker (Docker 29 disponible) |
| Step Functions de extracción | Simulador de reproducción sobre los fixtures |
| `GuardIA-Incident-Flow` | Fase 1: ejecutor en Python que recorre los mismos estados. Fase 2: el ASL real en Step Functions Local (imagen oficial `amazon/aws-stepfunctions-local`) |
| EventBridge reglas y Scheduler | Bus de eventos en proceso + reloj virtual |
| Bedrock (Nova Pro) | Interfaz `Agent`: implementación de prueba con respuestas de fixtures; adaptador real a Bedrock opcional |
| Teams (webhook de Workflows) | Servidor HTTP local que recibe y muestra las tarjetas; opcional webhook real |
| SES | Mailpit en Docker |
| CloudWatch (EMF, heartbeat) | Salida EMF a stdout + verificación del heartbeat en el reloj virtual |

Los mismos handlers corren en local y en AWS; solo cambian los adaptadores inyectados en `Deps`.

---

## 6. Plan por fases

| Fase | Contenido | Tareas de `tasks.md` |
|---|---|---|
| 0 · Datos | Saneamiento de `step/` → `tests/fixtures/executions/`; `extract_root_cause`; recorte y redacción de causa; catálogo ampliado (H6); limpiar `kiro/` | 1.4 + nuevas |
| 1 · Base local | Tablas (CDK + creación local compartida), reloj virtual, bus de eventos, simulador de reproducción | 1.2 |
| 2 · Nivel 0 | `DiscoverProcesses`, ejecuciones esperadas y mediana, `MissedRunWatchdog`, `OpenIncident` con historial | 2.1, 2.2, 3.2–3.4 |
| 3 · Clasificación | `AgentDecision`, `InvokeAgent` (prueba → Bedrock) | 1.3, 4.2, 4.3 |
| 4 · Nivel 1 | `RemediationPolicy`, `RedriveOrRestart`, `PollExecution`, espera `Retry-After` | 5.1–5.4 |
| 5 · Flujo | Ejecutor local de `GuardIA-Incident-Flow` + ASL | 5.5 |
| 6 · Nivel 2 | `ResolveOnCall`, Teams simulado, SES/Mailpit, recordatorios, `ResolveOnSuccess`, `SLA_BREACHED`, agrupación | 6.1–6.6 |
| 7 · Trazabilidad | Decorador de auditoría, resumen diario, heartbeat | 7.x, 8.x |
| 8 · Demo | Guion "noche completa" en un solo comando; luego despliegue con CDK | 9.x |

---

## 7. Decisiones pendientes

1. Clasificación de Genesys 5xx, `Runtime.OutOfMemory` y `States.Runtime` por JSONPath (propuesta en H6).
2. Bedrock real o simulado en local (recomendado: simulado, adaptador real al final).
3. Teams real o simulado (recomendado: simulado, webhook real para la demo final).
4. Cron real de cada proceso y si existen datos del proceso master.
5. Solo moto en memoria, o Docker (LocalStack / Step Functions Local) desde el inicio (recomendado: moto primero, Docker en la fase 5).

---

## 8. Avance — 2026-11-16 (tarde)

Decisión tomada: el cron de cada proceso se lee de la regla de EventBridge (o del schedule de EventBridge Scheduler) que lo lanza, no de una etiqueta. Queda en `design.md` como D14; las reglas del watchdog, como D15.

| Pieza | Estado | Dónde |
|---|---|---|
| Definición única de tablas (1.2, parcial) | Hecho. La usan `DataStack` (KMS, TTL, PITR, índice `execution_arn`), las pruebas y los demos. Falta la política IAM append-only de `IncidentEvents` | `src/guardia_common/tables.py` |
| Ejecuciones esperadas y duración máxima (3.3) | Hecho. Traduce el cron de AWS (6 campos, días 1–7, `?`, `L`, `#`) a croniter | `src/guardia_common/schedule.py` |
| `DiscoverProcesses` (2.1) | Hecho. Etiquetas, reglas, Scheduler, mediana, limpieza del registro | `src/lambdas/discover_processes/` |
| `MissedRunWatchdog` (3.4) | Hecho. `MISSED_RUN` y `STUCK_RUN`, publica a EventBridge, no repite incidentes abiertos | `src/lambdas/missed_run_watchdog/`, `src/guardia_common/watchdog.py` |
| Fase 1 · base local | Hecho: reloj virtual, planificador, bus de eventos en memoria, réplica de ejecuciones con vista en el tiempo (`RUNNING` hasta que terminan), noches guionizadas | `src/guardia_local/` |
| Demo de una noche | Hecho | `scripts/demo_discovery_and_watchdog.py` |
| 2.2 y reglas de EventBridge en CDK | Pendiente. Requiere empaquetar las Lambdas (pydantic y croniter no vienen en el runtime) | `infra/stacks/detection_stack.py` |

Supuestos que hay que confirmar:

- **Cron (H8):** el manifiesto de fixtures asume ejecución cada hora, a las :30 para EMT12 y a las :00 para EMT22 y EMTSAS. Cambiarlo es editar `schedule` en `tests/fixtures/executions/manifest.json`.
- **Ejecución retrasada:** si arranca después de los 10 minutos de tolerancia, se abre `MISSED_RUN` (lectura literal del requisito 3.1) y la ejecución exitosa posterior lo cierra. Alternativa: no abrir si ya arrancó.
- **Mediana:** se calcula sobre las últimas 14 ejecuciones `SUCCEEDED`. Las fallidas no cuentan, porque su duración no es representativa (EMTSAS falló en 7 s).

Entorno: el `.venv` en `/mnt/d` hacía tardar la batería 39 min; con `~/.venvs/guardia` tarda ~1 min (170 pruebas).

## 9. Avance — 2026-11-17: Nivel 0 completo en una sola noche

`scripts/demo_discovery_and_watchdog.py` ahora corre toda la detección más la clasificación en un solo comando (unos 18 s):

- Cada ejecución reproducida emite su evento `Step Functions Execution Status Change` en la hora exacta en que termina (`status_changes` en `src/guardia_local/timeline.py`).
- Las fallas reales se reproducen en horas de la noche (`scheduled_replays(..., replace=...)`): EMTSAS a las 00:00, con el Genesys 500 del 18-sep, y EMT12 a las 02:30, con el OOM del 23-sep.
- Las dos reglas que invocan `OpenIncident` usan los patrones de producción, `STATUS_CHANGE_PATTERN` y `WATCHDOG_PATTERN` en `guardia_common/incidents.py`; el stack de CDK debe usar los mismos. Las 18 ejecuciones `SUCCEEDED` no pasan el filtro de la regla.
- `DetectionPipeline` (`src/guardia_local/flow.py`) ejecuta `ClassifyIncident` justo después de abrir el incidente. Reemplaza al primer estado de `GuardIA-Incident-Flow` mientras el flujo sea un cascarón.
- A las 00:01 EventBridge entrega otra vez el mismo evento de EMTSAS, y no se crea un incidente nuevo (requisito 2.4).

Resultado: 7 incidentes, todos en estado `OPEN`.

| Hora | Incidente | Clasificación | Siguiente paso |
|---|---|---|---|
| 00:00 | FAILED EMTSAS (`Exception` en `IngestionControllerTask`) | `auto_recoverable` (redrive) | RemediationPolicy |
| 02:36 | FAILED EMT12 (`Runtime.OutOfMemory`) | `requires_escalation` | escalar |
| 03:10 | MISSED_RUN EMT22 | `requires_escalation` (regla 4.4) | escalar |
| 04:05 | STUCK_RUN EMT12 | `unknown` | agente Bedrock |
| 04:10 | MISSED_RUN EMTSAS y EMT99 | `requires_escalation` | escalar |
| 05:10 | MISSED_RUN EMT44 | `requires_escalation` | escalar |

Pruebas nuevas: `tests/unit/lambdas/test_detection_pipeline.py` (de punta a punta) y `tests/unit/guardia_local/test_timeline.py`. Pasan 176 pruebas y el lint está limpio.

Decisión pendiente:

- **Clasificación de `STUCK_RUN`:** los requisitos no la definen. Como no tiene error ni fila en el catálogo, cae en `unknown` y va al agente. Una alternativa es tratarlo como `MISSED_RUN` (siempre `requires_escalation`), porque cancelar y relanzar una ejecución que sigue corriendo no es seguro sin confirmar con el dueño.
