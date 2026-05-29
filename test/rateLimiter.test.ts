import { describe, it, expect, vi, beforeEach } from 'vitest';
import { makeMockClock } from '../src/core/clock';
import { RateLimiter } from '../src/exchange/rateLimiter';
import { BybitApiError } from '../src/core/errors';

describe('rateLimiter', () => {
  it('builds without error', () => {
    const clock = makeMockClock(Date.now());
    const rl = new RateLimiter(clock);
    expect(rl).toBeDefined();
  });

  it('scheduleGet resolves with function result', async () => {
    const clock = makeMockClock(Date.now());
    const rl = new RateLimiter(clock);
    const result = await rl.scheduleGet(() => Promise.resolve(42));
    expect(result).toBe(42);
  });

  it('schedulePost resolves with function result', async () => {
    const clock = makeMockClock(Date.now());
    const rl = new RateLimiter(clock);
    const result = await rl.schedulePost(() => Promise.resolve('done'));
    expect(result).toBe('done');
  });

  it('propagates non-rate-limit errors', async () => {
    const clock = makeMockClock(Date.now());
    const rl = new RateLimiter(clock);
    await expect(rl.scheduleGet(() => Promise.reject(new Error('network error')))).rejects.toThrow('network error');
  });
});
