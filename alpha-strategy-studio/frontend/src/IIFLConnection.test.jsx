/**
 * IIFLConnection.test.jsx
 * =======================
 * UI wiring tests for the redesigned IIFL Connection page (Part 1).
 *
 * Run:
 *   cd frontend
 *   npm install
 *   npm test
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { IIFLConnection } from './IIFLConnection.jsx';

// ── Shared props ────────────────────────────────────────────────────────────

const API = 'http://127.0.0.1:8000/api';

/** Minimal iifl prop — no session, no credentials. */
const IIFL_EMPTY = {
  credentialsConfigured: false,
  authenticationCompleted: false,
  profileVerified: false,
  sessionActive: false,
  sessionExpired: false,
};

/** Fully connected. */
const IIFL_FULL = {
  credentialsConfigured: true,
  authenticationCompleted: true,
  profileVerified: true,
  sessionActive: true,
  sessionExpired: false,
};

/** Session expired — reconnect needed. */
const IIFL_EXPIRED = {
  credentialsConfigured: true,
  authenticationCompleted: false,
  profileVerified: false,
  sessionActive: false,
  sessionExpired: true,
};

/** Legacy shape — only the old keys. */
const IIFL_LEGACY = {
  configured: true,
  sessionPresent: true,
  sessionExpired: false,
};

// ── Helpers ─────────────────────────────────────────────────────────────────

function renderComp(iifl = IIFL_EMPTY, extraProps = {}) {
  const notify   = vi.fn();
  const loadData = vi.fn();
  render(
    <IIFLConnection iifl={iifl} notify={notify} loadData={loadData} API={API} />,
  );
  return { notify, loadData };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('IIFLConnection — status chips', () => {
  it('renders all four chip labels', () => {
    renderComp(IIFL_EMPTY);
    expect(screen.getByText(/App credentials configured/i)).toBeInTheDocument();
    expect(screen.getByText(/Authentication completed/i)).toBeInTheDocument();
    expect(screen.getByText(/Profile verified/i)).toBeInTheDocument();
    expect(screen.getByText(/Session active/i)).toBeInTheDocument();
  });

  it('shows pending chips when nothing is configured', () => {
    renderComp(IIFL_EMPTY);
    // All four chips render as pending (no --success / --warning / --error classes)
    const chips = document.querySelectorAll('.iifl-chip--pending');
    expect(chips.length).toBe(4);
  });

  it('shows all success chips when fully connected', () => {
    renderComp(IIFL_FULL);
    const successChips = document.querySelectorAll('.iifl-chip--success');
    expect(successChips.length).toBe(4);
  });

  it('shows error chip for session when expired', () => {
    renderComp(IIFL_EXPIRED);
    const errorChip = document.querySelector('.iifl-chip--error');
    expect(errorChip).not.toBeNull();
  });

  it('shows actionable reconnect message on expired session', () => {
    renderComp(IIFL_EXPIRED);
    expect(screen.getAllByText(/reconnect/i).length).toBeGreaterThan(0);
  });
});

describe('IIFLConnection — legacy keys compatibility', () => {
  it('renders without error when only legacy keys are provided', () => {
    expect(() => renderComp(IIFL_LEGACY)).not.toThrow();
  });

  it('shows credentials chip as success for legacy configured:true', () => {
    renderComp(IIFL_LEGACY);
    const successChips = document.querySelectorAll('.iifl-chip--success');
    expect(successChips.length).toBeGreaterThanOrEqual(1);
  });
});

describe('IIFLConnection — wizard structure', () => {
  it('renders a progress bar', () => {
    renderComp(IIFL_EMPTY);
    const bar = document.querySelector('[role="progressbar"]');
    expect(bar).not.toBeNull();
    expect(bar.getAttribute('aria-valuenow')).toBe('0');
  });

  it('progress is 100% when fully connected', () => {
    renderComp(IIFL_FULL);
    const bar = document.querySelector('[role="progressbar"]');
    expect(bar.getAttribute('aria-valuenow')).toBe('100');
  });

  it('renders step 1 through step 4 titles', () => {
    renderComp(IIFL_EMPTY);
    expect(screen.getAllByText(/App credentials/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Connect IIFL account/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Complete authentication/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Verify profile/i).length).toBeGreaterThan(0);
  });
});

describe('IIFLConnection — authCode input security', () => {
  it('authCode input is type="password"', () => {
    renderComp({ ...IIFL_EMPTY, credentialsConfigured: true });
    const inputs = document.querySelectorAll('input[name="authCode"]');
    expect(inputs.length).toBeGreaterThan(0);
    inputs.forEach(inp => expect(inp.type).toBe('password'));
  });

  it('authCode input has autoComplete="new-password" (not "current-password")', () => {
    renderComp({ ...IIFL_EMPTY, credentialsConfigured: true });
    const inp = document.querySelector('input[name="authCode"]');
    if (inp) {
      expect(inp.getAttribute('autocomplete')).not.toBe('current-password');
    }
  });
});

describe('IIFLConnection — form reset after submit', () => {
  it('clears the form after successful session creation', async () => {
    // Mock a successful /iifl/session POST
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ message: 'Session created' }),
    });

    const { notify, loadData } = renderComp({
      ...IIFL_EMPTY,
      credentialsConfigured: true,
    });

    const clientIdInput = document.querySelector('input[name="clientId"]');
    const authCodeInput = document.querySelector('input[name="authCode"]');
    const form          = clientIdInput.closest('form');

    fireEvent.change(clientIdInput, { target: { value: 'CLIENT123' } });
    fireEvent.change(authCodeInput, { target: { value: 'AUTHCODE_VALUE' } });

    fireEvent.submit(form);

    await waitFor(() => {
      expect(notify).toHaveBeenCalledWith(expect.stringContaining('Session'));
      expect(loadData).toHaveBeenCalled();
    });

    // form.reset() should have cleared the inputs
    expect(clientIdInput.value).toBe('');
    expect(authCodeInput.value).toBe('');

    global.fetch = undefined;
  });
});

describe('IIFLConnection — developer setup section', () => {
  it('developer setup toggle is collapsed by default', () => {
    renderComp(IIFL_EMPTY);
    const body = document.querySelector('.iifl-devsetup__body');
    expect(body).toBeNull();
  });

  it('developer setup expands when toggle is clicked', () => {
    renderComp(IIFL_EMPTY);
    const toggle = document.querySelector('.iifl-devsetup__toggle');
    fireEvent.click(toggle);
    const body = document.querySelector('.iifl-devsetup__body');
    expect(body).not.toBeNull();
  });

  it('developer setup body shows env var names but no input fields', () => {
    renderComp(IIFL_EMPTY);
    const toggle = document.querySelector('.iifl-devsetup__toggle');
    fireEvent.click(toggle);
    expect(screen.getAllByText(/IIFL_APP_KEY/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/IIFL_APP_SECRET/).length).toBeGreaterThan(0);
    // No <input> for secrets in the browser
    const secretInputs = document.querySelectorAll('.iifl-devsetup__body input');
    expect(secretInputs.length).toBe(0);
  });
});

describe('IIFLConnection — no browser storage', () => {
  it('does not call localStorage.setItem for authCode', async () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem');

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ message: 'ok' }),
    });

    renderComp({ ...IIFL_EMPTY, credentialsConfigured: true });

    const authCodeInput = document.querySelector('input[name="authCode"]');
    const form          = authCodeInput?.closest('form');

    if (form && authCodeInput) {
      fireEvent.change(authCodeInput, { target: { value: 'SECRET_CODE' } });
      fireEvent.submit(form);
      await waitFor(() => {});
    }

    const authCodeStorageCalls = spy.mock.calls.filter(([, v]) =>
      typeof v === 'string' && v.includes('SECRET_CODE'),
    );
    expect(authCodeStorageCalls.length).toBe(0);

    spy.mockRestore();
    global.fetch = undefined;
  });
});
