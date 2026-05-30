import { describe, it, expect } from 'vitest';
import { generateKeyPairSync, createVerify } from 'crypto';
import { buildGetParamStr, buildPostParamStr, hmacSign, rsaSign, buildHeaders } from '../src/exchange/signer';

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

  it('buildHeaders (HMAC) includes all required Bybit headers and omits SIGN-TYPE', () => {
    const paramStr = buildGetParamStr(ts, apiKey, '');
    const headers = buildHeaders(
      { apiKey, signType: 1, secret },
      ts, paramStr, 'bybit-skill/1.4.1', 'bybit-skill',
    );
    expect(headers['X-BAPI-API-KEY']).toBe(apiKey);
    expect(headers['X-BAPI-TIMESTAMP']).toBe(String(ts));
    expect(headers['X-BAPI-RECV-WINDOW']).toBe('5000');
    expect(headers['X-BAPI-SIGN']).toBe(hmacSign(secret, paramStr));
    expect(headers['X-BAPI-SIGN-TYPE']).toBeUndefined(); // HMAC omits it
    expect(headers['User-Agent']).toBe('bybit-skill/1.4.1');
    expect(headers['X-Referer']).toBe('bybit-skill');
  });

  it('different bodies produce different signatures', () => {
    const s1 = hmacSign(secret, buildPostParamStr(ts, apiKey, '{"side":"Buy"}'));
    const s2 = hmacSign(secret, buildPostParamStr(ts, apiKey, '{"side":"Sell"}'));
    expect(s1).not.toBe(s2);
  });

  describe('RSA signing (sign type 2)', () => {
    const { publicKey, privateKey } = generateKeyPairSync('rsa', {
      modulusLength: 2048,
      publicKeyEncoding: { type: 'spki', format: 'pem' },
      privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
    });

    it('rsaSign produces a base64 signature Bybit can verify with the public key', () => {
      const paramStr = buildPostParamStr(ts, apiKey, '{"symbol":"BTCUSDT"}');
      const sig = rsaSign(privateKey, paramStr);
      // base64, not hex
      expect(sig).toMatch(/^[A-Za-z0-9+/]+=*$/);
      // The matching public key (what Bybit stores) verifies it.
      const ok = createVerify('RSA-SHA256').update(paramStr).end().verify(publicKey, sig, 'base64');
      expect(ok).toBe(true);
    });

    it('buildHeaders (RSA) sets X-BAPI-SIGN-TYPE: 2 and a base64 sign', () => {
      const paramStr = buildGetParamStr(ts, apiKey, '');
      const headers = buildHeaders(
        { apiKey, signType: 2, privateKey },
        ts, paramStr, 'bybit-skill/1.4.1', 'bybit-skill',
      );
      expect(headers['X-BAPI-SIGN-TYPE']).toBe('2');
      expect(headers['X-BAPI-SIGN']).toBe(rsaSign(privateKey, paramStr));
    });
  });
});
