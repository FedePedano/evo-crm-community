# HANDOFF — Multitenant A2 (evo-flow / evo-crm)

> Estado al **2026-10-09**, rama `exp/multitenant-a2`. Documento hermano de
> `AGENTS.md` y `INTEGRAR_NUEVO_CANAL.txt`.
> Plan de ejecución final: `scripts/multitenant/PLAN-EJECUCION.md`.
> Contrato de slug/seguridad: `scripts/multitenant/CONTRACT.md`.

---

## 1. Resumen del estado

La **Fase A de multitenant (A2)** está completa en **CRM** y **Auth**, y la
parte de **evo-flow** está **propagada y commiteada** en la rama
`exp/multitenant-a2` (working tree limpio). Pendiente: Fase 2 corregida
(bypasses que quedan), el **Processor** (sin tocar), y el **Frontend** (sin
tocar). Todo validado localmente (tests y typecheck verdes) y reproducible en
el staging local.

Progreso por servicio (evidencia en §4):

| Servicio | Estado A2 | Rama/commit | Working tree |
|---|---|---|---|
| evo-crm | **Completo** | `exp/multitenant-a2` `5269e2b` | limpio |
| evo-auth | **Completo** (solo `validate`) | `exp/multitenant-a2` `a01ded9` | limpio |
| evo-flow | **Propagado** (Fase 2 pendiente) | `exp/multitenant-a2` `d639ab5` | limpio |
| evo-ai-processor | **Pendiente** | `feat/summarize-on-open` `6d2b624` | 7 archivos sucios (summarize/GC, ajenos a A2) |
| evo-ai-frontend | **Pendiente** | `fix/chat-list-refetch` `bda7f233` | limpio, 0 tenancy |

---

## 2. Arquitectura objetivo y decisiones

### Estrategia global: schema-per-tenant
- Un schema por cliente `cliente_<slug>` en cada Postgres + registro en
  `public.tenants`.
- `search_path` runtime **estricto**: `<tenant>,extensions` (SIN `public`).
  Solo el provisioning/seed usa `/public,extensions`.
- `evo_community` (CRM+Auth) y `evo_campaign` (Flow) son bases distintas; cada
  una con sus schemas de tenant.
- **Un solo formato de slug** (ver `CONTRACT.md`): canónico en
  `public.tenants.slug`; PG `cliente_<slug>`, CH `tenant_id=<slug>`,
  Processor `tenant_slug=<slug>`, Redis scope `<slug>`. Nunca derivado por
  servicio.

### Decisiones por servicio

**CRM (`5269e2b`)** — modelo de tenants completo:
`Tenant` / `TenantMembership` / `TenantContext` / `TenantSwitcher` /
`SidekiqTenantMiddleware` / `TenantDispatch`. 13/14 schedulers fan-out por
tenant; el 14º (`account/conversations_resolution_scheduler_job.rb`) pendiente
de self-dispatch (ver §5, S2). Endpoint-audit: 206 rutas, 0 fugas.
Spec `tenant_isolation_spec.rb` (5/5) en `spec/multitenant/`.

**Auth (`a01ded9`)** — único endpoint tenant-aware: `validate` devuelve
`tenants[]` por email. login/refresh/me/register sin tenants, JWT sin claims
de tenant. **Pendiente (Fase 0, plan final):** cookie host-scope a
`beexa-auth.evonectech.com` (hoy `COOKIE_DOMAIN=.evonectech.com` compartido),
`AUTH_ALLOWED_HOSTS`, claims `tenants[]` en JWT y **403 por claims**
(§CONTRACT 4). `_evo_rt` scope ya; verificar consumidores de `_evo_at`.
Pendiente (Track D): `status_statement` fall-resuelto para login verdadero.

**Flow (`d639ab5`)** — overlay schema-per-tenant (seam `tenant_db_context`,
`options.search_path` por migración, `SET LOCAL` dentro de txn) + propagación
de `tenantId` por todo el pipeline:
- `payload-normalizer` lee `X-Tenant-Slug` y **sanear** el slug (`^[a-z0-9_]+$`).
- `clickhouse-writer` escribe `tenant_id` (DEFAULT `'default'` = legacy global).
- Contrato `events-received` con `tenantId`.
- Activities/nodos con `tenantId` en inputs; dispatch via
  `servicesForTenant` / `activeJourneysForTenant`.
- `journey-trigger-processor`: **fail-closed** si `MULTITENANT_SCHEMA` está
  activado y el evento no trae `tenantId`; log total con `withoutTenantAttribution`.

**Processor (pendiente, decisión tomada — Track B, paralelo desde Día 1):**
columna `tenant_slug` en las tablas + RLS.
- **`ENABLE`/`FORCE ROW LEVEL SECURITY`** + política única
  `tenant_slug = current_setting('app.tenant_slug')`.
- `SET LOCAL app.tenant_slug` por request/job. **No** editar ~30 `WHERE`.
- **13 tablas**: las 12 de `processor-tenant-migration.sql` + **`users`**
  (la migración NO la cubre; revisar `plans/features/plan_features` como
  globales vs tenant).
- Los **2 `SessionLocal()` propios** (`tool_builder.py:70`,
  `custom_tools.py:84`) + `database.py:63` → sesiones tenant-scoped.
- **Aserción de arranque** post-`create_all`: `relrowsecurity = true` en las
  13 tablas ⇒ fail rápido si alguna no tiene RLS (`FAIL_FAST_MULTITENANT=1`).
- **Reescribir 2 tests legacy single-tenant** (`test_agent_object_authz.py`
  "no owner column", `test_test_panel_cross_user_isolation.py` pool-wide).
- Fix `NameError: name 'request' is not defined` (11 archivos `*_routes.py`).

**ClickHouse (decisión tomada):** setting
`max_bytes_before_external_group_by` con valor en bytes (env-configurable) en
los 3 sitios (`clickhouse.service.ts:51,102`, `batch-database-optimizer.service.ts:211`).
**No pinchar versión**; verificar la versión real en prod antes de aplicar.
**Backfill (2.4 ampliado):** `createContactEventsTable` ordena por
`(occurred_at, event_type)` SIN `tenant_id` (`:620`) → `ALTER TABLE ... UPDATE
tenant_id='<slug>' WHERE tenant_id='default'` es viable (CH bloquea UPDATE solo
sobre columnas del sort key). **No** adoptar `ORDER BY (tenant_id,...)` en
tablas existentes (rompería el backfill). Mutation que reescribe partes →
**ventana tranquila + `mutations_sync=2`**. Writer siempre slug explícito;
fail-closed sin slug (nunca `'default'` como destino).

### Mecanismo de migraciones por schema (commit `672f698`)
`options.search_path CURRENT()` + split del contenido de cada migración en
`before/after`, probado con replay idempotente por schema
(`scripts/multitenant/flow-replay-tenant.mjs`).
Migración de datos ClickHouse: `clickhouse-tenant-migration.sql`.

### Descubrimiento de tenants y provisioning

- Flow resuelve `slug → schema` contra **`public.tenants` en SU PROPIA DB**
  (`evo_campaign`), no en `evo_community`: `resolveTenantSchema` en
  `src/multitenant/tenants.registry.ts` hace
  `SELECT schema_name FROM public.tenants WHERE slug = $1 AND status = 'active'`
  (cache 30 s, prefijo `cliente_` obligatorio, `UnknownTenantError` fail-closed).
- Por tanto hay **dos registros de tenants** por cliente: CRM/Auth en
  `evo_community` y Flow en `evo_campaign`. La sincronización entre ambos es
  un loose-end a decidir en el provisioning.
- `scripts/multitenant/provision_tenant.py <slug>` crea el schema + fila en
  `public.tenants`, pero hoy **apunta a Supabase TEST** vía `.env.test`; para
  staging hay que retargetearlo al PG local de flow (5433).
- `MULTITENANT_SCHEMA=1` (string) en el `.env` de Flow activa el overlay:
  `registerSchemaPerTenantOverlay()` (`src/multitenant/register.ts`) reemplaza
  el seam `tenant_db_context` por `schemaPerTenantDbContext` (search_path
  estricto `<tenant>,extensions`, sin `public`). Sin la variable → no-op
  community (single-account intacto, `single-account.spec.ts` verde).
- El intake resuelve el slug del header `X-Tenant-Slug` (regex `^[a-z0-9_]+$`,
  ausente/malformado → `null` → evento legacy `'default'`). La atribución por
  webhook path `/webhooks/<slug>/...` en el intake está **pendiente** (hoy solo
  header).

---

## 3. Estado git (post-commit, verificado)

| Repo | Rama | HEAD | Estado |
|---|---|---|---|
| super-repo | `exp/multitenant-a2` | `9222032` | limpio (salvo submódulo processor sucio) |
| evo-ai-crm-community | `exp/multitenant-a2` | `5269e2b` | limpio |
| evo-auth-service-community | `exp/multitenant-a2` | `a01ded9` | limpio |
| evo-flow-community | `exp/multitenant-a2` | `d639ab5` | limpio |
| evo-ai-processor-community | `feat/summarize-on-open` | `6d2b624` | **7 archivos sucios** (ajenos a A2) |
| evo-ai-frontend-community | `fix/chat-list-refetch` | `bda7f233` | limpio, 0 tenancy |

> **Pendiente de commit (super-repo, tras aprobación del plan final):**
> `scripts/multitenant/CONTRACT.md` + `scripts/multitenant/PLAN-EJECUCION.md`
> (este HANDOFF los referencia). Commitearlos juntos en `exp/multitenant-a2`.

### Registro de commits de esta tanda
- super-repo `5b0322c` — pin flow `d639ab5` + `docker-compose.staging.yml` + `scripts/multitenant/flow-replay-tenant.mjs`.
- Flow `d639ab5` — A2 propagación `tenantId` (47 archivos, +721/−195).
- Flow `672f698` — mecanismo migraciones por schema (`search_path`, probado).
- Flow `5d50732` — migración ClickHouse `tenant_id` + checklist fase 2.
- Flow `706f8a4` — `bypasses-fase2.md` (checklist).
- Flow `b51143d` — overlay schema-per-tenant (seam `tenant_db_context`).

> ⚠️ El Processor **NO** está en `exp/multitenant-a2`; está en `feat/summarize-on-open`
> con working tree sucio (fixes google_calendar/tool_builder vía `git diff`). NO
> hacer `git checkout .` allí sin backup: son fixes de producción ya desplegados.

---

## 4. Cómo validar / evidencia

### Local (mi máquina)
- Flow: `npx tsc -b --noEmit` exit 0; suite completa **142 suites / 1139 tests
  passed, 4 skipped** (con `EVOAI_CRM_API_TOKEN`/`EVOAI_CRM_BASE_URL`/
  `POSTGRES_DB_HOST`).
- Espec `single-account.spec.ts` (guard FR44): sanciones A2 documentadas
  (4 tokens ident + 6 líneas neutras anchoradas; self-tests OK).
- Espec journeys: mock de `multitenant/tenant-services`; aserciones de log con
  2º arg `withoutTenantAttribution`.
- Espec campaigns: `stop(id)` (1 arg) — se eliminó el `accountId` del service.

### Replay por schema (validado antes del último commit)
`flow-replay-tenant.mjs`: 19 tablas en `cliente_demo_inmo` / `cliente_otro_tenant`,
`public` en 0, idempotente; extensiones en `extensions`; path estricto.

### Modelo de leak test a replicar
`evo-ai-crm-community/spec/multitenant/tenant_isolation_spec.rb` (5/5).
**Esqueleto agendado en Fase 0 (+0.5d)** y es el **gate al cierre de cada
fase** (0/A/B/C/D/6). Casos: B no ve datos de A; **token de A + tenant B ⇒
403**; schema estricto (`<tenant>,extensions` sin `public`). Complementa (no
reemplaza) los guards estáticos de DoD Fase 2.

### Evidencia débil a rehacer
- `scripts/multitenant/e2e_tenant_switch.py:75` usa `<schema>, public, extensions`
  e `flow-replay-tenant.mjs:37` usa `${schema},extensions,public` → **no
  replican el path estricto**. El "0 fugas / 160 reqs" previo es débil: rehacer
  con path estricto.

### Runbook operativo (local)

```bash
# Test + types de Flow (requiere las env; sin ellas 6+ suites fallan por config)
cd evo-flow-community
EVOAI_CRM_API_TOKEN=test-token EVOAI_CRM_BASE_URL=http://localhost:3000 \
POSTGRES_DB_HOST=localhost POSTGRES_DB_USERNAME=flow POSTGRES_DB_PASSWORD=x \
POSTGRES_DB_DATABASE=flow npx jest
npx tsc -b --noEmit

# Stack staging aislado (desde la raíz del super-repo)
docker compose -f docker-compose.staging.yml up -d
# Peek por puerto: CRM 3100 · Flow 3200 · PG flow 5433 · ClickHouse 8124/9001 · redis 6380

# Provision de tenant — OJO: hoy apunta a Supabase TEST vía .env.test;
# retargetear a PG local 5433 antes de usar contra el staging.
python3 scripts/multitenant/provision_tenant.py demo_inmo

# Replay de migraciones Flow en el schema del tenant (requiere `npm run build` + PG flow levantado)
node scripts/multitenant/flow-replay-tenant.mjs demo_inmo

# Audit de bypasses y lecturas ClickHouse (Fase 2)
grep -rn "AppDataSource.getRepository\|app.get('DataSource')" src  # flow
grep -rl "contact_events" src --include='*.ts'                      # flow
```

---

## 5. Pendientes (ordenados — plan final: `PLAN-EJECUCION.md`)

### Fase 0 — Seguridad auth transversal + esqueleto leak test (1.5d, en serie)
1. Cookie host-scope: `_evo_rt` ya (path `/api/v1/auth`); **verificar
   consumidores de `_evo_at`** antes de host-scopearla (¿backend o solo
   front same-origin?). Quitar `COOKIE_DOMAIN=.evonectech.com` compartido.
   `secure` + `same_site` estricto.
2. `AUTH_ALLOWED_HOSTS`: validar `request.host` al emitir/borrar cookies
   (anti DNS-rebinding / confused-deputy) + validar `Origin`.
3. **Claims `tenants[]` en JWT** (mapper Doorkeeper; re-emisión en deploy).
4. **403 por claims** (CONTRACT §4): Flow `TenantInterceptor`, CRM
   `TenantSwitcher`, Auth `me/validate` — slug resuelto (host o header),
   saneado `^[a-z0-9_]+$`, resuelto contra el registro, **no en `tenants[]` ⇒
   403** (jamás `'default'` ni schema ajeno).
5. Commit `CONTRACT.md` + `PLAN-EJECUCION.md` (+ este HANDOFF).
6. Esqueleto leak-test 2-tenants (CRM+Flow, model + leak case ver §4).

**DoD**: leak token-A/tenant-B ⇒ 403; cookies host-scoped; claims
verificados en los 3 backends.

### Track A — evo-flow + ClickHouse (5.5-6.5d, en serie; único bloqueante = 2.4)
1. **Bypasses que quedan** (re-auditoría 2026-10-09, checklist `bypasses-fase2.md`):
   - `journey-execution.activities.ts:182-183` — `AppDataSource.getRepository(JourneySession/Journey)` (pool global).
   - `campaign-execution.activities.ts:226,281,363` — `app.get('DataSource').getRepository('CampaignContact'/'Campaign')` sin tenant; 6 de 7 inputs sin `tenantId` (solo `UpdateExecutionProgressInput:90` lo lleva).
   - `nodes/scheduled-action.node.ts:56,62` — `initializeDatabase()` sin `tenantId` + `getRepository('ScheduledJourneyAction')`.
   - 4 nodos con DI de servicios globales (repos de `AppDataSource`): `add-label`, `remove-label`, `update-custom-attribute`, `transfer-journey`.
   - Delicadeza `wait.activities.ts` (ya envuelto): cache singleton `waitRegistryServiceCache` debe quedar por-tenant.
   - Ya migrados en `d639ab5`: journey-trigger-processor (fail-closed), `action-nodes.activities`, `variable-interpolation.util`, `base.node.ts` (seam `getTenantDataSource`).
2. **Redis namespacing**: registrar `cache_key_scope` en `src/multitenant/register.ts` (el fix va ahí, NO en `tenant-services.ts`).
3. **ClickHouse — 2.4 (bloqueante)**: aplicar `max_bytes_before_external_group_by` (absoluto) en los 3 sitios; verificar versión real de prod antes.
4. **`createContactEventsTable()`** (`clickhouse.service.ts:596-644`): agregar columna `tenant_id` con `ORDER BY` SIN tenant_id (ver decisión §2).
5. **Backfill**: `ALTER TABLE contact_events UPDATE tenant_id='<slug>' WHERE tenant_id='default'` — mutation, **ventana tranquila + `mutations_sync=2`**; writer fail-closed sin slug (nunca `'default'` como destino); lector `tenant_id=<slug>`.
6. **kafka_queue + MV**: reconstruye SELECT pasando `tenant_id` (validar en staging; Kafka Engine no corre en clickhouse-local).
7. Rehacer replay/e2e con `search_path` estricto (sin `public`).
8. Fail-closed `TenantInterceptor`: `contact-events.controller.ts:22`, `event-search.controller.ts:18`, `click-tracking.controller.ts:34`.
9. Guards estáticos (2.2), lecturas runtime 16 sitios (2.5), `flow-replay-tenant.mjs` path estricto (2.7), validación + commit (2.8).

**DoD**: leak test 2-tenants verde con schema estricto; bypasses sin fuga;
CH backfilleado en staging.

### Track B — Processor (3-4d, **paralelo desde Día 1**; solo contrato del slug)
- Migración: 12 tablas del SQL + **`users` (13ª)**; revisar
  `plans/features/plan_features` (globales vs tenant). RLS en las 13
  (`ENABLE/FORCE` + política `current_setting('app.tenant_slug')`).
- **2 `SessionLocal()` propios** (`tool_builder.py:70`, `custom_tools.py:84`)
  + `database.py:63` → sesiones tenant-scoped con GUC `app.tenant_slug` vía
  `after_begin`.
- **`custom_tools.py:84`**: resolución de credenciales con contexto de tenant.
- 4 tools `google_calendar` psycopg2 crudo (create/edit/check/cancel): GUC
  explícito o fail ruidoso.
- **Aserción de arranque** post-`create_all` (`main.py`):
  `relrowsecurity = true` en 13 tablas ⇒ fail rápido
  (`FAIL_FAST_MULTITENANT=1`).
- **Reescribir 2 tests legacy**: `test_agent_object_authz.py` ("no owner
  column") y `test_test_panel_cross_user_isolation.py` (pool-wide).
- Revisar `PostgresDestination` / `load_pandas_into_postgres` (BULK con RLS).
- Rol de app SIN `BYPASSRLS` (rol admin separado).
- Fix `NameError: name 'request' is not defined` (11 archivos `*_routes.py`).

**DoD**: leak test 2-tenants verde en Processor (token A + tenant B ⇒ 403);
RLS verificada en arranque.

### Track C — Frontend + Auth (2-3d, paralelo)
- `validate`/`me` → `tenants[]` en el selector del UI (solo muestra los del
  token); headers `X-Tenant-Slug` en el cliente de API; nginx wildcard
  `api_tenant` / subrutas.
- Encaje con claims JWT de Fase 0 (selector consume `tenants[]` firmados).

**DoD**: dos tenants visibles en UI, switching sin datos cruzados.

### Track D — CRM/Auth (2-3d, paralelo)
- Scheduler pendiente: self-dispatch en `channels/whatsapp/templates_sync_scheduler_job.rb` y `account/conversations_resolution_scheduler_job.rb` (patrón = los otros 12).
- Auth: `status_statement` fall-resuelto (login verdadero con múltiples `tenants`).
- Decisión **registro dual** (`public.tenants` en `evo_community` vs
  `evo_campaign`): sincronización entre ambos a resolver en provisioning (§2).

**DoD**: auto-provision de schema; registro dual coherente.

### Fase 6 — Provision, backfill y deploy (2-3d)
- Retarget `provision_tenant.py` → PG local 5433 + alta dual (ambas DBs).
- Backfill CH en ventana tranquila (`mutations_sync=2`).
- VPS aislada (Supabase clone / wildcard DNS) + prueba viaje completa T3.

**DoD**: `provision_tenant.py beexa` funcional en staging; stack aislado en
VPS con backups (§10 AGENTS).

### Super-repo / infra (transversal)
- Docker daemon en pánico (500 Internal Server Error) — reiniciar (pendiente confirmar).
- Verificar versión ClickHouse real en prod (solo lectura).
- Proyecto Supabase clonado + wildcard DNS para prueba VPS real.
- `flow-staging` con `RUN_MODE: api` no levanta worker Temporal: decide worker aparte para probar journeys.

---

## 6. Riesgos y trampas (lecciones)

1. **Processor NO está en la rama multitenant** — su working tree sucio es
   producción desplegada (summarize/GC). Aislar commits.
2. **`SET LOCAL` exige txn abierta** → evento `after_begin` de SQLAlchemy, no
   `on_connect`.
3. **Rol de app con `BYPASSRLS` anula RLS** silenciosamente.
4. **`create_all` con RLS**: ópero DDL → `ENABLE/FORCE` → `CREATE POLICY`, por tabla.
5. **psycopg2 crudo en `google_calendar`** no aplica RLS → 0 filas silencioso.
6. **Search path estricto**: incluir `public` en el runtime reabre el leak.
7. **Guard FR44 (`single-account.spec.ts`)**: el intake tocó `src/runners/`;
   las sanciones A2 están documentadas como tokens/líneas — no borrarlas sin
   razón (prueban que el intake sella, no ruthea).
8. **Baseline de lint sucio**: prettier/eslint fallan también en HEAD (verificado).
   El gate real es `tsc -b` + jest.
9. `e2e_tenant_switch.py` y `flow-replay-tenant.mjs` incluyen `public` en el
   path → evidencia "0 fugas" debería rehacerse con path estricto.
10. **Cookie en host compartido**: `beexa-auth.evonectech.com` sirve a TODOS
    los tenants → host-scope solo detiene el compartir entre subdominios;
    el aislamiento real es la validación **slug ∈ `tenants[]` ⇒ 403**.
11. **Falsificación de tenant**: el `X-Tenant-Slug`/host resuelto debe validarse
    contra los claims; sin eso, usuario de A con header de B entra (caso
    obligatorio del leak test).
12. **`ALTER TABLE ... UPDATE` en ClickHouse** falla si `tenant_id` entrara al
    sort key → mantener `ORDER BY (occurred_at, event_type)` en tablas
    existentes (backfill roto si se reordena).
13. **Slug con formatos dispares**: `beexa` en CH vs `cliente_beexa` en PG
    rompe el matching en silencio → un solo formato vía `CONTRACT.md`, todo
    derivado desde el registro.

---

## 7. Qué sigue

1. **Fase 0** (1.5d): cookie host-scope + `AUTH_ALLOWED_HOSTS` + claims
   `tenants[]` + **403 por claims** + esqueleto leak-test 2-tenants + commit
   de `CONTRACT.md`/`PLAN-EJECUCION.md`/HANDOFF (super-repo).
2. **Tracks en paralelo desde Día 1**: A (Flow+ClickHouse, bloqueante 2.4),
   B (Processor, paralelo), C (Frontend+Auth), D (CRM/Auth).
3. Empezar Track A por `journey-execution.activities.ts:182-183` (bypass más
   crítico); Track B por la migración 13 tablas + RLS.
4. Aplicar decisión ClickHouse (`max_bytes_before_external_group_by` absoluto)
   y backfill en ventana tranquila.
5. Cierre de cada fase con el leak test 2-tenants (gate).

---

## 8. Glosario y Definition of Done (Fase 2)

### Glosario
- **`X-Tenant-Slug`**: header HTTP con el slug del tenant (intake/clientes API). Regex `^[a-z0-9_]+$`; ausente/malformado → `null` (evento legacy).
- **`tenantId`**: campo del envelope `events-received` e inputs de activities/nodos (concepto Flow).
- **`tenant_id`**: columna ClickHouse `LowCardinality(String) DEFAULT 'default'` — valor = slug pelado (`beexa`, no `cliente_beexa`).
- **schema `cliente_<slug>`**: schema de tenant en Postgres (prefijo obligatorio; `public` nunca).
- **`public.tenants`**: registro `slug → schema`; por DB (`evo_community` para CRM/Auth, `evo_campaign` para Flow).
- **`MULTITENANT_SCHEMA=1`**: env (string) que activa el overlay schema-per-tenant en Flow; sin ella, no-op community.
- **`tenants[]`**: claims firmados del JWT (Fase 0) con los slugs del usuario; **403** si el slug resuelto del request no está ahí.
- **`CONTRACT.md`**: fuente de verdad de nomenclatura (slug canónico + derivados) y de la regla de autorización por claims.
- **legacy `'default'`**: evento sin tenant atribuido = pool global (solo escritura); lectura SIEMPRE fail-closed sin tenant.

### Definition of Done (Fase 2)
1. `grep -rn "AppDataSource.getRepository\|app.get('DataSource')" src` = 0 fuera de `database/` + `src/multitenant/` + seam `base.node.ts` — convertido en spec.
2. `grep "FROM contact_events"` sin `tenant_id` en el mismo query = 0 — convertido en spec (análogo a `single-account.spec.ts`).
3. Search path estricto (`<tenant>,extensions`, sin `public`) en runtime y en replay/e2e; evidencia nueva con path estricto.
4. Journey fail-closed: con `MULTITENANT_SCHEMA=1` y evento sin `tenantId` → skip con log `withoutTenantAttribution`.
5. Suite completa de Flow verde (hoy: 142 suites / 1139 tests) + `npx tsc -b --noEmit` limpio.
6. **Leak test 2-tenants verde** (B no ve datos de A + token A/tenant B ⇒ 403) al cierre de fase — gate global, no solo de Fase 2.