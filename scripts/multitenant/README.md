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
  python3 deprovision_tenant.py demo_x --i-am-sure demo_x
  python3 e2e_tenant_switch.py   # replica TenantSwitcher: 160 reqs cruzados
                                 # entre 2 tenants + slugs inválidos, exige
                                 # cero fuga, cero search_path residual y 404
                                 # fail-closed (válido con Ruby 3.4.4 `ruby -c`
                                 # para los .rb; toolchain en /tmp, fuera del repo)
