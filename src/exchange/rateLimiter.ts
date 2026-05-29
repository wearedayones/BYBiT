import Bottleneck from 'bottleneck';
import { RATE_LIMIT } from '../config/constants';
import { childLogger } from '../core/logger';
import type { Clock } from '../core/clock';
import { realClock } from '../core/clock';

const log = childLogger({ module: 'rate-limiter' });

export class RateLimiter {
  private readonly getLane: Bottleneck;
  private readonly postLane: Bottleneck;
  private consecutiveRateLimits = 0;
  private pausedUntil = 0;
  private readonly clock: Clock;

  constructor(clock: Clock = realClock) {
    this.clock = clock;
    this.getLane = new Bottleneck({ minTime: RATE_LIMIT.GET_MIN_MS, maxConcurrent: 10 });
    this.postLane = new Bottleneck({ minTime: RATE_LIMIT.POST_MIN_MS, maxConcurrent: 5 });
  }

  async scheduleGet<T>(fn: () => Promise<T>): Promise<T> {
    return this.getLane.schedule(() => this.withBackoff(fn));
  }

  async schedulePost<T>(fn: () => Promise<T>): Promise<T> {
    return this.postLane.schedule(() => this.withBackoff(fn));
  }

  private async withBackoff<T>(fn: () => Promise<T>): Promise<T> {
    const now = this.clock.now();
    if (now < this.pausedUntil) {
      await sleep(this.pausedUntil - now);
    }

    let attempts = 0;
    while (true) {
      try {
        const result = await fn();
        this.consecutiveRateLimits = 0;
        return result;
      } catch (err: unknown) {
        if (isRateLimitError(err) && attempts < RATE_LIMIT.RATE_LIMIT_MAX_RETRIES) {
          attempts++;
          this.consecutiveRateLimits++;

          if (this.consecutiveRateLimits >= RATE_LIMIT.RATE_LIMIT_MAX_RETRIES) {
            this.pausedUntil = this.clock.now() + RATE_LIMIT.CONSECUTIVE_LIMIT_PAUSE_MS;
            log.warn({ pauseMs: RATE_LIMIT.CONSECUTIVE_LIMIT_PAUSE_MS }, '3 consecutive rate limits — pausing all requests');
            this.consecutiveRateLimits = 0;
            await sleep(RATE_LIMIT.CONSECUTIVE_LIMIT_PAUSE_MS);
          } else {
            const backoff = randomBetween(RATE_LIMIT.RATE_LIMIT_BACKOFF_MIN_MS, RATE_LIMIT.RATE_LIMIT_BACKOFF_MAX_MS);
            log.warn({ attempt: attempts, backoffMs: backoff }, 'Rate limit hit — backing off');
            await sleep(backoff);
          }
        } else {
          throw err;
        }
      }
    }
  }

  getConsecutiveRateLimits() { return this.consecutiveRateLimits; }
}

function isRateLimitError(err: unknown): boolean {
  if (err && typeof err === 'object' && 'retCode' in err) {
    return (err as { retCode: number }).retCode === 10006;
  }
  return false;
}

function randomBetween(min: number, max: number): number {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}

export const rateLimiter = new RateLimiter();
