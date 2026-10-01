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

## Schedulers pendientes de convertir a dispatcher (patrón probado con
## InactivityCheckSchedulerJob + TenantDispatch + InactivityCheckWorkerJob)
## Cada uno: scheduler -> dispatch_to_each_tenant(Worker); worker con el
## cuerpo original; los hijos heredan tenant por el Client middleware.
## Falta verificar en staging con tenants reales (sin Ruby local no se ejecutan).
- trigger_scheduled_items_job / scheduled_actions_processor_job
- pipelines: CheckOverdueTasksJob, StageInactivityCheckSchedulerJob
- task_due_soon_reminder_job / trigger_imap_email_inboxes_job
- migration: conversations_first_reply_scheduler_job
- whatsapp: templates_sync_scheduler_job, credential_probe_scheduler_job
