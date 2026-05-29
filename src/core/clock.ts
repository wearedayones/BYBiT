export interface Clock {
  now(): number;
  nowDate(): Date;
}

export const realClock: Clock = {
  now: () => Date.now(),
  nowDate: () => new Date(),
};

export function makeMockClock(startMs: number): Clock & { advance(ms: number): void } {
  let t = startMs;
  return {
    now: () => t,
    nowDate: () => new Date(t),
    advance(ms: number) { t += ms; },
  };
}
