#!/usr/bin/env node
// Replay de migraciones de evo-flow en el schema del tenant (exp/multitenant-a2).
// Uso: node scripts/multitenant/flow-replay-tenant.mjs <slug>
// Requiere dist/ compilado (nest build) y PG de flow levantado.
// Mecanismo probado 2026-10-01: `schema` + `options: -c search_path=` cubren
// entidades Y raw SQL; la tabla `migrations` queda local al schema.
import { DataSource } from '/home/maldito/beexa/crm/evo-crm-community/evo-flow-community/node_modules/typeorm/index.js';
import { readdirSync } from 'fs';

const slug = process.argv[2];
if (!slug || !/^[a-z0-9_]+$/.test(slug)) {
  console.error('slug inválido');
  process.exit(1);
}
const schema = `cliente_${slug}`;

const migDir = '/home/maldito/beexa/crm/evo-crm-community/evo-flow-community/dist/database/migrations';
const classes = [];
for (const f of readdirSync(migDir).filter((x) => x.endsWith('.js'))) {
  const mod = await import(`${migDir}/${f}`);
  for (const v of Object.values(mod)) {
    if (typeof v === 'function' && v.name) classes.push(v);
  }
}
console.log(`migraciones: ${classes.length}`);

const ds = new DataSource({
  type: 'postgres',
  host: 'localhost',
  port: 5433,
  username: 'flow',
  password: 'flow_staging_pw',
  database: 'flow_staging',
  schema,
  entities: [],
  migrations: classes,
  extra: { options: `-c search_path=${schema},extensions,public` },
});

await ds.initialize();
// El schema del tenant debe existir antes de cualquier DDL (search_path no crea).
await ds.query(`CREATE SCHEMA IF NOT EXISTS ${schema}`);
await ds.query(`CREATE SCHEMA IF NOT EXISTS extensions`);
const pending = await ds.showMigrations();
console.log(`pendientes antes: ${pending}`);
await ds.runMigrations({ transaction: 'all' });
const tables = await ds.query(
  `SELECT count(*) FROM information_schema.tables WHERE table_schema = $1`,
  [schema],
);
console.log(`OK replay flow ${slug}: tablas=${tables[0].count}`);
await ds.destroy();
