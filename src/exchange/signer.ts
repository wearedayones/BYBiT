import { createHmac, createSign } from 'crypto';
import { RECV_WINDOW } from '../config/constants';

// Two signing methods, per Bybit V5 spec (see skills/SKILL.md "Signing Algorithm"):
//   HMAC-SHA256 — Bybit-generated key (you hold a Secret string). SIGN-TYPE 1, hex output.
//   RSA-SHA256  — self-generated key (you uploaded a public key). SIGN-TYPE 2, base64 output.
// `param_str` (timestamp+apiKey+recvWindow+body) is identical for both; only the SIGN
// computation and the X-BAPI-SIGN-TYPE header differ.
export interface BybitAuth {
  apiKey: string;
  signType: 1 | 2;
  secret?: string;      // HMAC secret string (signType 1)
  privateKey?: string;  // RSA private key PEM contents (signType 2)
}

export function buildGetParamStr(timestamp: number, apiKey: string, queryString: string): string {
  return `${timestamp}${apiKey}${RECV_WINDOW}${queryString}`;
}

export function buildPostParamStr(timestamp: number, apiKey: string, jsonBody: string): string {
  return `${timestamp}${apiKey}${RECV_WINDOW}${jsonBody}`;
}

export function hmacSign(secret: string, paramStr: string): string {
  return createHmac('sha256', secret).update(paramStr).digest('hex');
}

// RSA-SHA256 with PKCS#1 v1.5 padding (Node's default), base64-encoded — matches the
// `openssl dgst -sha256 -sign … | base64` flow the skill documents.
export function rsaSign(privateKey: string, paramStr: string): string {
  return createSign('RSA-SHA256').update(paramStr).end().sign(privateKey, 'base64');
}

function sign(auth: BybitAuth, paramStr: string): string {
  if (auth.signType === 2) {
    if (!auth.privateKey) throw new Error('RSA signing selected but no private key provided');
    return rsaSign(auth.privateKey, paramStr);
  }
  if (!auth.secret) throw new Error('HMAC signing selected but no secret provided');
  return hmacSign(auth.secret, paramStr);
}

export function buildHeaders(
  auth: BybitAuth,
  timestamp: number,
  paramStr: string,
  userAgent: string,
  referer: string,
): Record<string, string> {
  const headers: Record<string, string> = {
    'X-BAPI-API-KEY': auth.apiKey,
    'X-BAPI-TIMESTAMP': String(timestamp),
    'X-BAPI-SIGN': sign(auth, paramStr),
    'X-BAPI-RECV-WINDOW': String(RECV_WINDOW),
    'User-Agent': userAgent,
    'X-Referer': referer,
    'Content-Type': 'application/json',
  };
  // SIGN-TYPE 2 is required for RSA; HMAC omits it (or sets 1).
  if (auth.signType === 2) headers['X-BAPI-SIGN-TYPE'] = '2';
  return headers;
}
