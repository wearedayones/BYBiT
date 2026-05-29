export class BybitApiError extends Error {
  constructor(
    public readonly retCode: number,
    public readonly retMsg: string,
    public readonly endpoint: string,
  ) {
    super(`Bybit API error ${retCode} on ${endpoint}: ${retMsg}`);
    this.name = 'BybitApiError';
  }
}

export class BotApiError extends Error {
  constructor(
    public readonly statusCode: number,
    public readonly debugMsg: string,
    public readonly endpoint: string,
  ) {
    super(`Bot API error ${statusCode} on ${endpoint}: ${debugMsg}`);
    this.name = 'BotApiError';
  }
}

export class RiskVetoError extends Error {
  constructor(public readonly reason: string) {
    super(`Risk veto: ${reason}`);
    this.name = 'RiskVetoError';
  }
}

export class KillSwitchError extends Error {
  constructor(public readonly reason: string) {
    super(`Kill switch engaged: ${reason}`);
    this.name = 'KillSwitchError';
  }
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}
