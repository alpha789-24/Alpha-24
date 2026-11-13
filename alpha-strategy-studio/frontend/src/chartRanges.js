// Quick history ranges (data-provider limits apply: e.g. 1m candles only go back ~7 days, intraday ~60 days).
export const RANGES = [
  { v: '1d', l: 'Today', t: 'today' }, { v: '5d', l: '5D', t: '5 days' }, { v: '1mo', l: '1M', t: '1 month' },
  { v: '3mo', l: '3M', t: '3 months' }, { v: '6mo', l: '6M', t: '6 months' }, { v: '1y', l: '1Y', t: '1 year' }
];

export const INTERVALS = ['1m', '5m', '15m', '30m', '1h', '1d', '1wk'];
const MAX_DAYS = { '1m': 7, '5m': 60, '15m': 60, '30m': 60, '1h': 730, '1d': 36500, '1wk': 36500 };
const RANGE_DAYS = { '1d': 1, '5d': 5, '1mo': 30, '3mo': 90, '6mo': 180, '1y': 365 };
export const intervalAllowed = (range, interval) => (RANGE_DAYS[range] ?? 1) <= (MAX_DAYS[interval] ?? 1);
export const bestIntervalFor = (range, current) =>
  intervalAllowed(range, current) ? current : (['5m', '15m', '30m', '1h', '1d'].find(i => intervalAllowed(range, i)) || '1d');

