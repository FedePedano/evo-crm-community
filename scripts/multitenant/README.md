# Spike multitenant A2 — tooling de provisioning (rama `exp/multitenant-a2`)

- `provision_tenant.py`: crea `cliente_<slug>` + registro en `public.tenants`
  (+ membresía owner opcional). Idempotente; ante fallo parcial hace
  `DROP SCHEMA ... CASCADE` y marca `failed` para retry limpio.
- **Solo contra el proyecto Supabase de TEST** (credenciales en `.env.test`
  local, git-ignorado). Jamás apuntar a producción ni al VPS.
- Alcance v1 = orquestación. El replay real de migraciones Rails y los seeds
  de negocio entran en Fase 1 (requiere toolchain Ruby/staging).

Uso:
  python3 provision_tenant.py demo_inmo [--owner <uuid> --plan free]
  python3 provision_tenant.py demo_x --fail-at seed   # probar limpieza
