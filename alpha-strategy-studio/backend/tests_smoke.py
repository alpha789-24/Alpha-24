from app.strategy import new_strategy, validate_strategy, compile_strategy
from app.market_service import market_service

# 1. Strategy logic smoke tests
s = new_strategy()
assert validate_strategy(s) == []
assert compile_strategy(s)['schemaVersion'] == 1
s['risk']['maxDailyLoss'] = 0
assert any('daily loss' in e.lower() for e in validate_strategy(s))
print('Strategy validation smoke tests passed.')

# 2. Live market data smoke tests
quote = market_service.get_quote('NIFTY 50')
assert quote.get('ltp', 0) > 0
assert 'change' in quote
assert 'ticker' in quote
print(f"Live market quote passed: {quote['symbol']} @ {quote['ltp']} ({quote['changePercent']}%)")

# 3. Dynamic strike computation smoke tests
strikes = market_service.calculate_strikes('NIFTY 50', quote['ltp'])
assert 'ATM' in strikes
assert 'CE' in strikes['ATM']
assert 'PE' in strikes['ATM']
print(f"Strike calculations passed: ATM Strike is {strikes['ATM']['strike']}")

# 4. Chart intraday candles smoke tests
chart = market_service.get_chart('NIFTY 50', range_period='1d', interval='5m')
assert 'candles' in chart
print(f"Intraday candle fetch passed: {len(chart['candles'])} candles loaded.")

print('All system smoke tests passed cleanly!')
