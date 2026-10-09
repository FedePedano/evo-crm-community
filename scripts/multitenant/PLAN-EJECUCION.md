# PLAN DE EJECUCIÓN — Multitenant A2

> Versión final: 2026-10-09. Super-sede del plan previo: corrige 6 críticas
> (cookie/jwt/allow-list, leak test dinámico, backfill CH, Processor completo,
> Fase 6 estimada, Processor en paralelo) + 3 cierres (falsificación de tenant,
> host-scope de cookie, CONTRACT de slug) + 2 detalles (UPDATE/ORDER BY de CH,
> esqueleto de leak test agendado).
> Contrato de nombres/seguridad: `CONTRACT.md`. Documento maestro: `HANDOFF.md`.

---

## Decisión arquitectónica (resumen)

- **Un slug, un formato**: canónico en `public.tenants.slug`; derivados leídos
  del registro: PG `cliente_<slug>`, CH `tenant_id=<slug>`, Processor
  `tenant_slug=<slug>`, Redis scope `<slug>`. Ver `CONTRACT.md`.
- **Aislamiento por claims, no por cookie**: Auth vive en un único host
  compartido (`beexa-auth.evonectech.com`). La cookie se host-scopea a ese
  subdominio (anti compartir entre subdominios), pero el límite tenant real es
  la validación **slug ∈ `tenants[]` del JWT ⇒ si no, 403**.
- **Backfill CH viable**: `createContactEventsTable` ordena por
  `(occurred_at, event_type)` SIN `tenant_id` → `ALTER TABLE ... UPDATE` sobre
  `tenant_id` funciona. No adoptar `ORDER BY (tenant_id,...)` en tablas
  existentes o el backfill rompería. Mutation = ventana tranquila
  (`mutations_sync=2`).
- **Dependencias**: el único bloqueo en serie es lo compartido con ClickHouse
  (2.4). Processor (B), Frontend/Auth (C) y CRM/Auth (D) arrancan en paralelo
  desde el Día 1 (Processor depende solo del contrato del slug).

---

## Fase 0 — Seguridad auth transversal + esqueleto leak test (1.5d, en serie)

1. Cookie host-scope: `_evo_rt` de inmediato (path `/api/v1/auth`); verificar
   consumidores de `_evo_at` antes de host-scopearla. Quitar el
   `COOKIE_DOMAIN=.evonectech.com` compartido. `secure` + `same_site` estricto.
2. `AUTH_ALLOWED_HOSTS`: validar `request.host` antes de emitir/borrar cookies
   (anti DNS-rebinding/confused-deputy); validar `Origin`.
3. **Claims `tenants[]` en JWT** (mapper de claims Doorkeeper; re-emisión en
   deploy, tokens cortos = bajo riesgo).
4. **403 por claims** (contrato §4): `Flow TenantInterceptor`, `CRM
   TenantSwitcher`, `Auth me/validate` — slug resuelto (host o
   `X-Tenant-Slug`), saneado `^[a-z0-9_]+$`, resuelto contra el registro, no en
   `tenants[]` ⇒ 403 (jamás `'default'`).
5. `CONTRACT.md` commitado (creado, ver arriba) + `PLAN-EJECUCION.md` (este).
6. **Esqueleto leak test 2-tenants**: spec dinámica (modelo
   `tenant_isolation_spec.rb` 5/5 de CRM), portada a CRM+Flow, con el caso
   "token de A + tenant B ⇒ 403". Es el **gate al cierre de cada fase**.

**DoD**: leaks falso-negativo del caso token-A/tenant-B; cookies host-scoped;
claims presentes y verificados (403) en los 3 backends.

---

## Track A — Flow + ClickHouse (5.5-6.5d, en serie; único bloqueante es 2.4)

Pendientes de la fase 2 (basado en `bypasses-fase2.md` / `clickhouse-fase2.md`):

- **2.1** bypasses reales: `campaign-execution`: 6 inputs sin tenantId; `entity
  idCampaign entityId` (successData); `onRetry`; `metrics (idCampaign)`;
  candidate flow; PQ candidates. (PROBADO anillos activos)
- **2.2** guards estáticos (grep): prohibición columna owner; desnudos en
  `selectSkeleton`; prohibición de `tenant_id` en expected; guard 403.
- **2.3** Redis `cache_key_scope` (CLS) → `base-cache.service.ts:484`.
- **2.4** ClickHouse (bloqueante): setting en `clickhouse.service.ts:51,102`;
  `batch-database-optimizer.service.ts:211`; **`createContactEventsTable` sin
  `tenant_id` (596-644) → nuevo DDL con tenant_id y `ORDER BY` SIN tenant_id**.
  **Backfill**: `ALTER TABLE contact_events UPDATE tenant_id='<slug>'
  WHERE tenant_id='default'` en ventana tranquila, `mutations_sync=2`; writer
  siempre slug explícito (final), fail-closed sin slug (no `'default'`);
  lector: `tenant_id = <slug>`.

  ojo: `contact_events_kafka_queue` + MV (recrear SELECT pasando tenant_id) —
  validar en staging (Kafka Engine no corre en clickhouse-local).

- **2.5** lecturas runtime (16 sitios según checklist): `userId`,
  `contact_or_anonymous_id`, `message_id` sin filtro → añadir.
- **2.6** `TenantInterceptor` en 3 controllers (http/get-segment/get-Tenant);
  ya en Flyway.
- **2.7** `flow-replay-tenant.mjs`: dom(req.path).includes + path estricto (con
  log developer); validar `collectionResolver` acepta `-` y `/`.
- **2.8** validación de migración + commit final.

**DoD**: leak test 2-tenants verde con schema estricto
(`<tenant>,extensions` sin `public`); bypasses sin fuga; CH backfilleado en
staging.

---

## Track B — Processor (3-4d, paralelo desde Día 1; solo contrato del slug)

Fase 3 ampliada (fix previo §8.3 + RLS):

- Migración: 12 tablas del SQL + **`users` (13ª)** y revisar
  `plans/features/plan_features` (globales vs tenant). RLS en las 13:
  `ALTER TABLE ... ENABLE ROW LEVEL SECURITY; FORCE ...` + política
  `tenant_slug = current_setting(...)`.
- **Los 2 `SessionLocal()` propios** (`tool_builder.py:70`, `custom_tools.py:84`)
  + `database.py:63`: sesiones tenant-scoped — GUC `app.tenant_slug` vía
  `after_begin`, rol SIN `BYPASSRLS`.
- `adk/custom_tools.py:84`: resolver credenciales con contexto de tenant.
- 4 tools Google Calendar psycopg2 crudo (create/edit/check/cancel): setear GUC
  explícito o fail ruidoso.
- **Aserción de arranque** en `main.py` post-`create_all`:
  `relrowsecurity = true` en las 13 tablas ⇒ falla si alguna no tiene RLS
  (gate `FAIL_FAST_MULTITENANT=1`).
- **Reescribir los 2 tests legacy**: `test_agent_object_authz.py` ("no owner
  column") y `test_test_panel_cross_user_isolation.py` (pool-wide).
- Fix `NameError: name 'request' is not defined` (§8.3 del HANDOFF, 11 archivos).

**DoD**: leak test 2-tenants verde en Processor (token A + tenant B ⇒ 403);
RLS verificada en arranque.

---

## Track C — Frontend + Auth (2-3d, paralelo)

- Fase 5: `validate`/`me` → `tenants[]` en selector del UI; `X-Tenant-Slug` en
  requests; nginx wildcard `api_tenant` / subrutas (con prueba viaje).
- Encaaje con claims JWT de Fase 0 (el selector solo muestra `tenants[]`).

**DoD**: dos tenants visibles en UI, switching sin recarga de datos cruzados.

---

## Track D — CRM/Auth (2-3d, paralelo)

- Fase 4: schedulers self-dispatch (proceso despierta el propio job), tabla
  `status` heredable (ColumnStore `Cross-Node`),
  `status_statement` (semilla del record no sobreescriba), decisión registro
  dual (public.tenants vs registro flow; hoy `resolveTenantSchema` lee DB flow —
  ver §6).

**DoD**: auto-provision de schema; registro dual coherente.

---

## Fase 6 — Provision, backfill y deploy (2-3d)

- Retarget `provision_tenant.py` → PG local 5433 + alta dual (ambas DBs) +
  confirma. Punto de entrada para workflow de alta de tenant.
- Backfill CH en ventana tranquila (`mutations_sync=2`).
- VPS aislada (Supabase clone / wildcard DNS) + prueba viaje completa T3.
- Estimación con buffer incluida en el camino crítico.

**DoD**: `provision_tenant.py beexa` funcional en staging; stack aislado en VPS
con backups (§10).

---

## Timeline

| Fase | Tiempo | Tracks |
|---|---|---|
| 0 | 1.5d | en serie |
| A | 5.5-6.5d | serie (2.4 bloqueante) |
| B | 3-4d | paralelo |
| C | 2-3d | paralelo |
| D | 2-3d | paralelo |
| 6 | 2-3d | serie |

**Camino crítico ≈ 1.5 + 6.5 + 3 ≈ 11-12d** (con buffer). Suma persona
~21-24d (B/C/D paralelos). Total restante del plan A2 general: ver HANDOFF §5.

---

## Leak test (spec 2-tenants, gate)

- Corre al cierre de cada fase (0, A/B/C/D, 6), no solo al final.
- Casos: B no ve datos de A; token de A + tenant B ⇒ 403; schema estricto
  (`<tenant>,extensions` sin `public`).

---

## Referencias

- `scripts/multitenant/CONTRACT.md` — nomenclatura + autorización.
- `scripts/multitenant/bypasses-fase2.md`, `clickhouse-fase2.md` — estado real
  pre-ejecución.
- `scripts/multitenant/processor-tenant-migration.sql` — base RLS (ampliar 13).
- `scripts/multitenant/flow-replay-tenant.mjs`, `provision_tenant.py` — tooling.
- HANDOFF.md §5-§7 — pendientes y chequeos previos.