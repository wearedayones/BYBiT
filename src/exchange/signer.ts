import { createHmac } from 'crypto';
import { RECV_WINDOW } from '../config/constants';

export function buildGetParamStr(timestamp: number, apiKey: string, queryString: string): string {
  return `${timestamp}${apiKey}${RECV_WINDOW}${queryString}`;
}

export function buildPostParamStr(timestamp: number, apiKey: string, jsonBody: string): string {
  return `${timestamp}${apiKey}${RECV_WINDOW}${jsonBody}`;
}

export function hmacSign(secret: string, paramStr: string): string {
  return createHmac('sha256', secret).update(paramStr).digest('hex');
}

export function buildHeaders(
  apiKey: string,
  secret: string,
  timestamp: number,
  paramStr: string,
  userAgent: string,
  referer: string,
): Record<string, string> {
  return {
    'X-BAPI-API-KEY': apiKey,
    'X-BAPI-TIMESTAMP': String(timestamp),
    'X-BAPI-SIGN': hmacSign(secret, paramStr),
    'X-BAPI-RECV-WINDOW': String(RECV_WINDOW),
    'User-Agent': userAgent,
    'X-Referer': referer,
    'Content-Type': 'application/json',
  };
}
