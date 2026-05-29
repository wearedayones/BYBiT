import postgres from 'postgres';
import { env } from '../config/env';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'db' });

let _sql: ReturnType<typeof postgres> | null = null;

export function getDb() {
  if (!_sql) {
    _sql = postgres(env.DATABASE_URL, {
      max: 10,
      idle_timeout: 30,
      connect_timeout: 10,
      onnotice: (msg) => log.debug({ msg }, 'DB notice'),
    });
  }
  return _sql;
}

export async function closeDb() {
  if (_sql) {
    await _sql.end();
    _sql = null;
  }
}
