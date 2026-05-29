import Bottleneck from 'bottleneck';
import { RATE_LIMIT } from '../config/constants';
import { childLogger } from '../core/logger';
import type { Clock } from '../core/clock';
import { realClock } from '../core/clock';

const log = childLogger({ module: 'rate-limiter' });

// Bybit rate-limit headers on every response
const HDR_LIMIT  = 'x-bapi-limit';
const HDR_STATUS = 'x-bapi-limit-status';  // remaining requests in window
const HDR_RESET  = 'x-bapi-limit-reset-timestamp'; // epoch ms when window resets

const PREEMPTIVE_SLOW_THRESHOLD = 0.80;   // start slowing at 80% usage
const PREEMPTIVE_STOP_THRESHOLD = 0.96;   // hard-pause at 96% usage

interface RateLimitBucket {
  limit: number;
  remaining: number;
  resetAt: number;
  endpoint: string;
}

export class RateLimiter {
  private readonly getLane: Bottleneck;
  private readonly postLane: Bottleneck;
  private consecutiveRateLimits = 0;
  private pausedUntil = 0;
  private readonly clock: Clock;
  private readonly buckets: Map<string, RateLimitBucket> = new Map();

  constructor(clock: Clock = realClock) {
    this.clock = clock;
    this.getLane  = new Bottleneck({ minTime: RATE_LIMIT.GET_MIN_MS,  maxConcurrent: 10 });
    this.postLane = new Bottleneck({ minTime: RATE_LIMIT.POST_MIN_MS, maxConcurrent: 5 });
  }

  async scheduleGet<T>(fn: () => Promise<T>): Promise<T> {
    return this.getLane.schedule(() => this.withBackoff(fn));
  }

  async schedulePost<T>(fn: () => Promise<T>): Promise<T> {
    return this.postLane.schedule(() => this.withBackoff(fn));
  }

  /**
   * Called after every HTTP response. Parses Bybit's rate-limit headers and
   * pre-emptively pauses the queues before the exchange enforces a hard block.
   */
  updateFromHeaders(
    headers: Record<string, string | string[] | undefined>,
    endpoint: string,
  ): void {
    const get = (k: string): string => {
      const v = headers[k];
      return Array.isArray(v) ? v[0] : (v ?? '');
    };

    const limit     = parseInt(get(HDR_LIMIT), 10);
    const remaining = parseInt(get(HDR_STATUS), 10);
    const resetAt   = parseInt(get(HDR_RESET), 10);

    if (!limit || isNaN(limit) || isNaN(remaining) || isNaN(resetAt)) return;

    const bucket: RateLimitBucket = { limit, remaining, resetAt, endpoint };
    this.buckets.set(endpoint, bucket);

    const used  = limit - remaining;
    const usage = used / limit;

    if (usage >= PREEMPTIVE_STOP_THRESHOLD) {
      // Nearly exhausted — pause until the window resets
      const pauseMs = Math.max(0, resetAt - this.clock.now()) + 50;
      if (pauseMs > 0 && this.clock.now() < resetAt) {
        this.pausedUntil = Math.max(this.pausedUntil, resetAt + 50);
        log.warn(
          { endpoint, remaining, limit, resetAt, pauseMs },
          'Rate limit critical — pausing until window reset',
        );
      }
    } else if (usage >= PREEMPTIVE_SLOW_THRESHOLD) {
      // Approaching limit — log once per endpoint per window
      log.debug(
        { endpoint, remaining, limit, usagePct: Math.round(usage * 100) },
        'Rate limit approaching — traffic will slow naturally',
      );
    }
  }

  /** Returns the live status for an endpoint, useful for diagnostics. */
  getBucket(endpoint: string): RateLimitBucket | undefined {
    return this.buckets.get(endpoint);
  }

  /** Returns the worst (most-used) bucket across all tracked endpoints. */
  getWorstBucket(): RateLimitBucket | undefined {
    let worst: RateLimitBucket | undefined;
    for (const b of this.buckets.values()) {
      if (!worst || (b.limit - b.remaining) / b.limit > (worst.limit - worst.remaining) / worst.limit) {
        worst = b;
      }
    }
    return worst;
  }

  getConsecutiveRateLimits() { return this.consecutiveRateLimits; }

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
            log.warn(
              { pauseMs: RATE_LIMIT.CONSECUTIVE_LIMIT_PAUSE_MS },
              '3 consecutive rate limits — pausing all requests',
            );
            this.consecutiveRateLimits = 0;
            await sleep(RATE_LIMIT.CONSECUTIVE_LIMIT_PAUSE_MS);
          } else {
            const backoff = randomBetween(
              RATE_LIMIT.RATE_LIMIT_BACKOFF_MIN_MS,
              RATE_LIMIT.RATE_LIMIT_BACKOFF_MAX_MS,
            );
            log.warn({ attempt: attempts, backoffMs: backoff }, 'Rate limit hit — backing off');
            await sleep(backoff);
          }
        } else {
          throw err;
        }
      }
    }
  }
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
