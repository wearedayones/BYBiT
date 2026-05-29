import { readFileSync } from 'fs';
import { join } from 'path';
import '../src/config/env';
import { getDb, closeDb } from '../src/persistence/db';

async function migrate() {
  const sql = getDb();
  const migrationPath = join(__dirname, '..', 'migrations', '0001_init.sql');
  const migrationSql = readFileSync(migrationPath, 'utf8');
  console.log('Running migrations…');
  await sql.unsafe(migrationSql);
  console.log('✅ Migrations complete');
  await closeDb();
}

migrate().catch(err => { console.error(err); process.exit(1); });
