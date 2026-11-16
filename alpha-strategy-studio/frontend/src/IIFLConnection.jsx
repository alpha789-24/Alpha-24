/**
 * IIFLConnection.jsx
 * Redesigned IIFL broker integration page — Part 1
 *
 * Rules enforced:
 *  - No hardcoded colours; all from theme variables in style.css
 *  - No browser storage of authCode / session token
 *  - authCode input is type="password", cleared after submit
 *  - App Key / App Secret never appear in the browser
 *  - Four status chips driven by four booleans from /api/iifl/status
 *  - Developer setup collapsed by default
 *  - LIVE execution hard-blocked (no order/trading code)
 */
import React, { useState } from 'react';
import {
  ShieldCheck, ExternalLink, LogOut, ChevronDown, ChevronRight,
  CheckCircle2, Circle, AlertCircle, XCircle, Info
} from 'lucide-react';

/** ─── Status chip ─────────────────────────────────────────────────────────── */
function StatusChip({ state, label, message }) {
  // state: 'pending' | 'success' | 'warning' | 'error'
  const map = {
    pending: { cls: 'iifl-chip iifl-chip--pending', Icon: Circle },
    success: { cls: 'iifl-chip iifl-chip--success', Icon: CheckCircle2 },
    warning: { cls: 'iifl-chip iifl-chip--warning', Icon: AlertCircle },
    error:   { cls: 'iifl-chip iifl-chip--error',   Icon: XCircle },
  };
  const { cls, Icon } = map[state] ?? map.pending;
  return (
    <div className={cls} role="status" aria-label={`${label}: ${state}`}>
      <Icon size={13} className="iifl-chip__icon" />
      <div className="iifl-chip__body">
        <span className="iifl-chip__label">{label}</span>
        {message && <span className="iifl-chip__msg">{message}</span>}
      </div>
    </div>
  );
}

/** ─── Wizard step ─────────────────────────────────────────────────────────── */
function WizardStep({ number, title, description, children, active, done }) {
  return (
    <div className={`iifl-step ${active ? 'iifl-step--active' : ''} ${done ? 'iifl-step--done' : ''}`}>
      <div className="iifl-step__indicator">
        {done
          ? <CheckCircle2 size={20} className="iifl-step__check" />
          : <span className="iifl-step__num">{number}</span>
        }
        <div className="iifl-step__line" aria-hidden="true" />
      </div>
      <div className="iifl-step__content">
        <div className="iifl-step__header">
          <span className="iifl-step__title">{title}</span>
          <span className="iifl-step__desc">{description}</span>
        </div>
        {(active || done) && children && (
          <div className="iifl-step__actions">{children}</div>
        )}
      </div>
    </div>
  );
}

/** ─── Helper text block ───────────────────────────────────────────────────── */
function HelperNote({ children }) {
  return (
    <div className="iifl-helper">
      <Info size={13} className="iifl-helper__icon" />
      <span>{children}</span>
    </div>
  );
}

/** ─── Main component ─────────────────────────────────────────────────────── */
export function IIFLConnection({ iifl, notify, loadData, API }) {
  const [devOpen, setDevOpen] = useState(false);
  const [authCodeRef] = useState(() => ({ current: null }));

  /**
   * iifl shape (from /api/iifl/status – booleans only):
   *   credentialsConfigured: boolean   — App Key + Secret present in backend env
   *   authenticationCompleted: boolean — session was successfully created
   *   profileVerified: boolean         — profile fetch succeeded for this session
   *   sessionActive: boolean           — session is live & not expired
   *   sessionExpired: boolean          — session was present but expired (for reconnect UI)
   */
  const {
    credentialsConfigured = false,
    authenticationCompleted = false,
    profileVerified = false,
    sessionActive = false,
    sessionExpired = false,
    // legacy keys — still respected for backward compat
    configured,
    sessionPresent,
  } = iifl ?? {};

  // Normalize to new keys if only legacy keys are present
  const creds    = iifl?.credentialsConfigured ?? iifl?.configured ?? false;
  const authed   = iifl?.authenticationCompleted ?? iifl?.sessionPresent ?? false;
  const profiled = iifl?.profileVerified ?? false;
  const active   = iifl?.sessionActive ?? iifl?.sessionPresent ?? false;
  const expired  = iifl?.sessionExpired ?? false;

  // Which wizard step is "current"
  const currentStep = !creds ? 1 : !authed ? 2 : !profiled ? 3 : !active ? 3 : 4;

  // Chip states
  function credChipState() { return creds ? 'success' : 'pending'; }
  function credChipMsg()   { return creds ? 'App Key and Secret detected in backend environment.' : 'Add IIFL_APP_KEY and IIFL_APP_SECRET to backend/.env (see Developer setup below).'; }

  function authChipState() {
    if (authed && active) return 'success';
    if (expired) return 'warning';
    if (authed) return 'success';
    return 'pending';
  }
  function authChipMsg() {
    if (expired) return 'Session expired — reconnect with a fresh authCode.';
    if (authed)  return 'Session token obtained for today.';
    return 'Complete the IIFL login and exchange your authCode below.';
  }

  function profChipState() {
    if (!authed) return 'pending';
    return profiled ? 'success' : 'warning';
  }
  function profChipMsg() {
    if (!authed)  return 'Complete authentication first.';
    if (profiled) return 'Profile and exchange permissions confirmed.';
    return 'Click "Verify profile" to confirm exchange permissions.';
  }

  function sessChipState() {
    if (expired)       return 'error';
    if (active)        return 'success';
    if (authed)        return 'warning';
    return 'pending';
  }
  function sessChipMsg() {
    if (expired)   return 'Session expired. Use a fresh authCode to reconnect.';
    if (active)    return 'Live and ready for market data.';
    if (authed)    return 'Session created — verify profile to confirm.';
    return 'No active session.';
  }

  /* ─── API actions ──────────────────────────────────────────────────────── */
  async function openLogin() {
    try {
      const r = await fetch(`${API}/iifl/login-url`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail);
      window.open(d.url, '_blank', 'noopener,noreferrer');
    } catch (e) {
      notify(e.message || 'Start backend and configure App Key first');
    }
  }

  async function generateSession(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const fd = new FormData(form);
    try {
      const r = await fetch(`${API}/iifl/session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientId: fd.get('clientId'), authCode: fd.get('authCode') }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail);
      notify(d.message || 'Session created');
      form.reset();
      loadData();
    } catch (err) {
      notify(err.message);
    }
  }

  async function verifyProfile() {
    try {
      const r = await fetch(`${API}/iifl/profile`);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        if (r.status === 401) loadData();
        throw new Error(err.detail || 'Session expired, reconnect');
      }
      notify('IIFL profile retrieved successfully');
      loadData();
    } catch (e) {
      notify(e.message);
    }
  }

  async function logoutSession() {
    try {
      await fetch(`${API}/iifl/logout`, { method: 'POST' });
      notify('Local session cleared');
      loadData();
    } catch {
      notify('Backend unavailable');
    }
  }

  /* ─── Progress indicator (percentage) ─────────────────────────────────── */
  const progressPct = Math.round(([creds, authed, profiled, active].filter(Boolean).length / 4) * 100);

  return (
    <section className="content">
      {/* ── Page title ── */}
      <div className="welcome-row" style={{ marginBottom: 16 }}>
        <div>
          <div className="eyebrow">BROKER INTEGRATION</div>
          <h1 className="iifl-page-title">IIFL Connection</h1>
          <p className="sub" style={{ marginTop: 4 }}>
            Connect your IIFL Markets account securely — credentials stay on the backend.
          </p>
        </div>
      </div>

      {/* ── Credential safety notice ── */}
      <div className="notice" style={{ marginBottom: 16 }}>
        <ShieldCheck size={17} />
        <div>
          <b>Credential Safety</b>
          <span>
            Your App Key, App Secret, authCode and session token are handled exclusively in backend
            process memory. They are never sent to the browser, stored in browser storage, or logged.
          </span>
        </div>
      </div>

      {/* ── Status chips row ── */}
      <div className="iifl-chips-row">
        <StatusChip state={credChipState()} label="App credentials configured"  message={credChipMsg()} />
        <StatusChip state={authChipState()} label="Authentication completed"    message={authChipMsg()} />
        <StatusChip state={profChipState()} label="Profile verified"            message={profChipMsg()} />
        <StatusChip state={sessChipState()} label="Session active"              message={sessChipMsg()} />
      </div>

      {/* ── Progress bar ── */}
      <div className="iifl-progress" role="progressbar" aria-valuenow={progressPct} aria-valuemin={0} aria-valuemax={100} aria-label="Connection setup progress">
        <div className="iifl-progress__track">
          <div className="iifl-progress__fill" style={{ width: `${progressPct}%` }} />
        </div>
        <span className="iifl-progress__label">{progressPct}% complete</span>
      </div>

      {/* ── Four-step wizard ── */}
      <div className="panel iifl-wizard">
        <WizardStep
          number={1}
          title="App credentials"
          description="Put IIFL_APP_KEY and IIFL_APP_SECRET in backend/.env"
          active={currentStep === 1}
          done={creds}
        >
          <HelperNote>
            Developer-only step — see the "Developer setup" section below. No browser input needed.
          </HelperNote>
        </WizardStep>

        <WizardStep
          number={2}
          title="Connect IIFL account"
          description="Open the official IIFL login page and authenticate with your trading credentials and OTP"
          active={currentStep === 2}
          done={authed && !expired}
        >
          <div className="iifl-btn-row">
            <button
              type="button"
              className="primary"
              onClick={openLogin}
              disabled={!creds}
              title={!creds ? 'Configure App credentials first' : 'Opens IIFL login in a new tab'}
            >
              <ExternalLink size={15} /> Open IIFL login
            </button>
          </div>
          <HelperNote>
            After login, IIFL redirects back and shows a one-time <strong>authCode</strong> in the URL.
            Copy it and paste it in Step 3. It expires in minutes — use it immediately.
          </HelperNote>
        </WizardStep>

        <WizardStep
          number={3}
          title="Complete authentication"
          description="Paste your one-time authCode — the backend generates the SHA-256 checksum and exchanges for a userSession"
          active={currentStep >= 2 || expired}
          done={authed && !expired}
        >
          <form onSubmit={generateSession} autoComplete="off" noValidate>
            <div className="iifl-form-row">
              <label className="iifl-field">
                <span className="iifl-field__label">Client ID</span>
                <input
                  name="clientId"
                  required
                  autoComplete="off"
                  placeholder="Your IIFL client ID"
                  className="iifl-input"
                />
              </label>
              <label className="iifl-field">
                <span className="iifl-field__label">authCode</span>
                <input
                  name="authCode"
                  required
                  type="password"
                  autoComplete="new-password"
                  placeholder="Paste authCode from IIFL redirect"
                  className="iifl-input"
                />
              </label>
            </div>
            <HelperNote>
              <strong>authCode</strong> — a one-time code IIFL includes in the redirect URL after you log in.
              It is sent directly to your local backend; never stored in this browser.
              The backend computes a <strong>SHA-256 checksum</strong> (hash of App Key + App Secret + authCode)
              that IIFL requires to exchange for a <strong>userSession</strong> token. The token stays on
              the backend only.
            </HelperNote>
            <div className="iifl-btn-row" style={{ marginTop: 10 }}>
              <button type="submit" className="primary" disabled={!creds}>
                <ShieldCheck size={15} /> Generate IIFL session
              </button>
              <button
                type="button"
                className="outline danger-outline"
                onClick={logoutSession}
                title="Clear local session"
              >
                <LogOut size={14} /> Logout session
              </button>
            </div>
          </form>
        </WizardStep>

        <WizardStep
          number={4}
          title="Verify profile and exchange permissions"
          description="Confirm enabled exchanges (NSE / BSE / NFO) and that the session is live"
          active={currentStep === 4 || (authed && !profiled)}
          done={profiled && active}
        >
          <div className="iifl-btn-row">
            <button
              type="button"
              className="primary"
              onClick={verifyProfile}
              disabled={!authed}
              title={!authed ? 'Complete authentication first' : 'Fetch IIFL profile from backend'}
            >
              <ShieldCheck size={15} /> Verify profile
            </button>
          </div>
          <HelperNote>
            Calls the backend, which fetches your IIFL profile and reports which exchanges are enabled.
            No data is stored in the browser.
          </HelperNote>
        </WizardStep>
      </div>

      {/* ── Developer setup (collapsed) ── */}
      <div className="iifl-devsetup">
        <button
          type="button"
          className="iifl-devsetup__toggle"
          onClick={() => setDevOpen(o => !o)}
          aria-expanded={devOpen}
          aria-controls="iifl-devsetup-body"
        >
          <span className="iifl-devsetup__badge">DEV</span>
          <span>Developer setup — backend environment variables</span>
          {devOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        </button>
        {devOpen && (
          <div id="iifl-devsetup-body" className="iifl-devsetup__body">
            <p className="iifl-devsetup__note">
              These values are set <strong>once</strong> in <code>backend/.env</code> by the developer who
              registers the IIFL developer application. They are never entered in the browser.
            </p>
            <div className="iifl-devsetup__table">
              <div className="iifl-devsetup__row">
                <code>IIFL_APP_KEY</code>
                <span>Your IIFL developer application key (from IIFL Markets developer portal).</span>
              </div>
              <div className="iifl-devsetup__row">
                <code>IIFL_APP_SECRET</code>
                <span>Corresponding secret. Keep it out of version control — add <code>backend/.env</code> to <code>.gitignore</code>.</span>
              </div>
              <div className="iifl-devsetup__row">
                <code>IIFL_REDIRECT_URL</code>
                <span>The redirect URL registered with IIFL (e.g. <code>http://127.0.0.1:8000/api/iifl/callback</code>).</span>
              </div>
            </div>
            <p className="iifl-devsetup__note" style={{ marginTop: 10 }}>
              Once set, restart the backend. The connection status chips above will update automatically.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

export default IIFLConnection;

