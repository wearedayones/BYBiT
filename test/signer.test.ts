import { describe, it, expect } from 'vitest';
import { buildGetParamStr, buildPostParamStr, hmacSign, buildHeaders } from '../src/exchange/signer';

describe('signer', () => {
  const apiKey = 'testkey123';
  const secret = 'supersecret';
  const ts = 1700000000000;

  it('buildGetParamStr follows skill spec: {ts}{key}{recv}{qs}', () => {
    const qs = 'category=linear&symbol=BTCUSDT';
    const result = buildGetParamStr(ts, apiKey, qs);
    expect(result).toBe(`${ts}${apiKey}5000${qs}`);
  });

  it('buildPostParamStr follows skill spec: {ts}{key}{recv}{body}', () => {
    const body = JSON.stringify({ symbol: 'BTCUSDT', side: 'Buy' });
    const result = buildPostParamStr(ts, apiKey, body);
    expect(result).toBe(`${ts}${apiKey}5000${body}`);
  });

  it('hmacSign produces correct HMAC-SHA256 hex', () => {
    const { createHmac } = require('crypto');
    const paramStr = buildGetParamStr(ts, apiKey, 'test=1');
    const expected = createHmac('sha256', secret).update(paramStr).digest('hex');
    expect(hmacSign(secret, paramStr)).toBe(expected);
  });

  it('buildHeaders includes all required Bybit headers', () => {
    const paramStr = buildGetParamStr(ts, apiKey, '');
    const headers = buildHeaders(apiKey, secret, ts, paramStr, 'bybit-skill/1.4.1', 'bybit-skill');
    expect(headers['X-BAPI-API-KEY']).toBe(apiKey);
    expect(headers['X-BAPI-TIMESTAMP']).toBe(String(ts));
    expect(headers['X-BAPI-RECV-WINDOW']).toBe('5000');
    expect(headers['X-BAPI-SIGN']).toBeTruthy();
    expect(headers['User-Agent']).toBe('bybit-skill/1.4.1');
    expect(headers['X-Referer']).toBe('bybit-skill');
  });

  it('different bodies produce different signatures', () => {
    const s1 = hmacSign(secret, buildPostParamStr(ts, apiKey, '{"side":"Buy"}'));
    const s2 = hmacSign(secret, buildPostParamStr(ts, apiKey, '{"side":"Sell"}'));
    expect(s1).not.toBe(s2);
  });
});
