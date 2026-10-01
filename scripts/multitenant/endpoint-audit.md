# Auditoría de aislamiento por endpoint (staging 2026-10-01)

Barrido de las rutas GET del CRM contra staging (`demo_inmo`), con el path
estricto `tenant, extensions` (sin `public`). Objetivo: ningún 500 por
lectura no-calificada de `public`, y fail-closed total sin tenant.

## Método

1. **Sin auth** (206 rutas): solo routing + middleware. Header `X-Tenant-Slug`.
2. **Con auth** (206 rutas): el 77% daba 401 (el auth frena antes de la
   query, no audita nada). Se sembró auth real:
   - `Rails.cache` (redis, tras alinear `staging.rb` a `:redis_cache_store`
     como producción) con validación evo-auth cacheada para
     `X-Api-Access-Token: staging-audit-token-demo-inmo` → admin@demo.test
     rol `administrator` (TTL 1h, solo staging).
   - 146 permission keys extraídas estático → `TenantCache` TRUE por 1h
     (evita las llamadas remotas a evo-auth, ausente en staging).
   - Script: `evo-ai-crm-community/tmp/seed_audit.rb` (throwaway, git-ignored).
3. **Fail-closed**: 20 rutas × (slug inválido + sin header).

## Resultados

| Barrido | Total | PASS | Sin-dato (404 c/ID falso) | 500 | 404-tenant |
|---|---|---|---|---|---|
| Sin auth, tenant válido | 206 | 194 | 12 | 0 | 0 |
| Con auth, tenant válido | 206 | 112 | 73 | 4* | 0 |
| Slug inválido (muestra) | 20 | 0 | 0 | 0 | 20 |
| Sin header (muestra) | 20 | 0 | 0 | 0 | 20 |

\* Los 4 500 autenticados, todos NO-aislamiento:
- `agents#index`: conecta a `localhost:3998` (EVO_AI_CORE ausente en staging) — ambiental.
- `evo_flow/contact_events#index`: falta `AUTH_APIKEY_INTEGRATION_LOCAL` (evo-flow ausente) — ambiental.
- `csat_survey_responses#download`: template faltante — bug preexistente, igual en prod.
- `callbacks#register_facebook_page`: crea con params vacíos → nil en el jbuilder — igual en prod sin params.

Resto no-200 con auth: 400s (validación de params), 503s (servicios externos
ausentes: segments, evolution_hub), 302s. Ningún `PG::UndefinedTable` en logs:
**cero lecturas no-calificadas de `public` en 206 rutas**.

## Límites conocidos

- POST/PUT/DELETE fuera del barrido (solo lectura + routing).
- Rutas `sin-dato` (inbox/canal/conversación real) se auditan con seeds
  completos (opción D).
- Tokens y seeds solo existen en staging; nada toca producción.
