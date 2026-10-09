# MAPA_FEDERICO — Multi-tenancy A2 (Mi carril)

> SOLO LECTURA - Este documento resume mi carril tal como está en `exp/multitenant-a2`. Generado al 2026-10-09.

**Mi carril:**
- **Fase 0** (seguridad Auth): cookie host-scope, `AUTH_ALLOWED_HOSTS`, claims `{slug, role}` en JWT, 403 si el slug no está en los claims, esqueleto leak test 2 tenants.
- **Track A** (evo-flow + ClickHouse): bypasses, guards, Redis (`cache_key_scope`), setting ClickHouse + tabla con backfill, lecturas, interceptores (`TenantInterceptor`), replay estricto (`flow-replay-tenant.mjs`).
- **Fase 6** (`provision_tenant.py`, alta dual, VPS aislada).

---

## 1. Submódulos/servicios que toca mi carril y para qué sirve cada uno

| Submódulo/servicio | Rol en mi carril | Notas (verificado en código) |
|---|---|---|
| **evo-auth-service-community** (rama `exp/multitenant-a2`, HEAD `a01ded9`) | **Fase 0 — Auth transversal.** Controla emisión de cookies (`_evo_rt`, `_evo_at`), dominio de cookie, validación de tokens, claims JWT vía Doorkeeper, y `GET /api/v1/auth/validate` devuelve `tenants[]` por email. | `app/controllers/concerns/auth_helper.rb` gestiona cookies y `cookie_domain` (PUBLIC_SUFFIX). `app/controllers/api/v1/auth_controller.rb:154` añade `tenants: tenants_for_user(...)` al `validate`. `config/initializers/doorkeeper.rb:152-187` genera payload JWT (ahora sin `tenants[]`). `lib/evo_extension_points/token_claims.rb` permite añadir claims vía extension point. `spec/concerns/auth_helper_cookie_domain_spec.rb` cubre `COOKIE_DOMAIN` override. |
| **evo-flow-community** (rama `exp/multitenant-a2`, HEAD `d639ab5`, estado limpio) | **Track A — Núcleo A2.** Opt-in `MULTITENANT_SCHEMA=1`, overlay `tenant_db_context` (schema-per-tenant con DataSource por tenant, `search_path=<tenant>,extensions`, sin `public`), `TenantInterceptor` (HTTP), `tenants.registry.ts` (slug→schema contra `public.tenants` de DB flow), `cache_key_scope` en base-cache, `tenant-services.ts` (non-HTTP paths), propagación `tenantId`, fail-closed sin tenant en journeys. | `src/multitenant/register.ts:16` registra overlay. `src/multitenant/tenant-schema-context.impl.ts` crea DataSources por tenant con pool pequeño. `src/multitenant/tenants.registry.ts` cache 30s + validación slug `^[a-z0-9_]+$`. `src/multitenant/tenant.interceptor.ts` extrae slug (header `X-Tenant-Slug` o subdominio) y delega a `TenantDbContext`. `src/evo-extension-points/registry.ts` expone `tenant_db_context` y `cache_key_scope`. `src/modules/cache/services/base-cache.service.ts:484` consume `cache_key_scope`. |
| **ClickHouse (evo-flow)** | **Track A — Aislamiento por `tenant_id`.** Escritura con `tenant_id` (esquema runtime legacy no lo incluye aún), lectura debe filtrar por `tenant_id=<slug>`, backfill `ALTER TABLE ... UPDATE tenant_id='slug' WHERE tenant_id='default'`. Setting `max_bytes_before_external_group_by` a aplicar (hoy `max_bytes_ratio_before_external_group_by:0.5`). | `src/multitenant/clickhouse-tenant-migration.sql` añade `tenant_id` a `contact_events`/`contact_events_kafka_queue`, crea índice bloom + MATERIALIZE; MV requiere recrear. `src/modules/processing/clickhouse/clickhouse.service.ts:51,102,596-644` tiene setting ratio y `createContactEventsTable()` **SIN `tenant_id`** (alinear runtime). `src/multitenant/clickhouse-fase2.md` lista 16 sitios con `contact_events`. |
| **evo-ai-crm-community** (rama `exp/multitenant-a2`, HEAD `5269e2b`) | **Soporte/consumo + leak test base.** Modelo `Tenant`, `TenantMembership`, `TenantContext`, `TenantSwitcher` middleware (schema-per-tenant vía `search_path`), `SidekiqTenantMiddleware`, 12 schedulers fan-out vía `TenantDispatch`. `evo_auth_concern` verifica membresía por email cuando hay `TenantContext`. Esqueleto `spec/multitenant/tenant_isolation_spec.rb` (5/5). | `app/middleware/tenant_switcher.rb` resuelve slug (ENV/TENANT_SLUG/header/subdominio), busca `Tenant.active`, `SET search_path TO <schema>,extensions` (sin `public`), `RESET` en `ensure`. `app/models/tenant.rb` califica `public.tenants`. `app/models/tenant_context.rb` expone `log_tag`. `spec/multitenant/tenant_isolation_spec.rb` crea schemas temporales y valida aislamiento + higiene pool. |
| **evo-ai-processor-community** (rama `feat/summarize-on-open`, HEAD `6d2b624`, **7 archivos sucios** — ajenos a A2) | **Track B (paralelo) — contrato slug/RLS pendiente.** Migración base `processor-tenant-migration.sql` lista 12 tablas + `users` (13ª) pendiente; 2 `SessionLocal()` propios (`tool_builder.py:70`, `custom_tools.py:84`) + `database.py:63`; aserción arranque post-`create_all`; fix `NameError: request` en `*_routes.py` (11 archivos afectos). **Mi carril NO modifica esto ahora** pero es referencia contractual. | `src/services/adk/tool_builder.py:70`, `src/services/adk/custom_tools.py:84`, `src/config/database.py:63` usan `SessionLocal()` propio. `src/api/summarize_routes.py` usa `request: Request` (válido). `src/api/google_sheets_routes.py:277-315` **carece de `request: Request` en firma** y usa `error_response(request=request, ...)` (líneas 307,315) → genera `NameError: name 'request' is not defined`. Otros `*_routes.py` con mismo patrón detectados. `src/main.py:226` hace `Base.metadata.create_all`. |
| **Infra/scripts multitenant** (super-repo `scripts/multitenant/`) | **Fase 6 + tooling.** `provision_tenant.py` (orquesta alta, idempotente, limpia ante fallo; apunta a Supabase TEST vía `.env.test` hoy), `deprovision_tenant.py`, `flow-replay-tenant.mjs` (replay migraciones Flow por schema con path estricto), `e2e_tenant_switch.py` (harness TenantSwitcher). | `provision_tenant.py:29-34` lee `SUPABASE_TEST_POOLER_SESSION_URL`. `flow-replay-tenant.mjs:37` usa `extra: { options: '-c search_path=${schema},extensions,public' }` (**incluye `public`** — debe estricto `<schema>,extensions`). `e2e_tenant_switch.py:75` hace `SET search_path TO {schema}, public, extensions` (**incluye `public`**). `processor-tenant-migration.sql` define política `tenant_slug = current_setting('app.tenant_slug')`. |

**Verificado/supuesto:** Lo anterior es **verificado** leyendo archivos actuales. Los HEAD/submódulos son los del estado `exp/multitenant-a2`. `docs/multitenant/` **no existe aún** (creado ahora).

---

## 2. Por cada tarea de mi carril: archivos exactos (archivo:línea), qué hay que cambiar y cómo verificar que quedó bien

### Fase 0 — Seguridad Auth

| Tarea | Archivos exactos | Qué hay que cambiar (**supuesto: aún no hecho**) | Cómo verificar (crítico) |
|---|---|---|---|
| **0.1 Cookie host-scope** | `evo-auth-service-community/app/controllers/concerns/auth_helper.rb:84-108,113-120,122-155` (métodos `set_refresh_cookie`, `set_access_token_cookie`, `cookie_domain`). `app/controllers/api/v1/auth_controller.rb:89-102` (delete cookies). | Quitar `COOKIE_DOMAIN=.evonectech.com` compartido. Host-scopear `_evo_rt` (path `/api/v1/auth`) y `_evo_at` al **host específico** del auth (`beexa-auth.evonectech.com`) en producción. Verificar consumidores de `_evo_at` antes de host-scopearla (hoy no hay referencias fuertes salvo set/delete). Mantener `secure: true`, `same_site: :none` solo si `is_secure_request?` true; caso dev/ngrok conservar nil domain. | **Verificado:** `cookie_domain` ya lee `ENV['COOKIE_DOMAIN']` override (`:124`). Logs muestran comportamiento actual. **Verificar:** `grep -rn "_evo_at" evo-auth-service-community/app | grep -v cookie_domain\|set_access_token_cookie\|delete` para confirmar consumidores. **Test:** `spec/concerns/auth_helper_cookie_domain_spec.rb` debe seguir pasando + nuevos casos host-scope. |
| **0.2 AUTH_ALLOWED_HOSTS** | `evo-auth-service-community/app/controllers/concerns/auth_helper.rb` (emitir/borrar cookies + refresh). `evo-auth-service-community/app/controllers/api/v1/auth_controller.rb`. Posible nuevo initializer `config/initializers/auth_allowed_hosts.rb` (**supuesto**). | Añadir validación `request.host` contra `ENV['AUTH_ALLOWED_HOSTS']` (lista separada por comas) al emitir/borrar cookies y en endpoints sensibles (login/refresh/logout/validate). Validar `Origin` header cuando presente. Anti DNS-rebinding/confused-deputy. | **Verificar:** Buscar referencias `AUTH_ALLOWED_HOSTS` (**ninguna hoy** en auth). Añadir chequeo temprano: si host no permitido → `403`/rechazo coherente con API. **Prueba:** con host inválido, emisión cookie rechazada; con host válido OK. |
| **0.3 Claims `tenants[]` en JWT** | `evo-auth-service-community/config/initializers/doorkeeper.rb:152-187` (token_payload). `evo-auth-service-community/lib/evo_extension_points/token_claims.rb` (ya existe, default `{}`). `evo-auth-service-community/app/controllers/api/v1/auth_controller.rb:167-191` (`tenants_for_user` ya devuelve filas con `slug, schema_name, plan, role`). | Incluir `tenants` (array de slugs) en JWT claims. Dos vías: (a) poblar desde `tenants_for_user(user)` en `token_payload` (lee `public.tenants` vía AR/SQL) o (b) consumir `EvoExtensionPoints::TokenClaims.claims_for(user)` y añadir `tenants` ahí (respetando RESERVED_JWT_KEYS). Debe listar **solo slugs activos** (`status='active'`) con membresía por email. | **Verificado:** `tenants_for_user` existe y consulta `public.tenants t JOIN public.tenant_memberships m ON m.tenant_id=t.id AND lower(m.email)=$1 WHERE t.status='active'` (`auth_controller.rb:175-191`). **Verificar:** JWT actual no trae `tenants` (solo sub,email,name,type,role,setup_active,iss,aud,jti,iat,exp). Añadir claim `tenants: [...]`. **Validar:** Doorkeeper firma con mismo secreto; consumidores deben tolerar campo extra. |
| **0.4 403 por claims (regla transversal)** | **Flow:** `evo-flow-community/src/multitenant/tenant.interceptor.ts:22-57`. `evo-flow-community/src/evo-extension-points/tenant-db-context/*`, `evo-flow-community/src/multitenant/tenants.registry.ts`. **CRM:** `evo-ai-crm-community/app/middleware/tenant_switcher.rb:17-101`, `evo-ai-crm-community/app/controllers/concerns/evo_auth_concern.rb:86-111` (`verify_tenant_membership!` ya existe: verifica membresía por email cuando hay schema). **Auth:** `evo-auth-service-community/app/controllers/api/v1/auth_controller.rb:146-163` (`validate`) y posibles usos. | **Contrato §4:** slug resuelto (host/header), saneado `^[a-z0-9_]+$`, resuelto contra registro `status='active'`, **si slug resuelto NO está en `tenants[]` del JWT ⇒ 403** (jamás `'default'`). Flow interceptor: además de resolver slug contra tenants.registry (DB flow), debe validar contra claims JWT si request trae token (o delegar a validación). CRM TenantSwitcher: tras resolver tenant válido, debe cruzar con claims del token del request. Auth `validate`/`me`: reforzar coherencia. | **Verificado:** Flow interceptor hoy lanza `NotFoundException('unknown tenant')` si slug inválido/desconocido (`:44-55`). CRM `verify_tenant_membership!` chequea membresía por email (no por claims). **Falta:** validación explícita **slug ∈ tenants[] claims**. **Leak test requerido:** token de A + tenant B ⇒ 403. |
| **0.5 Commit CONTRACT/PLAN/HANDOFF** | Super-repo: `scripts/multitenant/CONTRACT.md` (existe), `scripts/multitenant/PLAN-EJECUCION.md` (existe), `HANDOFF.md` (existe) — **todos ya presentes** en `exp/multitenant-a2`. | Confirmar estado (ya commiteados/pendientes según §3 HANDOFF: "Pendiente de commit (super-repo, tras aprobación del plan final): CONTRACT.md + PLAN-EJECUCION.md (+ este HANDOFF)" — pero HANDOFF.md ya existe con contenido completo). **Acción:** verificar `git diff --name-only` en super-repo. | **Verificar:** `cd /home/maldito/beexa/crm/evo-crm-community && git diff --name-only -- scripts/multitenant docs 2>&1 | head -10`. Si nada modificado, podrían estar staged? También revisar `git status`. |
| **0.6 Esqueleto leak-test 2-tenants** | **CRM:** `evo-ai-crm-community/spec/multitenant/tenant_isolation_spec.rb` ya existe (5/5) — modelo base. **Flow:** crear `evo-flow-community/src/multitenant/__spec__/tenant-leak.spec.ts` o ampliar `spec/` con test 2-tenants con `MULTITENANT_SCHEMA=1` (**supuesto: no existe aún**). **Auth:** tests de claims/403 (**supuesto**). | Portar lógica 2-tenants a CRM+Flow. Casos obligatorios: (a) B no ve datos de A; (b) **token A + tenant B ⇒ 403**; (c) schema estricto `<tenant>,extensions` sin `public`; (d) hygiene pool (`search_path` reseteado). Debe correrse al cierre de cada fase (gate). | **Verificado:** CRM spec crea `exp_spec_a/widgets`, `exp_spec_b/widgets`, valida aislamiento, fail-closed sin search_path, hygiene. **Falta:** caso (b) con token+claims. **Flow:** falta spec equivalente. |

### Track A — evo-flow + ClickHouse

| Tarea | Archivos exactos | Qué hay que cambiar (**supuesto**) | Cómo verificar |
|---|---|---|---|
| **A.1 Bypasses que quedan (re-auditoría `bypasses-fase2.md`)** | `evo-flow-community/src/modules/temporal/activities/journey-execution.activities.ts:182-183` (AppDataSource.getRepository). `evo-flow-community/src/modules/temporal/activities/campaign-execution.activities.ts:226,281,363` (app.get('DataSource').getRepository) + inputs sin tenantId (6 de 7). `evo-flow-community/src/modules/temporal/activities/nodes/scheduled-action.node.ts:56,62` (initializeDatabase sin tenant + getRepository). Nodos: `add-label.node.ts`, `remove-label.node.ts`, `update-custom-attribute.node.ts`, `transfer-journey.node.ts` (DI de servicios globales). `wait.activities.ts:49-72` (singleton cache por-tenant). | Convertir a usar `runActivityInTenantDbContext(input.tenantId, ...)` o resolver vía `dataSourceFor(tenantId)`/`getTenantDataSource()` siguiendo patrón `base.node.ts:31-56`. Para nodos con DI, resolver servicios contra DataSource por-tenant (no AppDataSource singleton). Cache `waitRegistryServiceCache` debe ser **por-tenant** (clave incluye schema/tenantKey). Campaign-execution: añadir `tenantId` a inputs faltantes donde corresponda. | **Verificado:** `bypasses-fase2.md` lista exactamente estos sitios. `base.node.ts` ya provee `initializeDatabase(tenantId?)` con tenantDataSource. `tenant-services.ts` provee `dataSourceFor`, `servicesForTenant`. **Gate:** `grep -rn "AppDataSource.getRepository\|app.get('DataSource')" src` **fuera** de `database/`, `src/multitenant/`, `base.node.ts` debe dar 0 (convertir en spec). |
| **A.2 Redis `cache_key_scope`** | `evo-flow-community/src/multitenant/register.ts` (actual solo registra `tenant_db_context`). `evo-flow-community/src/modules/cache/services/base-cache.service.ts:481-494` ya consume `cache_key_scope`. `evo-flow-community/src/evo-extension-points/registry.ts` ya registra `cache_key_scope` default. | **Plan dice:** "registrar `cache_key_scope` en `src/multitenant/register.ts` (el fix va ahí, NO en `tenant-services.ts`)". Implementar un `cacheKeyScope` que devuelva el slug/namespace por-tenant (desde CLS/tenant context). Con `MULTITENANT_SCHEMA=1`, devolver scope por tenant; sin él, comportamiento legacy (vacío). | **Verificado:** base-cache ya lee `EvoExtensionPoints.get('cache_key_scope')()`. Registry tiene default `defaultCacheKeyScope`. **Falta:** en `register.ts`, también reemplazar `cache_key_scope` con implementación que use tenant activo (p.ej. desde `TenantDbContext`/CLS o desde slug resuelto). Ver `tenant-services.ts` nota sobre namespacing Redis pendiente. |
| **A.3 ClickHouse settings** | `evo-flow-community/src/modules/processing/clickhouse/clickhouse.service.ts:51,102` (`max_bytes_ratio_before_external_group_by:0.5`). `evo-flow-community/src/modules/processing/services/batch-database-optimizer.service.ts:211` (`max_bytes_ratio_before_external_group_by = 0.5`). | Reemplazar por `max_bytes_before_external_group_by` (valor absoluto en **bytes**, env-configurable). **No pinchar versión**; verificar versión real prod antes. Nota: `clickhouse/clickhouse-server:22.8` en staging (COMPOSE) usa ratio aún; prod puede ser >=23.x. | **Verificado:** `clickhouse-fase2.md:18-24` documenta esto explícitamente. **Verificar:** leer `batch-database-optimizer.service.ts:211`. Añadir env `CLICKHOUSE_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY` con valor razonable (bytes). |
| **A.4 `createContactEventsTable()` runtime** | `evo-flow-community/src/modules/processing/clickhouse/clickhouse.service.ts:596-644` (DDL runtime). `evo-flow-community/src/multitenant/clickhouse-tenant-migration.sql` (fuente de verdad migración). | Alinear runtime con SQL: añadir columna `tenant_id LowCardinality(String) DEFAULT 'default'`, añadir índice bloom `idx_tenant_id` (MATERIALIZE opcional en runtime create? o seguir patrón), mantener `ORDER BY (occurred_at, event_type)` **SIN** `tenant_id` (backfill viable). No pasar a `ORDER BY (tenant_id, occurred_at, event_type)` en tablas existentes. | **Crítico:** `clickhouse-fase2.md:12-14` dice explícitamente runtime NO incluye `tenant_id` hoy. **Verificar:** crear tabla nueva debe incluir `tenant_id`. **Backfill:** `ALTER TABLE contact_events UPDATE tenant_id='<slug>' WHERE tenant_id='default'` — requiere ventana tranquila + `mutations_sync=2`. Writer debe escribir `tenant_id` explícito (fail-closed sin slug). |
| **A.5 Lecturas runtime ClickHouse (16 sitios)** | Listados en `clickhouse-fase2.md:28-44`: `contact-events.service.ts`, `event-search.service.ts`, `click-tracking.service.ts`, `webhook.trigger.ts`, `journey-trigger-processor.service.ts`, `contact-event-names.ts`, `segment-query-execution.service.ts`, `modular-segment-computation.service.ts`, `segment-clickhouse-query-builder.service.ts`, `clickhouse.service.ts`, `clickhouse.processor.ts`, `optimized-clickhouse.processor.ts`, `batch-processor.service.ts`, `atomic-processor.service.ts`, `single-contact-updater.service.ts`, `event-process-metrics.ts`. | Añadir filtro `AND tenant_id = <slug>` en **TODOS** los `FROM contact_events`. Slug viene por `TenantInterceptor` (HTTP) o por payload Temporal. **Sin tenant ⇒ 404/error, nunca query sin filtro** (fail-closed). `'default'` solo compat escritura legacy, nunca destino de lectura autorizada. | **Verificado:** lista completa en `clickhouse-fase2.md`. **Gate:** spec que greppee `FROM contact_events` sin `tenant_id` en el mismo query = 0 (análogo `single-account.spec.ts`). |
| **A.6 Interceptores HTTP faltantes** | `evo-flow-community/src/multitenant/tenant.interceptor.ts` existe (aplicado selectivamente?). `src/modules/events/controllers/contact-events.controller.ts:1-107`, `src/modules/events/controllers/event-search.controller.ts:1-178`, `src/modules/click-tracking/controllers/click-tracking.controller.ts:1-269` — **no usan `@UseInterceptors(TenantInterceptor)`** hoy. | Aplicar `@UseInterceptors(TenantInterceptor)` a los 3 controllers listados en `clickhouse-fase2.md:78` (`contact-events.controller.ts:22`, `event-search.controller.ts:18`, `click-tracking.controller.ts:34`). Además validar slug resuelto contra claims JWT (Fase 0.4) — el interceptor actual solo resuelve y delega; necesita pasar validación autorización por claims. | **Verificado:** interceptor existe, pero controllers no lo aplican. **Importar:** `UseInterceptors` desde `@nestjs/common`, `TenantInterceptor` desde `src/multitenant/tenant.interceptor`. |
| **A.7 Replay estricto** | `scripts/multitenant/flow-replay-tenant.mjs:37` tiene `extra: { options: '-c search_path=${schema},extensions,public' }` (**incluye `public`**). `scripts/multitenant/e2e_tenant_switch.py:75` idem. `evo-flow-community/src/multitenant/tenant-schema-context.impl.ts:53` usa **estricto** `-c search_path=${schema},extensions` (**SIN `public`**) — **correcto** ahí. | Cambiar `flow-replay-tenant.mjs:37` a `search_path=${schema},extensions` (sin `public`). También revisar `e2e_tenant_switch.py:75` (aunque es harness, debe reflejar path estricto para evidencia válida). Log "developer" si se detecta `public` en path (sugerido en PLAN 2.7). | **Crítico:** evidencia "0 fugas" anterior débil por incluir `public`. Path runtime en tenant-schema-context.impl **ya es estricto**. **Verificar:** replay debe fallar si tabla ausente en schema (fail-closed) — sin `public`, sí falla. |
| **A.8 Validación migración + commit** | Todo Track A. Correr `npx tsc -b --noEmit` y suite Jest Flow. | **DoD Track A:** leak test 2-tenants verde con schema estricto (`<tenant>,extensions` sin `public`); bypasses sin fuga; CH backfilleado en staging. | **Verificado baseline:** Flow `npx tsc -b --noEmit` exit 0; 142 suites / 1139 tests passed, 4 skipped (HANDOFF §4). |

### Fase 6 — Provision, backfill, VPS aislada

| Tarea | Archivos exactos | Qué hay que cambiar (**supuesto**) | Cómo verificar |
|---|---|---|---|
| **6.1 Retarget `provision_tenant.py` + alta dual** | `scripts/multitenant/provision_tenant.py:29-34` lee `SUPABASE_TEST_POOLER_SESSION_URL` de `.env.test`. `docker-compose.staging.yml:116-131` levanta `postgres-flow` local (5433). `evo-ai-crm-community` usa Supabase TEST vía `.env.test` en compose staging (`crm-staging` env). | Retargetear a PG local 5433 para **Flow** (o hacer configurable por `--db flow|crm`). Además **alta dual**: crear schema + fila en `public.tenants` en **ambas DBs** (`evo_community` para CRM/Auth y `evo_campaign` para Flow). Hoy `resolveTenantSchema` de Flow lee `public.tenants` de **SU PROPIA DB** (`evo_campaign`) — ver `tenants.registry.ts:29-42`. CRM/Auth leen en `evo_community`. | **Crítico:** "Por tanto hay **dos registros de tenants** por cliente..." (HANDOFF §2). `provision_tenant.py` hoy apunta solo a Supabase TEST (una DB). Debe crear registro en ambas bases. Añadir flags `--flow-db-url`, `--crm-db-url` o leer de `.env.test`/staging. **Test:** provisionar `beexa` local → ambas DBs tienen registro activo con mismo slug/schema. |
| **6.2 Backfill CH ventana tranquila** | ClickHouse: `contact_events` con `tenant_id='default'` legacy. Usar `ALTER TABLE contact_events UPDATE tenant_id='<slug>' WHERE tenant_id='default'` con `mutations_sync=2`. | Ejecutar mutation en ventana tranquila. Writer debe pasar a fail-closed sin slug (nunca `'default'` como destino). Lector SIEMPRE filtra por `tenant_id=<slug>`. | **Advertencia:** no reordenar `ORDER BY` (mantener `(occurred_at,event_type)`). Ver `clickhouse-tenant-migration.sql:12`. `mutations_sync=2` bloquea hasta completar (o usar `mutations_sync=1` según versión). Validar en staging CH 22.8 primero. |
| **6.3 VPS aislada + prueba T3** | `docker-compose.staging.yml` referencia; VPS real distinto. | Clonar proyecto Supabase + wildcard DNS. Aplicar misma configuración con envs VPS. Backups DB obligatorios (RIESGO #1 HANDOFF). | **DoD 6:** `provision_tenant.py beexa` funcional en staging; stack aislado VPS con backups. |

---

## 3. Cómo levantar cada servicio de mi carril en local y cómo correr sus tests

### 3.1 evo-auth-service-community (Fase 0)

**Requisitos:** Ruby/Rails (Gemfile presente). En root super-repo hay `docker-compose.staging.yml` pero auth también corre standalone? Revisar `Makefile`/`docker/Dockerfile` en auth.

**Levantar local (desarrollo):**
```bash
cd evo-auth-service-community
# Instalar dependencias
bundle install
# Preparar DB (usa config/database.yml)
rails db:prepare  # o db:create db:migrate
# Levantar servidor
rails s -p 3011  # según puertos HANDOFF
```
O vía Docker compose raíz: `make start` / servicios auth.

**Variables relevantes (Fase 0):**
- `COOKIE_DOMAIN` (override) — hoy leído
- `AUTH_ALLOWED_HOSTS` (**crear/usar** en Fase 0.2)
- `DOORKEEPER_JWT_SECRET_KEY`, `DOORKEEPER_JWT_ALGORITHM`, `DOORKEEPER_JWT_ISS`, `DOORKEEPER_JWT_AUD`
- `FRONTEND_URL`

**Tests:**
```bash
cd evo-auth-service-community
bundle exec rspec spec/concerns/auth_helper_cookie_domain_spec.rb
bundle exec rspec spec/lib/evo_extension_points_spec.rb
# tests multitenant/auth cuando existan
bundle exec rspec --fail-fast
```
**Verificado:** spec cookie_domain existe y scaffold fallback.

### 3.2 evo-flow-community (Track A — principal)

**Requisitos:** Node.js + pnpm/npm (ver `package.json`). PostgreSQL flow local (puerto 5433 vía compose staging) o propio. ClickHouse opcional para tests unitarios.

**Levantar local (dev):**
```bash
cd evo-flow-community
npm install  # o pnpm
# Configurar .env (ver .env.example). Claves mínimas para tests: EVOAI_CRM_API_TOKEN, EVOAI_CRM_BASE_URL, POSTGRES_DB_*
# Modo single-account (sin multitenant)
npm run dev:single
# Modo multitenant overlay
MULTITENANT_SCHEMA=1 npm run dev:api
```

**Con staging docker-compose (aislado):**
```bash
cd /home/maldito/beexa/crm/evo-crm-community
docker compose -f docker-compose.staging.yml up -d postgres-flow clickhouse-staging redis-staging
# luego flow local o flow-staging con profile flow
docker compose -f docker-compose.staging.yml --profile flow up -d flow-staging
```

**Tests:**
```bash
cd evo-flow-community
# Typecheck (obligatorio)
npx tsc -b --noEmit

# Jest completo (baseline verde)
EVOAI_CRM_API_TOKEN=test-token EVOAI_CRM_BASE_URL=http://localhost:3000 \
POSTGRES_DB_HOST=localhost POSTGRES_DB_USERNAME=flow POSTGRES_DB_PASSWORD=x \
POSTGRES_DB_DATABASE=flow npx jest --maxWorkers=1

# Con cobertura si necesario
npx jest --maxWorkers=1 --no-coverage
```
**Baseline verificado:** 142 suites / 1139 passed / 4 skipped. `single-account.spec.ts` verde (guard FR44).

**Replay por schema (tooling):**
```bash
cd /home/maldito/beexa/crm/evo-crm-community
# requiere dist compilado
cd evo-flow-community && npm run build && cd ..
node scripts/multitenant/flow-replay-tenant.mjs demo_inmo
```
**Nota:** cambiar path estricto tras fix A.7.

### 3.3 evo-ai-crm-community (soporte leak test)

**Rails local:**
```bash
cd evo-ai-crm-community
bundle install
rails db:prepare
rails s -p 3010
# tests
bundle exec rspec spec/multitenant/tenant_isolation_spec.rb  # ya existe, debe seguir verde
```

### 3.4 Provisioning/Fase 6 tooling

**`provision_tenant.py`:**
```bash
cd /home/maldito/beexa/crm/evo-crm-community
python3 scripts/multitenant/provision_tenant.py demo_inmo [--owner <uuid> --plan free]
# test fallo simulado
python3 scripts/multitenant/provision_tenant.py demo_x --fail-at seed
```
**Importante:** hoy apunta a Supabase TEST (`.env.test`). Tras 6.1, retargetear a DBs locales (CRM 5432 vía Supabase direct? o staging CRM usa Supabase TEST; Flow usa 5433 local).

**`e2e_tenant_switch.py`:**
```bash
cd /home/maldito/beexa/crm/evo-crm-community
python3 scripts/multitenant/e2e_tenant_switch.py
# exige 0 fugas, 0 search_path residual, 404 fail-closed
```
**Nota:** usa path con `public` hoy (A.7).

---

## 4. Qué espero recibir del carril de Franco y qué le tengo que entregar yo (según CONTRACT.md)

**Fuente:** `CONTRACT.md` §§1-6; `PLAN-EJECUCION.md` §4 (Track C/D relaciones); `HANDOFF.md` §5.

### Lo que **YO (Fase 0 + Track A + Fase 6)** tengo que entregar

| Entregable | Destino | Estado esperado |
|---|---|---|
| **Cookie host-scope + AUTH_ALLOWED_HOSTS** (0.1-0.2) | Auth + Frontend/infra (consumidores cookies) | `_evo_rt` host-scoped a auth host; `_evo_at` validado (no romper consumidores); `AUTH_ALLOWED_HOSTS` aplicado con 403 coherente. |
| **Claims `tenants[]` en JWT** (0.3) | Todos los backends que validan JWT (CRM, Flow, Processor) | JWT incluye `tenants[]` (slugs activos) firmados. Re-emisión documentada. |
| **Regla 403 por claims** (0.4) | Flow `TenantInterceptor`, CRM `TenantSwitcher`, Auth `validate/me` | Si slug resuelto ∉ `tenants[]` ⇒ 403. Jamás `'default'`. Caso token-A/tenant-B cubierto. |
| **Overlay Flow schema-per-tenant completo** (A.1-2, A.6-8) | Flow runtime + consumers Temporal | Bypasses corregidos; `cache_key_scope` registrado; interceptores aplicados a 3 controllers; replay estricto; gate specs verdes. |
| **ClickHouse tenant_id completo** (A.3-5) | Flow/analytics | Settings corregidos (absoluto bytes); runtime `createContactEventsTable` alineado con migración (tenant_id); **TODAS** lecturas con `tenant_id=<slug>`; backfill planificado + writer fail-closed. |
| **Tooling replay/e2e estricto** (A.7) | QA/staging | `flow-replay-tenant.mjs` sin `public` en path; `e2e_tenant_switch.py` con path estricto (re-hacer evidencia). |
| **Esqueleto leak test 2-tenants** (0.6) | CRM+Flow (+ Auth) | Spec dinámico con caso 403 por claims; corre al cierre de cada fase. |
| **Fase 6** (6.1-3) | Provisioning/VPS | `provision_tenant.py` con alta dual (CRM+Flow DBs); backfill CH ventana tranquila; guía VPS aislada + backups. |

### Lo que **ESPERO recibir del carril de Franco** (según contrato)

Basado en `CONTRACT.md`, `PLAN-EJECUCION.md` (Track B/C/D) y `HANDOFF.md`:

| De Franco | Para qué lo necesito | Notas |
|---|---|---|
| **Track B — Processor (paralelo)** | Consumo slug unificado + RLS coherente. | Migración 13 tablas (12 + `users`), RLS `tenant_slug = current_setting('app.tenant_slug')`, sesiones tenant-scoped (`SessionLocal()` propios), aserción arranque post-`create_all`, fix `NameError: request` en `*_routes.py`, tests legacy reescritos. Necesito que Processor acepte/valide `tenant_slug` igual formato (slug pelado). |
| **Track C — Frontend + Auth UI** | Selector de tenants desde `tenants[]` JWT; envío `X-Tenant-Slug`. | `validate`/`me` devuelven `tenants[]` (yo añado claims+respuesta). Front debe mostrar solo esos, enviar header `X-Tenant-Slug` en requests API. Encaje con claims Fase 0. |
| **Track D — CRM/Auth (paralelo)** | Schedulers self-dispatch + registro dual coherente + `status_statement`. | Scheduler `account/conversations_resolution_scheduler_job.rb` y `whatsapp/templates_sync_scheduler_job.rb` (patrón TenantDispatch). Decisión registro dual (`public.tenants` evo_community vs evo_campaign) — necesito definición clara para que `provision_tenant.py` (6.1) haga alta dual correctamente. `status_statement` fall-resuelto para login verdadero con múltiples tenants. |
| **Decisión registro dual** (transversal) | Mi Fase 6.1 | ¿Quién es dueño de sincronización? Hoy Flow resuelve contra SU DB. CRM/Auth contra evo_community. Provision debe escribir ambos o hay sync? CONTRACT deja "sincronización entre ambos a resolver en provisioning" (§2 HANDOFF). Necesito regla acordada. |

**Entrega cruzada:** Yo entrego claims JWT + regla 403 por claims; Franco consume `tenants[]` (frontend) y valida membresía/tenant_slug (Processor/CRM) conforme a CONTRACT.

---

## 5. Trampas conocidas (pooler de Supabase, create_all, cookie compartida, orden de ClickHouse) con el archivo donde viven

| Trampa | Archivo(s) donde vive | Explicación (verificado/supuesto) | Mitigación |
|---|---|---|---|
| **Pooler Supabase + prepared statements** | `evo-ai-processor-community` (migración pgbouncer), `evo-ai-crm-community/docker-compose.yml` (menciona pooler 6543 vs 5432). `HANDOFF.md: §4` "el Processor en dev apunta a schema `processor` con `sslmode=require`; en la VPS corre directo a 5432 (no al pooler 6543) porque Supavisor deja prepared statements huérfanos (`DuplicatePreparedStatement`)". | Usar transaction-mode vs session-mode afecta prepared statements. SET LOCAL vía `after_begin` debe funcionar en session-mode; en transaction-mode con ciertas configuraciones hay efectos. | Verificar `DB_PGBOUNCER`/modo en cada servicio. En staging CRM usa `DB_PGBOUNCER: "false"` (session-mode) — OK. No asumir transaction-mode. |
| **`create_all` con RLS / tablas stub** | `evo-ai-processor-community/src/main.py:216-226` excluye `users` (`_owned_by_other_services = {"users"}`) para no romper auth. `HANDOFF.md §6 #4`: "create_all con RLS: ópero DDL → ENABLE/FORCE → CREATE POLICY, por tabla". | Processor NO debe crear `users` (propiedad auth). Al añadir RLS (Track B), `create_all` **no gestiona RLS/políticas** — requiere migración SQL explícita + aserción arranque. | Track B debe: migración con `ENABLE ROW LEVEL SECURITY; FORCE ROW LEVEL SECURITY;` + policies; **NO** confiar en `create_all` para RLS. Aserción post-`create_all` verifica `relrowsecurity=true` en 13 tablas. |
| **Cookie compartida entre dominios** | `evo-auth-service-community/app/controllers/concerns/auth_helper.rb:122-155` (`cookie_domain`), `auth_controller.rb:89-102`. `HANDOFF.md §6 #10-11`: host compartido `beexa-auth.evonectech.com` sirve todos tenants → host-scope detiene compartir entre subdominios; aislamiento real = **slug ∈ `tenants[]` ⇒ 403**. | Host-scopear a subdominio específico (0.1) es necesario pero **insuficiente** si no hay validación por claims. Un usuario con cookie válida pero sin membresía del tenant resuelto **debe** recibir 403. | Implementar 0.4 SIEMPRE junto con 0.1. Nunca confiar solo en cookie/domain. Validar contra JWT claims en cada backend que resuelve tenant. |
| **Orden de ClickHouse: `ORDER BY` vs backfill** | `evo-flow-community/src/modules/processing/clickhouse/clickhouse.service.ts:625` `ORDER BY (occurred_at, event_type)` **SIN** `tenant_id`. `src/multitenant/clickhouse-tenant-migration.sql:12`: "NO se toca ORDER BY en tablas existentes... En provisioning fresco se recomienda ORDER BY (tenant_id, occurred_at, event_type)". | **CRÍTICO:** `ALTER TABLE ... UPDATE tenant_id='...' WHERE tenant_id='default'` **funciona** con ORDER BY actual. Si se añade `tenant_id` al sort key **ANTES** o mal, mutations UPDATE sobre columnas no-key son más restrictivas? CH bloquea UPDATE sobre columnas que formen parte de la clave de ordenación primaria en ciertos casos. Mantener **sin** `tenant_id` en ORDER BY existente. Solo para **nuevas** tablas considerar `(tenant_id, occurred_at, event_type)`. | No cambiar ORDER BY de `contact_events` existente. Runtime debe crear con mismo ORDER BY hoy. Tras backfill completo y validación, evaluar reordering **únicamente** con `ALTER TABLE ... MODIFY ORDER BY` (requiere recrear partes) — **no hacer ahora**. |
| **Search path estricto vs evidencia débil** | `evo-flow-community/src/multitenant/tenant-schema-context.impl.ts:53` → `search_path=${schema},extensions` (**OK estricto**). `flow-replay-tenant.mjs:37` → **incluye `public`** (trampa A.7). `e2e_tenant_switch.py:75` → **incluye `public`**. `TenantSwitcher` CRM: `switch_to` usa `[tenant.schema_name, 'extensions'].join(', ')` (**OK estricto**, `:76` CRM). | Incluir `public` en runtime reabre leak silencioso: tabla ausente en tenant resuelve en `public`. La evidencia "0 fugas / 160 reqs" previa usa path con `public` → **invalida** afirmación. Debe rehacerse con path estricto. | Corregir A.7 inmediatamente. Añadir spec guard: grep que prohíba `public` en search_path runtime para tenants (salvo `SET search_path TO <tenant>,extensions` explícito). |
| **`SET LOCAL` exige txn abierta** | `HANDOFF.md §6 #2`: "SET LOCAL exige txn abierta → evento `after_begin` de SQLAlchemy, no `on_connect`". Processor (Track B) usa SQLAlchemy sessions. | Si se intenta `SET LOCAL app.tenant_slug` fuera de transacción, no aplica por conexión? `SET LOCAL` solo dura hasta fin de transacción actual. `on_connect` ejecuta al adquirir conexión del pool (puede vivir entre requests/jobs) → riesgo fugas. Usar `after_begin` listener. | Track B: en `SessionLocal()` tenant-scoped, añadir listener `after_begin` que haga `execute("SET LOCAL app.tenant_slug = %s", [slug])` si hay tenant activo. **NO** usar `event.listen(engine, 'connect', ...)` para SET LOCAL. |
| **Rol app con BYPASSRLS** | `HANDOFF.md §6 #3`: "Rol de app con `BYPASSRLS` anula RLS silenciosamente". Processor Track B. | Cualquier conexión con rol que tenga `BYPASSRLS` ignora políticas RLS. | Verificar rol DB usado por Processor (`app.tenant_slug` + RLS). Debe ser rol **SIN** `BYPASSRLS`. Rol admin separado solo para migraciones/provisioning. Añadir chequeo/alerta si detectado. |
| **psycopg2 crudo en google_calendar tools** | `evo-ai-processor-community/src/services/adk/tools/google_calendar/*.py` (create/edit/check/cancel) — referencias en HANDOFF §6 #5 y Track B. | psycopg2 crudo **no hereda** search_path/variables SQLAlchemy; RLS con `current_setting('app.tenant_slug')` **no se aplica** a menos que se seteé GUC explícito en esa conexión. Resultado: 0 filas silencioso o cross-tenant leak si se olvida filtro. | Track B: en tools google_calendar con psycopg2, hacer `conn.execute("SET LOCAL app.tenant_slug = %s", (tenant_slug,))` (con txn) o `SET` por conexión + validar. Mejor: pasar por mismo mecanismo tenant-scoped o añadir `AND tenant_slug=%s` explícito (defensivo). |
| **Falsificación tenant (X-Tenant-Slug)** | `Flow TenantInterceptor:22-57` lee header/subdominio sin validar contra claims. `CRM TenantSwitcher:60-66` lee ENV/TENANT_SLUG/header/subdominio. `CONTRACT §4` exige validación contra `tenants[]`. | Header **nunca** se confía tal cual. Usuario tenant A puede enviar `X-Tenant-Slug: tenant_b`. Sin cruce con claims JWT → acceso indebido. | **0.4 obligatorio.** Resolver slug autoritativamente, saneado, contra registro; **comparar con `tenants[]` del token** → 403 si no presente. Esto es el núcleo del leak test caso (b). |
| **ClickHouse kafka_queue + MV** | `src/multitenant/clickhouse-tenant-migration.sql:23-31`: MV debe recrearse con `tenant_id` en SELECT. `clickhouse.service.ts` crea kafka integration/MV runtime. | Crear MV con `SELECT ..., tenant_id, ... FROM contact_events_kafka_queue ...` pasando `tenant_id`. Runtime actual no añade `tenant_id` a tabla runtime (A.4) → MV también debe incluirlo. | Tras A.4, actualizar `createKafkaIntegration`/MV DDL en `clickhouse.service.ts` para incluir `tenant_id`. Validar en staging (Kafka Engine no corre clickhouse-local). |
| **Cache singleton por-tenant (wait.activities)** | `evo-flow-community/src/modules/temporal/activities/wait.activities.ts:46,55,91` usa `waitRegistryServiceCaches = new Map<string, any>()` con clave `schema` derivada. | Wrapper `runActivityInTenantDbContext` ya envuelve, pero si se cachea el servicio construido con manager global vs tenant, puede reutilizarse incorrectamente? Clave por schema OK; pero asegurar que al cambiar tenant no se mezcle. También `tenant-services.ts` cachea por `tenantKeyOf(schema)` — coherente. | Verificar: `createWaitRegistryService` obtiene schema via `runActivityInTenantDbContext` → usa `tenantKeyOf(schema)`? Revisar líneas 48-100 de wait.activities.ts: `const schema = (ds.options as any)?.schema || 'public'; const hit = waitRegistryServiceCaches.get(schema);` OK si ds es tenant-specific. |
| **`flow-replay-tenant.mjs` hardcoded paths** | `scripts/multitenant/flow-replay-tenant.mjs:7,17` usa rutas absolutas `/home/maldito/beexa/crm/evo-crm-community/...` | Solo funciona en este entorno. Documentar o parametrizar. | **Baja prioridad** (tooling local). No bloqueante. |

---

## 6. Orden de trabajo recomendado, con un chequeo al final de cada paso

**Principio:** Fase 0 **en serie** (bloquea claims/403). Track A **serie** (bloqueante 2.4). B/C/D **paralelos desde Día 1** (no toco ahora). Fase 6 **serie** al cierre.

### PASO 1 — Fase 0: Base auth transversal (1.5d estimado) **[SERIE]**
- [ ] **1a.** Revisar consumidores `_evo_at` (auth_helper + usages). `grep -rn "_evo_at" evo-auth-service-community/app | grep -v set_access_token_cookie\|delete`
- [ ] **1b.** 0.1 Cookie host-scope: modificar `auth_helper.rb` (set_refresh_cookie, set_access_token_cookie, cookie_domain) + `auth_controller.rb` delete. Mantener compat dev/ngrok.
- [ ] **1c.** 0.2 `AUTH_ALLOWED_HOSTS`: añadir validación request.host + Origin en emisión/borrado cookies + endpoints relevantes. Crear initializer si conviene.
- [ ] **1d.** 0.3 Claims `tenants[]` en JWT: poblar en `doorkeeper.rb token_payload` (o vía `TokenClaims` extension point). Leer `tenants_for_user` con `status='active'`.
- [ ] **1e.** 0.4 403 por claims: **Flow** `TenantInterceptor` (cruzar slug resuelto con claims JWT) — **necesitará extraer claims** (desde Authorization Bearer o cookie). **CRM** reforzar `TenantSwitcher` + `verify_tenant_membership!` con validación slug∈claims? O al menos exigir membresía+coherencia. **Auth** `validate` ya devuelve tenants; añadir guard.
- [ ] **1f.** 0.6 Esqueleto leak test 2-tenants: ampliar CRM spec con caso token-A/tenant-B ⇒ 403 (cuando claims existan). Crear spec base Flow.

**Chequeo 1 (fin Fase 0):**
- [ ] `spec/concerns/auth_helper_cookie_domain_spec.rb` verde.
- [ ] JWT contiene `tenants[]` en payload (inspección lógica + test).
- [ ] Leak test: token-A/tenant-B ⇒ 403 pasa (CRM mínimo).
- [ ] Cookies host-scoped en prod path; dev/ngrok sin romper.
- [ ] `AUTH_ALLOWED_HOSTS` rechaza hosts inválidos.

### PASO 2 — Track A: Bypasses + Redis (sub-bloque, paralelo a entender 2.4) **[SERIE dentro Track A]**
- [ ] **2a.** Auditar grep bypasses: `cd evo-flow-community && grep -rn "AppDataSource.getRepository\|app.get('DataSource')" src | grep -v __spec__ | grep -v database/ | grep -v multitenant | grep -v "base.node.ts"`
- [ ] **2b.** Corregir journey-execution.activities.ts:182-183 → envolver con `runActivityInTenantDbContext(input.tenantId, ...)` o usar `servicesForTenant`.
- [ ] **2c.** Corregir campaign-execution.activities.ts:226,281,363 + añadir `tenantId` a inputs faltantes (ver `bypasses-fase2.md`).
- [ ] **2d.** scheduled-action.node.ts:56,62 → pasar `tenantId` a `initializeDatabase(input.tenantId)`.
- [ ] **2e.** 4 nodos (add/remove/update-custom-attribute/transfer-journey): resolver servicios contra DataSource por-tenant (no DI singleton global). Mirar patrón `base.node.ts` + `tenant-services.ts`.
- [ ] **2f.** wait.activities.ts: cache por-tenant (clave schema/tenant). Verificar Map key correcto.
- [ ] **2g.** A.2 Redis `cache_key_scope`: registrar implementación en `register.ts` (además de `tenant_db_context`). Debe devolver scope por tenant activo con `MULTITENANT_SCHEMA=1`.

**Chequeo 2:**
- [ ] Grep bypasses = 0 fuera de whitelist.
- [ ] `npx tsc -b --noEmit` OK.
- [ ] Tests relacionados journeys/campaigns verdes.

### PASO 3 — Track A: ClickHouse 2.4 (BLOQUEANTE) **[SERIE]**
- [ ] **3a.** A.3 Settings: reemplazar `max_bytes_ratio_before_external_group_by` por `max_bytes_before_external_group_by` (bytes, env) en `clickhouse.service.ts:51,102` y `batch-database-optimizer.service.ts:211`. Leer versión CH real prod (solo lectura) — hoy staging 22.8.
- [ ] **3b.** A.4 `createContactEventsTable()`: añadir `tenant_id LowCardinality(String) DEFAULT 'default'` + índice bloom `idx_tenant_id` + `MATERIALIZE INDEX IF EXISTS idx_tenant_id`? Mantener `ORDER BY (occurred_at,event_type)`. NO añadir `tenant_id` al ORDER BY existente.
- [ ] **3c.** Actualizar `createKafkaIntegration`/MV DDL para incluir `tenant_id` (kafka_queue + MV SELECT).
- [ ] **3d.** A.5 Auditar 16 sitios: añadir `AND tenant_id = {slug}` con parámetro. Crear spec guard "no contact_events sin tenant_id".
- [ ] **3e.** Plan backfill: script/steps `ALTER TABLE contact_events UPDATE tenant_id='<slug>' WHERE tenant_id='default'` con `mutations_sync=2`, ventana tranquila. Writer fail-closed sin slug.

**Chequeo 3 (2.4 completo):**
- [ ] DDL runtime incluye `tenant_id`.
- [ ] 0 lecturas sin filtro tenant_id (guard spec pasa).
- [ ] Settings corregidos.
- [ ] Backfill documentado/testeado en staging CH 22.8.

### PASO 4 — Track A: Interceptores + replay estricto + cierre **[SERIE]**
- [ ] **4a.** A.6 Aplicar `@UseInterceptors(TenantInterceptor)` a: `contact-events.controller.ts`, `event-search.controller.ts`, `click-tracking.controller.ts`. Añadir imports.
- [ ] **4b.** A.7 Corregir `flow-replay-tenant.mjs:37` → quitar `public` (`search_path=${schema},extensions`). Corregir `e2e_tenant_switch.py:75` idem. Añadir validación/log.
- [ ] **4c.** Integrar validación claims en `TenantInterceptor` (0.4) — extraer token JWT, obtener `tenants[]` claims (decodificar o validar vía Auth? o pasar contexto). Definir mecanismo.
- [ ] **4d.** Correr full suite Flow + typecheck.
- [ ] **4e.** Ejecutar leak test 2-tenants CRM+Flow con schema estricto.

**Chequeo 4 (DoD Track A):**
- [ ] `npx tsc -b --noEmit` limpio.
- [ ] 142 suites / 1139 passed / 4 skipped (o equivalente tras cambios).
- [ ] Leak test 2-tenants verde: B no ve A + token-A/tenant-B ⇒ 403.
- [ ] Path estricto `<tenant>,extensions` sin `public` en runtime+replay.
- [ ] Interceptores aplicados.

### PASO 5 — Fase 6: Provision + dual + VPS **[SERIE tras A]**
- [ ] **5a.** 6.1 Refactor `provision_tenant.py`: soporte DBs dual (CRM `evo_community` + Flow `evo_campaign`). Leer URLs desde `.env.test`/args. Mantener idempotencia + limpieza.
- [ ] **5b.** 6.2 Backfill CH en staging ventana tranquila (con `mutations_sync=2`).
- [ ] **5c.** 6.3 Plan VPS aislada (Supabase clone/wildcard DNS), checklist backups DB (§10 AGENTS). Smoke test T3.
- [ ] **5d.** Verificar `docs/multitenant/MAPA_FEDERICO.md` completo (este doc) — **ya creado**.

**Chequeo 5 (DoD Fase 6):**
- [ ] `provision_tenant.py beexa` crea registro en ambas DBs, schema Flow creado via replay.
- [ ] CH backfilleado sin rotura ORDER BY.
- [ ] VPS aislada documentada + backups activos.

---

## Dudas abiertas al final

| # | Duda (**supuesto/no verificado**) | Acción a resolver |
|---|---|---|
| **D1** | **Extracción claims JWT en Flow interceptor.** `TenantInterceptor` corre en NestJS HTTP. ¿Cómo obtener `tenants[]` claims? Desde `Authorization: Bearer <token>` decodificando JWT con mismo secreto (`DOORKEEPER_JWT_SECRET_KEY`)? O validar vía Auth service `/validate` (sync HTTP)? Decodificar localmente es más performante pero requiere secreto compartido. ¿Auth comparte secreto JWT con Flow? | Confirmar secret compartido o estrategia validación. En Auth `decode_jwt_token` usa `Doorkeeper::JWT.configuration.secret_key`. Flow necesita mismo valor. |
| **D2** | **Validación 403 por claims en CRM TenantSwitcher vs `verify_tenant_membership!`.** `verify_tenant_membership!` ya chequea membresía por email (`TenantMembership.exists?` con `user.email`). Claims `tenants[]` trae **slugs**. ¿Coincide siempre? Además, ¿aplicar 403 por "slug no en claims" **antes** o **además** de membresía? CONTRATO dice "slug resuelto NO está en `tenants[]` del JWT ⇒ 403". Eso es autorización a nivel token (más fuerte). | Definir precedencia: primero validar slug ∈ claims[] → 403 si no; luego validar membresía activa → 403 si no. Ambas deben pasar. |
| **D3** | **Consumidores `_evo_at`** (Fase 0.1). Busqué referencias en auth app: solo set/delete/set en helper+controller. ¿Frontend/backend otros servicios leen cookie `_evo_at`? Riesgo romper auth si host-scopeamos mal. | Verificar en Frontend (`evo-ai-frontend-community`) y otros servicios si acceden a `_evo_at`. Ejecutar grep global: `grep -rn "_evo_at" evo-ai-frontend-community src 2>&1 | head -15`. |
| **D4** | **Registro dual dueño/sincronización** (6.1). Flow lee `public.tenants` de **DB flow** (`evo_campaign`). CRM/Auth leen de **evo_community`. `provision_tenant.py` debe escribir **ambos** con mismo `slug,schema_name,status,plan`. ¿Crear membership también en ambas? Membership vive en `public.tenant_memberships` de evo_community (CRM/Auth). En evo_campaign **NO existe** tabla memberships hoy. ¿Flow necesita memberships? `tenants.registry.ts` solo lee `tenants` (no memberships). | Acordar esquema dual: **escribir `tenants` en ambas DBs**. **`tenant_memberships` solo en `evo_community`** (donde Auth/CRM lo usan). `provision_tenant.py` debe hacer 2 escrituras independientes con transacciones por DB. |
| **D5** | **ClickHouse versión prod vs settings** (A.3). Staging usa `clickhouse/clickhouse-server:22.8` (tiene `max_bytes_ratio_before_external_group_by`). Prod: "ClickHouse al 90%+ CPU" (HANDOFF §8) pero **no sabemos versión exacta**. En >=23.x ratio **no existe** → setting falla. Debo verificar versión real prod **solo lectura** antes de aplicar. | Ejecutar en prod VPS: `docker exec -it evo-crm-community-clickhouse-1 clickhouse-client --query "SELECT version()"` (o equivalente). Documentar resultado. |
| **D6** | **Path estricto en tooling afecta evidencia previa** (A.7). `e2e_tenant_switch.py` simula TenantSwitcher CRM (que **ya es estricto** sin `public`). Cambiarlo a estricto es correcto. ¿Rehacer corrida histórica "0 fugas 160 reqs"? HANDOFF dice "evidencia débil a rehacer". | Aceptar corrección: rehacer `e2e_tenant_switch.py` con path estricto y validar pasa. También `flow-replay-tenant.mjs` corregir. |
| **D7** | **Processor `users` 13ª tabla RLS + create_all exclusion** (Track B ref). Processor excluye `users` de `create_all` (`main.py:220-226`). Si se añade `users` a las 13 con RLS, migración debe crearla con RLS; **nunca** vía `create_all`. ¿`users` existe en schema tenant o es global? `plans/features/plan_features` vs tenant — revisar. | Track B debe aclarar: ¿`users` es tabla tenant-scoped (RLS por `tenant_slug`) o global compartida? HANDOFF dice "13 tablas: las 12 de `processor-tenant-migration.sql` + **`users`** (la migración NO la cubre; revisar...)". **Crítico** definir alcance. |
| **D8** | **Claims extracción en Flow interceptor sin romper single-account**. Interceptor hoy solo se aplica selectivamente? Controllers 3 lo llevarán (A.6). ¿Aplicarlo globalmente? TenantInterceptor lanza 404 si no hay slug → rompe rutas sin tenant. Hoy opt-in por `@UseInterceptors` (comentario interceptor). Bien. Pero validación claims requiere token presente. ¿Requests sin token en esas rutas multitenant deben ser 403? Depende auth. | Mantener **opt-in** (no global). Solo rutas tenant-aware llevan interceptor. Para esas rutas, exigir token válido con claims que incluyan slug resuelto → 403 coherente con contrato. |
| **D9** | **`X-Tenant-Slug` precedence vs subdominio**. `TenantInterceptor.slugFromRequest`: primero header `x-tenant-slug`, luego subdominio (`parts[0]` si >=3). `TenantSwitcher` CRM: ENV/TENANT_SLUG > header > subdominio. Misma lógica razonable. ¿Prohibir header en prod? Origen puede falsificarse si no hay validación claims (D8). | Con 0.4 (claims), header permitido pero **validado**: slug debe estar en `tenants[]`. Si no, 403. Evita falsificación. No necesario prohibir header, basta con validar. |
| **D10** | **Backfill CH: `mutations_sync=2` comportamiento**. En CH, `mutations_sync` controla sincronización mutations. Valor 2 = espera a que mutation termine (síncrono). Requiere ventana tranquila (baja carga). ¿Tamaño tabla `contact_events` hoy? VPS vacío (HANDOFF §4: 1 conversation, etc.) pero prod puede tener datos. | Verificar count: `SELECT count() FROM contact_events WHERE tenant_id='default'` antes de backfill. Si 0, trivial. Si grande, planificar ventana. |
| **D11** | **JWT re-emisión al añadir claims `tenants[]`** (0.3). Tokens existentes (emitidos antes del cambio) **no tendrán** `tenants[]`. Consumidores que **exijan** claims fallarán? O hacer retrocompatible: si falta `tenants[]`, tratar como vacío → 403 en rutas tenant-aware? Más seguro. | Emisión nueva sí incluye claims. Validación: si ruta es tenant-aware y slug resuelto no está en claims (vacío) ⇒ 403. No romper login/refresh, solo autorización tenant-aware. |

---

**Separación:** Todo lo marcado **(supuesto)** es lo que **aún no implementé**; lo demás está **verificado** leyendo el código actual en `exp/multitenant-a2`. Este mapa es **solo lectura** y cubre íntegramente mi carril.