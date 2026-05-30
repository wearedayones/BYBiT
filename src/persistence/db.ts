import { neon } from '@neondatabase/serverless';
import { env } from '../config/env';
import { childLogger } from '../core/logger';

const log = childLogger({ module: 'db' });

// neon() uses Neon's HTTP API on port 443 — bypasses the blocked TCP 5432/6543.

class JsonbValue {
  constructor(readonly data: unknown) {}
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Row = Record<string, any>;

export interface SqlClient {
  // Generic overload so sql<MyType>`...` compiles (T unused at runtime).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  <T = Row>(strings: TemplateStringsArray, ...values: any[]): Promise<Row[]>;
  unsafe(rawSql: string): Promise<void>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  json(value: any): JsonbValue;
}

let _client: SqlClient | null = null;

// Retry up to 3× on CERT_NOT_YET_VALID (TLS inspection proxy clock skew).
async function tlsRetry<T>(fn: () => Promise<T>): Promise<T> {
  for (let i = 0; i < 3; i++) {
    try {
      return await fn();
    } catch (err: unknown) {
      const e = err as { code?: string; sourceError?: { cause?: { code?: string } } };
      const code = e?.code ?? e?.sourceError?.cause?.code;
      if (code === 'CERT_NOT_YET_VALID' && i < 2) {
        await new Promise(r => setTimeout(r, 1500 * (i + 1)));
        continue;
      }
      throw err;
    }
  }
  throw new Error('unreachable');
}

export function getDb(): SqlClient {
  if (!_client) {
    const neonFn = neon(env.DATABASE_URL);

    const sql = (strings: TemplateStringsArray, ...values: unknown[]): Promise<Row[]> => {
      // Inline JsonbValue → encode as JSON string and add ::jsonb cast in the query.
      const resolvedStrings: string[] = [];
      const resolvedValues: unknown[] = [];

      strings.forEach((str, i) => {
        if (i < values.length && values[i] instanceof JsonbValue) {
          resolvedStrings.push(str + '$__JSONB__');
          resolvedValues.push(JSON.stringify((values[i] as JsonbValue).data));
        } else {
          resolvedStrings.push(str);
          if (i < values.length) resolvedValues.push(values[i]);
        }
      });

      // Rebuild a proper TemplateStringsArray-compatible object by reconstructing
      // the query string with ::jsonb casts injected at the right positions.
      let query = '';
      const params: unknown[] = [];
      resolvedStrings.forEach((str, i) => {
        if (str.endsWith('$__JSONB__')) {
          query += str.slice(0, -10); // remove sentinel
          params.push(resolvedValues[i]);
          query += `$${params.length}::jsonb`;
        } else {
          query += str;
          if (i < resolvedValues.length) {
            params.push(resolvedValues[i]);
            query += `$${params.length}`;
          }
        }
      });

      // Use neonFn with a pre-built parameterized query string.
      return tlsRetry(() => neonFn.query(query, params as string[]) as Promise<Row[]>);
    };

    // For the migration SQL file: split on semicolons and run each statement.
    sql.unsafe = async (rawSql: string): Promise<void> => {
      const stmts = rawSql.split(';').map(s => s.trim()).filter(s => s.length > 0);
      for (const stmt of stmts) {
        await tlsRetry(() => neonFn.query(stmt, []));
      }
    };

    sql.json = (value: unknown): JsonbValue => new JsonbValue(value);

    _client = sql as unknown as SqlClient;
    log.info('DB connected via Neon HTTP API (port 443)');
  }
  return _client;
}

export async function closeDb(): Promise<void> {
  _client = null;
}
