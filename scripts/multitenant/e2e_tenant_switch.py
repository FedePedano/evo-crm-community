#!/usr/bin/env python3
"""e2e_tenant_switch — harness del flujo TenantSwitcher contra Supabase TEST.

Replica EXACTAMENTE la semántica del middleware Ruby
(app/middleware/tenant_switcher.rb):
  1. registry_present?  -> public.tenants existe?
  2. resolve(slug)      -> SELECT en public.tenants (fail-closed: 404 si no hay)
  3. switch_to          -> SET search_path <schema>, public, extensions
  4. trabajo del request -> query sin calificar
  5. ensure             -> RESET search_path + reset de contexto

Además simula el pool de conexiones Rails (N conexiones persistentes
compartidas entre threads) para probar higiene bajo concurrencia:
200 requests mezclados entre 2 tenants + slugs inválidos, verificando
cero fuga y cero search_path residual.

Solo TEST (.env.test). Jamás producción.
"""
import queue
import threading
import psycopg

TENANTS = {
    'demo_inmo': 'solo-A',    # semilla esperada en su schema (tabla probe)
    'demo_taller': 'solo-T',
}
POOL_SIZE = 4
REQUESTS_PER_TENANT = 80
BAD_SLUGS = ['noexiste', 'demo_falla', 'PUBLIC', 'public']


def db_url():
    for line in open('.env.test'):
        if line.startswith('SUPABASE_TEST_POOLER_SESSION_URL='):
            return line.strip().split('=', 1)[1]
    raise SystemExit('.env.test sin SUPABASE_TEST_POOLER_SESSION_URL')


def seed():
    with psycopg.connect(db_url(), connect_timeout=30) as c:
        for schema, val in [('cliente_demo_inmo', 'solo-A'), ('cliente_demo_taller', 'solo-T')]:
            c.execute(f"CREATE TABLE IF NOT EXISTS {schema}.probe (v text)")
            c.execute(f"DELETE FROM {schema}.probe")
            c.execute(f"INSERT INTO {schema}.probe (v) VALUES (%s)", (val,))
        c.commit()


class App:
    """El 'inner app': qué ve el request con el search_path fijado."""
    @staticmethod
    def call(conn):
        return conn.execute("SELECT v FROM probe").fetchall()


def handle_request(pool, slug, results):
    """Un request HTTP simulado: checkout, switch, trabajo, reset, checkin."""
    conn = pool.get()
    try:
        # 1. registry_present?
        reg = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='tenants'").fetchone()
        if not reg:
            results.append(('transparent', slug))
            return
        # 2. resolve(slug) — fail-closed
        row = conn.execute(
            "SELECT schema_name FROM public.tenants WHERE slug=%s AND status='active'",
            (slug,)).fetchone()
        if not row:
            results.append(('404', slug))  # jamás toca datos del tenant
            return
        schema = row[0]
        # 3. switch_to
        conn.execute(f"SET search_path TO {schema}, public, extensions")
        try:
            # 4. trabajo
            rows = App.call(conn)
            results.append(('200', slug, tuple(r[0] for r in rows)))
        finally:
            # 5. ensure RESET
            try:
                conn.execute("RESET search_path")
            except Exception:
                pass
    finally:
        pool.put(conn)


def main():
    seed()
    pool = queue.Queue()
    for _ in range(POOL_SIZE):
        pool.put(psycopg.connect(db_url(), connect_timeout=30))
    results, threads = [], []

    def run(slug, n):
        for _ in range(n):
            handle_request(pool, slug, results)

    for slug in list(TENANTS) + BAD_SLUGS:
        n = REQUESTS_PER_TENANT if slug in TENANTS else 5
        threads.append(threading.Thread(target=run, args=(slug, n)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == '200']
    notf = [r for r in results if r[0] == '404']
    leaks = [r for r in oks if r[2] != (TENANTS[r[1]],)]
    print(f'requests 200: {len(oks)}, 404 fail-closed: {len(notf)}, FUGAS: {len(leaks)}')
    assert not leaks, leaks
    assert len(notf) == 5 * len(BAD_SLUGS), notf
    # higiene: ninguna conexión del pool retiene search_path
    dirty = 0
    while not pool.empty():
        c = pool.get()
        sp = c.execute("SHOW search_path").fetchone()[0]
        if 'cliente_' in sp:
            dirty += 1
        c.close()
    print(f'conexiones con search_path residual: {dirty}')
    assert dirty == 0
    print('E2E_SWITCH: TODO OK — cero fuga, cero residuo, fail-closed.')


if __name__ == '__main__':
    main()
