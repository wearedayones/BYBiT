import { describe, it, expect } from 'vitest';
import { BotApiError } from '../src/core/errors';

describe('bot response envelope', () => {
  function parseBotResponse(body: { status_code: number; debug_msg: string; result: unknown }) {
    if (body.status_code === 503) throw new BotApiError(503, 'Active investment cycle', '/v5/dca/close-bot');
    if (body.status_code === 421) throw new BotApiError(421, 'Account ban', '/v5/grid/create-grid');
    if (body.status_code !== 200) throw new BotApiError(body.status_code, body.debug_msg, '/v5/test');
    return body.result;
  }

  it('returns result on status_code 200', () => {
    const r = parseBotResponse({ status_code: 200, debug_msg: '', result: { grid_id: 'abc' } });
    expect(r).toEqual({ grid_id: 'abc' });
  });

  it('throws BotApiError on 421 (ban)', () => {
    expect(() => parseBotResponse({ status_code: 421, debug_msg: 'ban', result: null }))
      .toThrow(BotApiError);
  });

  it('throws BotApiError on 503 (active cycle)', () => {
    expect(() => parseBotResponse({ status_code: 503, debug_msg: 'active', result: null }))
      .toThrow(BotApiError);
  });

  it('throws BotApiError on any non-200 code', () => {
    expect(() => parseBotResponse({ status_code: 80002, debug_msg: 'margin error', result: null }))
      .toThrow(BotApiError);
  });
});
