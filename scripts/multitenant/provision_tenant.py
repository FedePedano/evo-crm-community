#!/usr/bin/env python3
"""provision_tenant — spike idempotente de aprovisionamiento multitenant A2.

Crea el schema del tenant + fila en el registro `public.tenants` (+ membresía
owner opcional) en el proyecto Supabase de TEST. Lee credenciales de
`.env.test` (jamás commitear, jamás apuntar a producción).

Alcance v1 (spike): valida la ORQUESTACIÓN (idempotencia, limpieza ante
fallo parcial, registro, search_path). El replay real de las migraciones
Rails (88 tablas) y los seeds de negocio (admin, inbox WA/IG, agente IA)
entran en Fase 1 con toolchain Ruby/staging; acá el "schema" lleva una
tabla marcador `_provision_log` que prueba el switch por search_path.

Garantías:
  - Idempotente: tenant activo existente => no-op con éxito.
  - Ante fallo parcial: DROP SCHEMA ... CASCADE + status 'failed', de modo
    que el retry con el mismo slug arranca limpio (sin schemas fantasma).
  - Fail-closed: el schema se nombra `cliente_<slug>` (nunca `public`).
"""
import os
import re
import sys
import psycopg

SLUG_RE = re.compile(r'\A[a-z0-9_]+\Z')
SCHEMA_PREFIX = 'cliente_'


def db_url():
    for line in open('.env.test'):
        if line.startswith('SUPABASE_TEST_POOLER_SESSION_URL='):
            return line.strip().split('=', 1)[1]
    raise SystemExit('.env.test sin SUPABASE_TEST_POOLER_SESSION_URL')


def ensure_registry(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            slug text NOT NULL UNIQUE,
            schema_name text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'active',
            plan text NOT NULL DEFAULT 'free',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public.tenant_memberships (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES public.tenants(id),
            user_id uuid NOT NULL,
            role text NOT NULL DEFAULT 'owner',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, user_id)
        )""")
    conn.commit()


def schema_exists(conn, schema):
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)).fetchone()
    return row is not None


def provision(slug, owner_user_id=None, plan='free', fail_at=None):
    if not SLUG_RE.match(slug or ''):
        raise ValueError(f'slug inválido: {slug!r} (solo minúsculas, números, guión bajo)')
    schema = SCHEMA_PREFIX + slug
    if schema == 'public':
        raise ValueError('el schema del tenant nunca puede ser public')

    with psycopg.connect(db_url(), connect_timeout=30) as conn:
        ensure_registry(conn)
        row = conn.execute(
            "SELECT id, status FROM public.tenants WHERE slug = %s", (slug,)).fetchone()
        if row and row[1] == 'active' and schema_exists(conn, schema):
            print(f'IDEMPOTENTE: {slug} ya activo en {schema}, sin cambios.')
            return {'slug': slug, 'schema': schema, 'reused': True}
        if row:
            print(f'RETRY tras fallo previo (status={row[1]}): limpiando...')
            conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
            conn.execute("DELETE FROM public.tenant_memberships WHERE tenant_id = %s", (row[0],))
            conn.execute("DELETE FROM public.tenants WHERE id = %s", (row[0],))
            conn.commit()
        elif schema_exists(conn, schema):
            print(f'Schema huérfano {schema} sin registro: eliminando...')
            conn.execute(f'DROP SCHEMA {schema} CASCADE')
            conn.commit()

        tenant_id = None
        try:
            conn.execute(f'CREATE SCHEMA {schema}')
            if fail_at == 'create-schema':
                raise RuntimeError('fallo simulado tras CREATE SCHEMA')
            row = conn.execute(
                """INSERT INTO public.tenants (slug, schema_name, status, plan)
                   VALUES (%s, %s, 'provisioning', %s) RETURNING id""",
                (slug, schema, plan)).fetchone()
            tenant_id = row[0]
            if fail_at == 'registry':
                raise RuntimeError('fallo simulado tras registro')
            # Seed mínimo v1: marcador que prueba el switch por search_path.
            conn.execute(f'CREATE TABLE {schema}._provision_log (step text, at timestamptz DEFAULT now())')
            conn.execute(f"INSERT INTO {schema}._provision_log (step) VALUES ('schema-created')")
            if fail_at == 'seed':
                raise RuntimeError('fallo simulado tras seed mínimo')
            if owner_user_id:
                conn.execute(
                    """INSERT INTO public.tenant_memberships (tenant_id, user_id, role)
                       VALUES (%s, %s, 'owner')""", (tenant_id, owner_user_id))
            conn.execute("UPDATE public.tenants SET status = 'active' WHERE id = %s", (tenant_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            # Limpieza: sin schemas fantasma; el registro queda en 'failed'.
            conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
            if tenant_id:
                conn.execute("UPDATE public.tenants SET status = 'failed' WHERE id = %s", (tenant_id,))
            conn.commit()
            raise
        print(f'OK: tenant {slug} activo en schema {schema}.')
        return {'slug': slug, 'schema': schema, 'reused': False}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('slug')
    ap.add_argument('--owner', default=None)
    ap.add_argument('--plan', default='free')
    ap.add_argument('--fail-at', default=None, choices=['create-schema', 'registry', 'seed'])
    args = ap.parse_args()
    try:
        provision(args.slug, args.owner, args.plan, args.fail_at)
    except Exception as e:
        print(f'PROVISION_FAIL: {type(e).__name__}: {e}')
        sys.exit(1)
