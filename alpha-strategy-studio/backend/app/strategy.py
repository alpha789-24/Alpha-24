from datetime import datetime, timezone
from uuid import uuid4

from .schema_v2 import compile_rule_builder, rule_builder_warnings, validate_rule_builder

INDICATORS = {'RSI','EMA','SMA','SUPERTREND','VWAP','MACD','ATR','ADX','BOLLINGER'}
OPERATORS = {'>','<','>=','<=','=','CROSSES_ABOVE','CROSSES_BELOW'}

def validate_strategy(s):
    errors=[]
    if not isinstance(s, dict):
        return ['Strategy must be an object.']
    if not s.get('name','').strip(): errors.append('Strategy name is required.')

    strategy_type = s.get('strategyType', 'INDICATOR')

    if strategy_type == 'LEVEL_SAR':
        from .sar_engine import validate_sar
        sar_errors, _ = validate_sar(s)
        errors.extend(sar_errors)
    elif strategy_type == 'RULE_BUILDER':
        errors.extend(validate_rule_builder(s))
        return errors
    else:
        entry=s.get('entry', {})
        if not entry.get('conditions'): errors.append('Add at least one entry condition.')
        for i,c in enumerate(entry.get('conditions', []), 1):
            if c.get('indicator') not in INDICATORS: errors.append(f'Entry condition {i}: unsupported indicator.')
            if c.get('operator') not in OPERATORS: errors.append(f'Entry condition {i}: unsupported operator.')
        for p in s.get('positions', []):
            if int(p.get('quantity',0)) < 1: errors.append('Every position quantity must be at least 1.')
            if p.get('side') not in ('BUY','SELL'): errors.append('Position side must be BUY or SELL.')

    risk=s.get('risk',{})
    if float(risk.get('maxDailyLoss',0)) <= 0: errors.append('Set a positive maximum daily loss.')
    if float(risk.get('maxCapital',0)) <= 0: errors.append('Set a positive capital cap.')
    return errors

def strategy_warnings(s):
    if not isinstance(s, dict):
        return []
    strategy_type = s.get('strategyType', 'INDICATOR')
    if strategy_type == 'RULE_BUILDER':
        return rule_builder_warnings(s)
    if strategy_type == 'LEVEL_SAR':
        from .sar_engine import validate_sar
        _, warnings = validate_sar(s)
        return warnings
    return []

def compile_strategy(s):
    # Deterministic JSON strategy definition; never eval user-entered Python.
    if isinstance(s, dict) and s.get('strategyType') == 'RULE_BUILDER':
        return compile_rule_builder(s)
    return {'schemaVersion':1,'strategyId':s.get('id'),'name':s.get('name'),
            'instrument':s.get('instrument'),'timeframe':s.get('timeframe'),
            'strategyType':s.get('strategyType', 'INDICATOR'),
            'entry':s.get('entry',{}),'positions':s.get('positions',[]),
            'sar':s.get('sar',{}),
            'repair':s.get('repair',{}),'overnight':s.get('overnight',{}),
            'universalExit':s.get('universalExit',{}),'risk':s.get('risk',{}),
            'advanced':s.get('advanced',{}),'compiledAt':datetime.now(timezone.utc).isoformat()}

def new_strategy():
    return {'id':str(uuid4()),'name':'New Strategy','description':'','instrument':'NIFTY 50','timeframe':'5m',
      'strategyType':'INDICATOR',
      'status':'DRAFT','entry':{'logic':'AND','conditions':[{'indicator':'SUPERTREND','operator':'CROSSES_ABOVE','value':'PRICE','period':10,'multiplier':3}]},
      'positions':[{'side':'BUY','underlying':'NIFTY','instrumentType':'OPTION','strike':'ATM','optionType':'CE','expiry':'CURRENT_WEEK','quantity':1,'orderType':'MARKET'}],
      'repair':{'enabled':False,'maxAttempts':1,'cooldownMinutes':5},'overnight':{'enabled':False,'action':'SQUARE_OFF'},
      'universalExit':{'enabled':True,'conditions':[]},'risk':{'maxDailyLoss':2000,'maxCapital':25000,'maxTradesPerDay':3,'maxOpenPositions':2},
      'advanced':{'checkFrequency':'CONTINUOUS','startAfterOpenMinutes':5,'stopBeforeCloseMinutes':15,'tranchePercent':100,'trancheIntervalSeconds':1,'reactivateAfterHours':6,'rollover':'NONE'},
      'sar':{'levelSource':'MANUAL','entryStyle':'REVERSAL','manual':{'upper':330,'lower':325},'candle':{'timeframe':'15m','upperSource':'HIGH','lowerSource':'LOW'},'slPoints':1,'tgtPoints':1.5,'baseQuantity':100,'multipliers':[3,2,2],'maxLegs':4,'startTime':'09:15','endTime':'15:00','afterTarget':'STOP','maxRestarts':2}}
