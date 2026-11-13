import React from 'react';

// Strike selection stored as the existing string format: "ATM", "ITM n", "OTM n" (n = strike steps from ATM).
export function parseStrike(v) {
  const m = /^(ITM|OTM)[ _]?(\d+)$/i.exec(String(v || 'ATM').trim());
  return m ? { mode: m[1].toUpperCase(), steps: Math.max(1, parseInt(m[2], 10)) } : { mode: 'ATM', steps: 0 };
}
export const formatStrike = (mode, steps) => (mode === 'ATM' ? 'ATM' : `${mode} ${Math.max(1, Math.min(50, Number(steps) || 1))}`);

// Computes the concrete strike for a leg from live ATM + step. CE: OTM is above ATM; PE: OTM is below.
export function resolveStrike(atm, step, strike, optionType) {
  if (!atm || !step) return null;
  const { mode, steps } = parseStrike(strike);
  if (mode === 'ATM') return { strike: atm, distance: 0, steps: 0 };
  const dir = optionType === 'PE' ? -1 : 1;
  const sign = mode === 'OTM' ? dir : -dir;
  return { strike: atm + sign * steps * step, distance: steps * step, steps };
}

export function StrikeSelector({ leg, onChange, atm, step }) {
  const { mode, steps } = parseStrike(leg.strike);
  const isOpt = leg.instrumentType === 'OPTION';
  if (!isOpt) return <small className="muted">n/a</small>;
  const res = resolveStrike(atm, step, leg.strike, leg.optionType);
  return (
    <div className="strike-sel">
      <div className="strike-row">
        <select value={mode} onChange={e => onChange(formatStrike(e.target.value, steps || 1))} aria-label="Moneyness">
          <option value="ATM">ATM</option><option value="ITM">ITM</option><option value="OTM">OTM</option>
        </select>
        {mode !== 'ATM' && (
          <input type="number" min="1" max="50" value={steps} aria-label="Strike steps from ATM"
            onChange={e => onChange(formatStrike(mode, e.target.value))} title="Distance in strike steps (intervals)" />
        )}
      </div>
      <small className="strike-preview">
        {res
          ? `${res.strike} ${leg.optionType}${res.steps ? ` • ${res.steps} step${res.steps > 1 ? 's' : ''} (${res.distance} pts) ${mode === 'ITM' ? 'ITM' : 'OTM'}` : ' • at ATM'}`
          : 'Distance in strike steps'}
      </small>
    </div>
  );
}
export default StrikeSelector;
