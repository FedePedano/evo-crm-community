# CONTRACT — Identidad de tenant multitenant A2

> Versión: 2026-10-10. Fuente de verdad de NOMENCLATURA y de la regla de
> autorización por claims. Ningún servicio deriva el slug por su cuenta:
> todos lo LEEN del registro de tenants. Documento hermano de `PLAN-EJECUCION.md`.

---

## 1. El slug: formato canónico

- El identificador canónico del tenant es el **`slug`** de `public.tenants`
  (tabla calificada, resuelve igual con cualquier `search_path`).
- Regex estricta (compartida por HTTP, provision y migrations):
  `^[a-z0-9_]+$` — solo minúsculas, dígitos y guion bajo. Slug vacío, con
  mayúsculas o con guion medio ⇒ **inválido** (fail-closed).
- Ejemplos válidos: `beexa`, `demo_inmo`, `acme_taller`. `cliente_beexa` NO
  es un slug; es el nombre de schema (§3).

## 2. Nombres derivados (SIEMPRE desde el registro, nunca derivados por servicio)

| Concepto | Valor | Ejemplo |
|---|---|---|
| slug canónico | `public.tenants.slug` | `beexa`, `demo_inmo` |
| schema Postgres | `cliente_<slug>` | `cliente_beexa`, `cliente_demo_inmo` |
| ClickHouse `tenant_id` | `<slug>` (raw) | `beexa` (**no** `cliente_beexa`) |
| Processor `tenant_slug` | `<slug>` | `beexa` |
| Redis scope / `cache_key_scope` | `<slug>` | `beexa` |
| header HTTP | `X-Tenant-Slug` | `demo_inmo` |

- Prefijo `cliente_` es SOLO de schemas PG. En ClickHouse, Processor y Redis va
  el slug pelado. Confundirlos rompe el matching en silencio (nada filtra, nada
  matchea).
- Cada servicio resuelve su derivado leyendo el registro:
  - **Flow**: `src/multitenant/tenants.registry.ts` → `resolveTenantSchema`
    (lee `public.tenants` de la DB de flow).
  - **Processor**: columna `tenant_slug` (migración `processor-tenant-migration.sql`).
  - **CRM/Auth**: `public.tenants` en `evo_community`.

## 3. Atribución y legacy

- Estado legacy previo a A2 = sin tenant. En almacenes de escritura se admite
  `'default'` como valor transitorio (ClickHouse `tenant_id DEFAULT 'default'`,
  Processor `tenant_slug DEFAULT 'default'`) para que los INSERTs viejos no
  rompan; **'default' no es un tenant** y ningún lector filtra por él: las
  lecturas SIEMPRE llevan `tenant_id = <slug>` explícito del usuario.
- Backfill de datos legacy: `ALTER TABLE ... UPDATE tenant_id='<slug_actual>'
  WHERE tenant_id='default'` en ventana tranquila (§5 de PLAN-EJECUCION.md).
- `'default'` se elimina cuando el runtime garantiza que todo insert lleva slug
  explícito (fail-closed).

## 4. Autorización: el slug del request DEBE estar en los claims del JWT

- El JWT lleva `tenants[]` (claims firmados, subidos en Fase 0). El **mecanismo
  de consumo** depende del servicio; para Flow es vía `/validate` (§4.1).
- Todo backend que resuelva el tenant del request —por subdominio (`<slug>.<host>`) o
  por header `X-Tenant-Slug`— DEBE validar:
  1. sanear el slug (`^[a-z0-9_]+$`);
  2. resolverlo autoritativo contra el registro de tenants (status `active`);
  3. **si el slug resuelto NO está en los tenants del token ⇒ `403`, jamás
     `'default'` ni el schema de otro tenant**.
- El header nunca se confía tal cual: solo es insumo de la resolución.
- Caso de prueba obligatorio en el leak test (§6): **token de A + tenant B ⇒ 403**.

### 4.1 Consumo de los tenants vía evo-auth-service (decisión Fase 0.4)

En esta fase **no** se comparte `DOORKEEPER_JWT_SECRET_KEY` con Flow ni se
verifica el JWT localmente (sin decode HS256). Los tenants se obtienen del
servicio de auth:

1. **Flow** envía el token recibido (`Authorization: Bearer <jwt>`) a
   `POST {EVO_AUTH_SERVICE_URL}/api/v1/auth/validate`.
2. **Auth** valida el token como access token respaldado por `oauth_access_tokens`
   (idéntico al flujo de Doorkeeper): la fila debe existir, **no** estar revocada
   y **no** estar expirada. **No** verifica firma, `iss` ni `aud` — el JWT es
   solo el formato de almacenamiento del token, que se resuelve por lookup
   (nunca por decode). Devuelve `data.tenants` como array de **slugs activos**
   (`string[]`, p. ej. `["acme","globex"]`), sin exponer `schema_name`/`plan`/`role`.
   `decode_jwt_token` existe pero no se invoca; no forma parte del flujo.
3. El slug resuelto por Flow (§1–§4) DEBE pertenecer a ese array.
4. Responder **`403`** (fail-closed) si: el token es inválido/expirado, falta
   `tenants[]`, el array está vacío, o el slug resuelto no está incluido.
5. **Nunca** fallback a `'default'` ni al schema de otro tenant.
6. En rutas tenant-aware, errores de Auth (5xx) o **timeout** ⇒ **fail-closed**
   (no fail-open).
7. Mecanismo de autenticación: `/validate` acepta `Authorization: Bearer <jwt>`
   o el header `api_access_token`; **no** acepta cookies (`_evo_at`/`_evo_rt`).
   Flow forwardea el Bearer.

> **Estado garantizado (2026-10-10):** `POST /validate` resuelve el token por
> lookup en `oauth_access_tokens` y rechaza token inexistente, expirado y
> **revocado** (cubierto por `spec/services/token_validation_service_spec.rb`).
> `data.tenants` es un `string[]` de slugs activos con membresía válida, sin
> `schema_name`/`plan`/`role` (cubierto por
> `spec/requests/api/v1/auth_validate_tenants_spec.rb`). Firma, `iss` y `aud`
> quedan **fuera del contrato a propósito**: el token se trata como
> opaco/DB-backed y `decode_jwt_token` no se usa (código muerto).

## 5. Redis multi-tenant

- `cache_key_scope` (extensión point de Flow, consumida en
  `base-cache.service.ts:484`) devuelve el slug del tenant activo (CLS). Todas
  las claves de caché quedan namespaced por tenant; nunca clave global tras la
  Fase 2.3.

## 6. Leak test dinámico (spec 2 tenants)

Modelo: `evo-ai-crm-community/spec/multitenant/tenant_isolation_spec.rb` (5/5).
Esqueleto agendado en Fase 0; se ejecuta al cierre de cada fase:
- levanta dos schemas (`cliente_a`/`cliente_b`), inyecta datos en A;
- consulta como B → **falla si B ve algo de A**;
- caso **token de A + tenant B → 403**;
- corre con `search_path` estricto (`<tenant>,extensions`, sin `public`).