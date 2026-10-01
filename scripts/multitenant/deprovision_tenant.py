#!/usr/bin/env python3
"""deprovision_tenant — baja idempotente de un tenant (spike multitenant A2).

Elimina el schema del tenant + membresías y marca el registro como
'deleted' (se conserva la fila como auditoría; un re-provision posterior
con el mismo slug arranca limpio porque provision solo reusa 'active').

Rieles de seguridad (no negociables):
  - Solo schemas con prefijo `cliente_`; `public` (y lo que no matchee)
    se rechaza siempre.
  - Requiere confirmación explícita: --i-am-sure <slug> (doble tipeo del
    slug: argumento posicional + flag deben coincidir).
  - Solo contra Supabase TEST vía `.env.test`. Jamás producción.

Uso:
  python3 deprovision_tenant.py demo_falla --i-am-sure demo_falla
"""
import re
import sys
import psycopg

SLUG_RE = re.compile(r'\A[a-z0-9_]+\Z')
SCHEMA_PREFIX = 'cliente_'
FORBIDDEN = {'public', 'extensions', 'auth', 'storage', 'realtime',
             'pg_catalog', 'information_schema'}


def db_url():
    for line in open('.env.test'):
        if line.startswith('SUPABASE_TEST_POOLER_SESSION_URL='):
            return line.strip().split('=', 1)[1]
    raise SystemExit('.env.test sin SUPABASE_TEST_POOLER_SESSION_URL')


def deprovision(slug, confirm):
    if not SLUG_RE.match(slug or ''):
        raise ValueError(f'slug inválido: {slug!r}')
    if slug != confirm:
        raise SystemExit('confirmación distinta del slug: abortado (pasá --i-am-sure <mismo-slug>)')
    schema = SCHEMA_PREFIX + slug
    if schema in FORBIDDEN or not schema.startswith(SCHEMA_PREFIX):
        raise SystemExit(f'schema protegido o inesperado: {schema}, abortado')

    with psycopg.connect(db_url(), connect_timeout=30) as conn:
        row = conn.execute(
            "SELECT id, status FROM public.tenants WHERE slug = %s", (slug,)).fetchone()
        if row is None and not schema_exists(conn, schema):
            print(f'IDEMPOTENTE: {slug} no existe (ni registro ni schema), sin cambios.')
            return {'slug': slug, 'removed': False}
        tenant_id = row[0] if row else None
        schema_present = schema_exists(conn, schema)
        n_tables = (conn.execute(
            """SELECT count(*) FROM information_schema.tables
               WHERE table_schema = %s""", (schema,)).fetchone()[0]
            if schema_present else 0)
        n_members = (conn.execute(
            "SELECT count(*) FROM public.tenant_memberships WHERE tenant_id = %s",
            (tenant_id,)).fetchone()[0] if tenant_id else 0)
        print(f'Baja de {slug}: schema {schema} '
              f'({"ausente" if not schema_present else f"{n_tables} tablas"}), '
              f'{n_members} membresías, registro={row[1] if row else "ausente"}.')
        conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        if tenant_id:
            conn.execute("DELETE FROM public.tenant_memberships WHERE tenant_id = %s", (tenant_id,))
            if row[1] != 'deleted':
                conn.execute("UPDATE public.tenants SET status = 'deleted' WHERE id = %s", (tenant_id,))
        conn.commit()
        print(f'OK: {slug} dado de baja (schema eliminado).')
        return {'slug': slug, 'removed': True}


def schema_exists(conn, schema):
    return conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,)).fetchone() is not None


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('slug')
    ap.add_argument('--i-am-sure', default=None)
    args = ap.parse_args()
    try:
        deprovision(args.slug, args.i_am_sure)
    except SystemExit as e:
        print(f'ABORTADO: {e}')
        sys.exit(2)
    except Exception as e:
        print(f'DEPROVISION_FAIL: {type(e).__name__}: {e}')
        sys.exit(1)
