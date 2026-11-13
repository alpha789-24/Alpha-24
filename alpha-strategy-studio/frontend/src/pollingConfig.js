export const WS_CONNECTED_MARKET_POLL_MS = 45000;
export const WS_DISCONNECTED_MARKET_POLL_MS = 5000;
export const OPERATIONAL_POLL_MS = 7500;

/**
 * Return REST polling cadence for /api/market/live in milliseconds.
 * When the WebSocket stream is open, reduce market polling to 45s (30-60s);
 * when disconnected, keep the 5s fallback cadence.
 */
export function getMarketPollIntervalMs(isWsOpen) {
  return isWsOpen ? WS_CONNECTED_MARKET_POLL_MS : WS_DISCONNECTED_MARKET_POLL_MS;
}

/**
 * Operational endpoints (trades, paper worker runs, IIFL status, broker orders)
 * stay on a 7.5s (5-10s) cadence regardless of WebSocket state.
 */
export function getOperationalPollIntervalMs() {
  return OPERATIONAL_POLL_MS;
}

export const getRestPollIntervalMs = getMarketPollIntervalMs;
