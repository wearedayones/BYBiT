import {
  EMA, RSI, MACD, ATR, BollingerBands, ADX,
} from 'technicalindicators';

export interface OHLCVData {
  open: number[];
  high: number[];
  low: number[];
  close: number[];
  volume: number[];
}

export interface MACDResult {
  MACD?: number;
  signal?: number;
  histogram?: number;
}

export interface BollingerResult {
  upper: number;
  middle: number;
  lower: number;
}

export interface ADXResult {
  adx: number;
  pdi: number;
  mdi: number;
}

export function ema(values: number[], period: number): number[] {
  return EMA.calculate({ values, period });
}

export function rsi(values: number[], period = 14): number[] {
  return RSI.calculate({ values, period });
}

export function macd(values: number[], fastPeriod = 12, slowPeriod = 26, signalPeriod = 9): MACDResult[] {
  return MACD.calculate({
    values,
    fastPeriod,
    slowPeriod,
    signalPeriod,
    SimpleMAOscillator: false,
    SimpleMASignal: false,
  });
}

export function atr(ohlcv: OHLCVData, period = 14): number[] {
  return ATR.calculate({
    high: ohlcv.high,
    low: ohlcv.low,
    close: ohlcv.close,
    period,
  });
}

export function bollinger(values: number[], period = 20, stdDev = 2): BollingerResult[] {
  return BollingerBands.calculate({ values, period, stdDev });
}

export function adx(ohlcv: OHLCVData, period = 14): ADXResult[] {
  return ADX.calculate({
    high: ohlcv.high,
    low: ohlcv.low,
    close: ohlcv.close,
    period,
  });
}

export function last<T>(arr: T[]): T | undefined {
  return arr[arr.length - 1];
}
