import { Pool, neonConfig } from '@neondatabase/serverless';
import ws from 'ws';
import { env } from '../config/env';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'db' });

// Route through WebSocket on port 443 — bypasses the TCP 5432 firewall restriction.
neonConfig.webSocketConstructor = ws;

// Marker class so the template builder can add ::jsonb casts automatically,
// matching the behaviour of postgres.js sql.json().
class JsonbValue {
  constructor(readonly data: unknown) {}
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Row = Record<string, any>;

// Minimal postgres.js-compatible interface used across the codebase.
export interface SqlClient {
  // Generic overload lets callers write sql<MyType>`...` like postgres.js — T is unused
  // at runtime but silences the TS2558 errors across the codebase.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  <T = Row>(strings: TemplateStringsArray, ...values: any[]): Promise<Row[]>;
  unsafe(rawSql: string): Promise<void>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  json(value: any): JsonbValue;
}

let _pool: Pool | null = null;
let _client: SqlClient | null = null;

function buildClient(pool: Pool): SqlClient {
  const sql = (strings: TemplateStringsArray, ...values: unknown[]): Promise<Row[]> => {
    let query = '';
    const params: unknown[] = [];
    strings.forEach((str, i) => {
      query += str;
      if (i < values.length) {
        const v = values[i];
        if (v instanceof JsonbValue) {
          params.push(JSON.stringify(v.data));
          query += `$${params.length}::jsonb`;
        } else {
          params.push(v);
          query += `$${params.length}`;
        }
      }
    });
    return pool.query(query, params).then(r => r.rows as Row[]);
  };

  // For raw migration SQL — split on semicolons and run each statement.
  sql.unsafe = async (rawSql: string): Promise<void> => {
    const stmts = rawSql
      .split(';')
      .map(s => s.trim())
      .filter(s => s.length > 0);
    for (const stmt of stmts) {
      await pool.query(stmt);
    }
  };

  sql.json = (value: unknown): JsonbValue => new JsonbValue(value);

  return sql;
}

export function getDb(): SqlClient {
  if (!_client) {
    _pool = new Pool({ connectionString: env.DATABASE_URL, max: 5 });
    _client = buildClient(_pool);
    log.info('DB connected via Neon serverless (WebSocket → port 443)');
  }
  return _client;
}

export async function closeDb(): Promise<void> {
  if (_pool) {
    await _pool.end();
    _pool = null;
    _client = null;
  }
}
