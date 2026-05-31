-- Trading mode column on agent_state.
-- agent_config stores NUMERIC params only (double precision value column).
-- trading_mode is a text state-machine value so it belongs on agent_state.
--
-- shadow       → paper=True,  testnet API  (default — no real orders)
-- testnet_live → paper=False, testnet API  (real fills; feeds promotion gate)
-- mainnet_live → paper=False, mainnet API  (real money)

ALTER TABLE agent_state
  ADD COLUMN IF NOT EXISTS trading_mode TEXT NOT NULL DEFAULT 'shadow';
