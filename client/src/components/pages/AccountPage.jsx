import { useState, useRef, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { LogOut, KeyRound, User, Upload, Bot, Calendar, ShieldCheck } from 'lucide-react'
import ClearAllDialog from '../dialogs/ClearAllDialog.jsx'
import MfaSetupDialog from '../dialogs/MfaSetupDialog.jsx'
import { supabase } from '../../supabase.js'
import { useC } from '../../colors'
import api, { getErrorMessage } from '../../api.js'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '../ui/dialog.jsx'

export default function AccountPage({ user, onLogout }) {
  const C = useC()
  const queryClient = useQueryClient()

  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [pwStatus, setPwStatus] = useState(null) // { type: 'success'|'error', msg }
  const [pwLoading, setPwLoading] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [confirmLogout, setConfirmLogout] = useState(false)

  const [aiEnabled, setAiEnabled] = useState(true)
  const [aiSettingsLoading, setAiSettingsLoading] = useState(true)
  const [aiDataDeleting, setAiDataDeleting] = useState(false)
  const [aiDataMsg, setAiDataMsg] = useState(null)

  const [cycleDayInput, setCycleDayInput] = useState('1')
  const [cycleSaving, setCycleSaving] = useState(false)
  const [cycleError, setCycleError] = useState(null)
  const [cycleSaved, setCycleSaved] = useState(false)
  const cycleDayTouched = useRef(false)

  // Two-factor auth (TOTP) — not enforced anywhere yet (no step-up gate exists
  // in this app today), but available for any user to turn on voluntarily.
  // Step-up gating in front of a specific sensitive action (e.g. Plaid Link)
  // is a separate, later piece of work that reads this same enrolled-factor
  // state via supabase.auth.mfa.
  const [mfaFactors, setMfaFactors] = useState([])
  const [mfaLoading, setMfaLoading] = useState(true)
  const [mfaSetupOpen, setMfaSetupOpen] = useState(false)
  const [mfaUnenrollTarget, setMfaUnenrollTarget] = useState(null)
  const [mfaMsg, setMfaMsg] = useState(null)
  const [mfaDisableCode, setMfaDisableCode] = useState('')
  const [mfaDisabling, setMfaDisabling] = useState(false)
  const [mfaDisableError, setMfaDisableError] = useState('')

  const refreshMfaFactors = () => {
    setMfaLoading(true)
    supabase.auth.mfa.listFactors()
      .then(({ data }) => setMfaFactors(data?.totp ?? []))
      .finally(() => setMfaLoading(false))
  }

  useEffect(() => { refreshMfaFactors() }, [])

  const handleMfaEnrolled = () => {
    setMfaSetupOpen(false)
    refreshMfaFactors()
    setMfaMsg('Two-factor authentication enabled.')
  }

  const closeMfaDisableDialog = () => {
    setMfaUnenrollTarget(null)
    setMfaDisableCode('')
    setMfaDisableError('')
  }

  const handleMfaUnenroll = async (e) => {
    e.preventDefault()
    const factorId = mfaUnenrollTarget
    setMfaDisabling(true)
    setMfaDisableError('')
    // Unenrolling a *verified* factor requires an aal2 (MFA-verified) session,
    // but nothing in this app's login flow ever challenges for a code after
    // the initial enrollment — every session sits at aal1 regardless. Step up
    // here with a fresh code before unenrolling, or Supabase silently rejects
    // the unenroll and the UI would otherwise show "disabled" while the
    // factor is still active.
    const { error: verifyErr } = await supabase.auth.mfa.challengeAndVerify({ factorId, code: mfaDisableCode })
    if (verifyErr) {
      setMfaDisabling(false)
      setMfaDisableError(verifyErr.message)
      return
    }
    const { error: unenrollErr } = await supabase.auth.mfa.unenroll({ factorId })
    setMfaDisabling(false)
    if (unenrollErr) {
      setMfaDisableError(unenrollErr.message)
      return
    }
    closeMfaDisableDialog()
    refreshMfaFactors()
    setMfaMsg('Two-factor authentication disabled.')
  }

  useEffect(() => {
    api.get('/settings').then(r => {
      setAiEnabled(r.data.ai_enabled)
      // Guard against a late-resolving fetch (StrictMode's double-invoked mount
      // effect, or just a slow network) clobbering a value the user already
      // started editing before this response came back.
      if (!cycleDayTouched.current) setCycleDayInput(String(r.data.cycle_start_day))
      setAiSettingsLoading(false)
    }).catch(() => setAiSettingsLoading(false))
  }, [])

  const handleAiToggle = async (enabled) => {
    setAiEnabled(enabled)
    try {
      await api.put('/settings', { ai_enabled: enabled })
      if (!enabled) setAiDataMsg('AI features disabled. Your embedding data has been deleted.')
      else setAiDataMsg(null)
    } catch {
      setAiEnabled(!enabled)
    }
  }

  const handleCycleDaySave = async () => {
    const day = parseInt(cycleDayInput, 10)
    if (!day || day < 1 || day > 31) { setCycleError('Enter a day between 1 and 31'); return }
    setCycleSaving(true); setCycleError(null); setCycleSaved(false)
    try {
      await api.put('/settings', { cycle_start_day: day })
      setCycleSaved(true)
      queryClient.invalidateQueries()
      setTimeout(() => setCycleSaved(false), 2500)
    } catch (err) {
      setCycleError(getErrorMessage(err, 'Failed to save'))
    } finally {
      setCycleSaving(false)
    }
  }

  const handleDeleteAiData = async () => {
    setAiDataDeleting(true)
    setAiDataMsg(null)
    try {
      const { data } = await api.delete('/settings/ai-data')
      setAiDataMsg(`Deleted ${data.deleted} stored AI data record${data.deleted !== 1 ? 's' : ''}.`)
    } catch {
      setAiDataMsg('Failed to delete AI data.')
    } finally {
      setAiDataDeleting(false)
    }
  }

  const fileRef = useRef(null)
  const [importOpen, setImportOpen] = useState(false)
  const [importLoading, setImportLoading] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const [importError, setImportError] = useState('')

  const handleLegacyImport = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    setImportResult(null)
    setImportError('')
    setImportLoading(true)
    setImportOpen(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const { data } = await api.post('/import/legacy-db', form)
      setImportResult(data.imported)
    } catch (err) {
      setImportError(getErrorMessage(err, 'Import failed.'))
    } finally {
      setImportLoading(false)
    }
  }

  const handleChangePassword = async (e) => {
    e.preventDefault()
    setPwStatus(null)
    if (newPassword !== confirmPassword) {
      setPwStatus({ type: 'error', msg: 'New passwords do not match.' })
      return
    }
    if (newPassword.length < 8) {
      setPwStatus({ type: 'error', msg: 'Password must be at least 8 characters.' })
      return
    }
    setPwLoading(true)
    const { error } = await supabase.auth.updateUser({ password: newPassword })
    if (error) {
      setPwStatus({ type: 'error', msg: error.message })
    } else {
      setPwStatus({ type: 'success', msg: 'Password updated.' })
      setNewPassword('')
      setConfirmPassword('')
      setShowForm(false)
    }
    setPwLoading(false)
  }

  const section = {
    background: C.surface,
    border: `1px solid ${C.border}`,
    borderRadius: 16,
    padding: '20px 24px',
    marginBottom: 16,
  }

  const inputStyle = {
    width: '100%',
    padding: '9px 13px',
    borderRadius: 9,
    border: `1px solid ${C.border}`,
    background: C.bg,
    color: C.text,
    fontSize: 14,
    outline: 'none',
    boxSizing: 'border-box',
    fontFamily: 'inherit',
  }

  const label = {
    fontSize: 12,
    fontWeight: 600,
    color: C.muted,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
    marginBottom: 6,
    display: 'block',
  }

  return (
    <div style={{ maxWidth: 520, margin: '0 auto' }}>
      <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: '0 0 24px', letterSpacing: '-0.02em' }}>
        Account
      </h1>

      {/* Profile */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{
            width: 44, height: 44, borderRadius: '50%',
            background: C.primaryTint,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0,
          }}>
            <User size={20} color={C.primary} />
          </div>
          <div>
            <p style={{ margin: 0, fontWeight: 700, fontSize: 15, color: C.text }}>{user?.email}</p>
            <p style={{ margin: '2px 0 0', fontSize: 12, color: C.muted }}>Your account email</p>
          </div>
        </div>
      </div>

      {/* Password */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: showForm ? 20 : 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <KeyRound size={16} color={C.muted} />
            <span style={{ fontWeight: 600, fontSize: 14, color: C.text }}>Password</span>
          </div>
          {!showForm && (
            <button
              type="button"
              onClick={() => { setShowForm(true); setPwStatus(null) }}
              style={{
                padding: '6px 14px', borderRadius: 8, border: `1px solid ${C.border}`,
                background: 'transparent', color: C.text, fontSize: 13, fontWeight: 600,
                cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              Change
            </button>
          )}
        </div>

        {showForm && (
          <form onSubmit={handleChangePassword} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div>
              <label style={label}>New password</label>
              <input
                type="password"
                value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required
                style={inputStyle}
              />
            </div>
            <div>
              <label style={label}>Confirm new password</label>
              <input
                type="password"
                value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required
                style={inputStyle}
              />
            </div>

            {pwStatus && (
              <p style={{
                margin: 0, fontSize: 13, textAlign: 'left',
                color: pwStatus.type === 'success' ? '#4caf50' : '#e57373',
              }}>
                {pwStatus.msg}
              </p>
            )}

            <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
              <button
                type="submit"
                disabled={pwLoading}
                style={{
                  flex: 1, padding: '9px 0', borderRadius: 9, border: 'none',
                  background: C.primary, color: '#fff', fontWeight: 700, fontSize: 13,
                  cursor: pwLoading ? 'not-allowed' : 'pointer',
                  opacity: pwLoading ? 0.7 : 1, fontFamily: 'inherit',
                }}
              >
                {pwLoading ? '…' : 'Update password'}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); setPwStatus(null); setNewPassword(''); setConfirmPassword('') }}
                style={{
                  padding: '9px 16px', borderRadius: 9, border: `1px solid ${C.border}`,
                  background: 'transparent', color: C.muted, fontSize: 13, fontWeight: 600,
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>

      {/* Two-Factor Authentication */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <ShieldCheck size={16} color={C.muted} />
            <div>
              <p style={{ margin: 0, fontWeight: 600, fontSize: 14, color: C.text }}>Two-factor authentication</p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: C.muted }}>
                {mfaLoading
                  ? 'Checking status…'
                  : mfaFactors.length > 0
                    ? 'Enabled — your account requires a verification code to enroll or reconnect a linked bank.'
                    : 'Adds a verification code step for sensitive actions like linking a bank account.'}
              </p>
            </div>
          </div>
          {!mfaLoading && (
            mfaFactors.length > 0 ? (
              <button
                type="button"
                onClick={() => setMfaUnenrollTarget(mfaFactors[0].id)}
                style={{
                  padding: '6px 14px', borderRadius: 8, border: '1px solid rgba(229,115,115,0.4)',
                  background: 'rgba(229,115,115,0.08)', color: '#e57373', fontSize: 13, fontWeight: 600,
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Disable
              </button>
            ) : (
              <button
                type="button"
                onClick={() => setMfaSetupOpen(true)}
                style={{
                  padding: '6px 14px', borderRadius: 8, border: `1px solid ${C.border}`,
                  background: 'transparent', color: C.text, fontSize: 13, fontWeight: 600,
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Enable
              </button>
            )
          )}
        </div>
        {mfaMsg && <p style={{ margin: '10px 0 0', fontSize: 12, color: C.muted }}>{mfaMsg}</p>}
      </div>

      <MfaSetupDialog
        open={mfaSetupOpen}
        onOpenChange={setMfaSetupOpen}
        onEnrolled={handleMfaEnrolled}
      />

      <Dialog open={!!mfaUnenrollTarget} onOpenChange={v => { if (!v) closeMfaDisableDialog() }}>
        <DialogContent style={{ maxWidth: 380 }}>
          <DialogHeader>
            <DialogTitle>Disable two-factor authentication?</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleMfaUnenroll} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <p style={{ margin: 0, fontSize: 14, color: C.muted }}>
              Enter a current code from your authenticator app to confirm. You'll no longer be asked
              for a verification code for sensitive actions on this account.
            </p>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={mfaDisableCode}
              onChange={e => setMfaDisableCode(e.target.value.replace(/\D/g, ''))}
              placeholder="6-digit code"
              required
              autoFocus
              style={{
                width: '100%', padding: '9px 13px', borderRadius: 9, border: `1px solid ${C.border}`,
                background: C.bg, color: C.text, fontSize: 16, letterSpacing: '0.3em', textAlign: 'center',
                outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit',
              }}
            />
            {mfaDisableError && <p style={{ margin: 0, fontSize: 12, color: '#e57373' }}>{mfaDisableError}</p>}
            <DialogFooter>
              <button
                type="button"
                onClick={closeMfaDisableDialog}
                style={{
                  padding: '8px 16px', borderRadius: 8, border: `1px solid ${C.border}`,
                  background: 'transparent', color: C.text, fontSize: 13, fontWeight: 600,
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={mfaDisabling || mfaDisableCode.length !== 6}
                style={{
                  padding: '8px 16px', borderRadius: 8, border: 'none',
                  background: '#e57373', color: '#fff', fontSize: 13, fontWeight: 700,
                  cursor: (mfaDisabling || mfaDisableCode.length !== 6) ? 'not-allowed' : 'pointer',
                  opacity: (mfaDisabling || mfaDisableCode.length !== 6) ? 0.6 : 1, fontFamily: 'inherit',
                }}
              >
                {mfaDisabling ? 'Disabling…' : 'Disable'}
              </button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Legacy DB Import */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Upload size={16} color={C.muted} />
            <div>
              <p style={{ margin: 0, fontWeight: 600, fontSize: 14, color: C.text }}>Import from desktop app</p>
              <p style={{ margin: '2px 0 0', fontSize: 12, color: C.muted }}>Upload your budget.db file from the offline version</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => fileRef.current.click()}
            disabled={importLoading}
            style={{
              padding: '6px 14px', borderRadius: 8, border: `1px solid ${C.border}`,
              background: 'transparent', color: C.text, fontSize: 13, fontWeight: 600,
              cursor: importLoading ? 'not-allowed' : 'pointer', opacity: importLoading ? 0.6 : 1,
              fontFamily: 'inherit', flexShrink: 0,
            }}
          >
            Choose file
          </button>
          <input ref={fileRef} type="file" accept=".db" onChange={handleLegacyImport} style={{ display: 'none' }} />
        </div>
      </div>

      {/* Import progress / results dialog */}
      <Dialog open={importOpen} onOpenChange={v => { if (!importLoading) setImportOpen(v) }}>
        <DialogContent style={{ maxWidth: 400 }}>
          <DialogHeader>
            <DialogTitle>{importLoading ? 'Importing…' : importError ? 'Import failed' : 'Import complete'}</DialogTitle>
          </DialogHeader>

          {importLoading && (
            <div>
              <p style={{ margin: '0 0 14px', fontSize: 13, color: C.muted }}>Reading your budget.db — this may take a moment.</p>
              <style>{`@keyframes imp-slide{0%{transform:translateX(-100%)}100%{transform:translateX(400%)}}`}</style>
              <div style={{ width: '100%', height: 4, background: C.border, borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  width: '25%', height: '100%', background: C.primary, borderRadius: 2,
                  animation: 'imp-slide 1.2s ease-in-out infinite',
                }} />
              </div>
            </div>
          )}

          {!importLoading && importResult && (() => {
            const LABELS = {
              expenses: 'Expenses', incomes: 'Income entries', expense_types: 'Categories',
              budgets: 'Budgets', monthly_budgets: 'Monthly budgets', import_rules: 'Import rules',
              savings_goals: 'Savings goals', savings_contributions: 'Contributions', macrocategories: 'Groups',
            }
            const rows = Object.entries(importResult).filter(([, n]) => n > 0)
            return rows.length ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {rows.map(([k, n]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                    <span style={{ color: C.muted }}>{LABELS[k] ?? k}</span>
                    <span style={{ fontWeight: 700, color: C.text }}>{n}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p style={{ margin: 0, fontSize: 13, color: C.muted }}>Nothing new — all records already exist.</p>
            )
          })()}

          {!importLoading && importError && (
            <p style={{ margin: 0, fontSize: 13, color: '#e57373' }}>{importError}</p>
          )}

          {!importLoading && (
            <DialogFooter>
              <button
                type="button"
                onClick={() => setImportOpen(false)}
                style={{
                  padding: '8px 20px', borderRadius: 8, border: 'none',
                  background: C.primary, color: '#fff', fontSize: 13, fontWeight: 700,
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
              >
                Done
              </button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>

      <ClearAllDialog/>

      {/* Billing Cycle */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
          <Calendar size={16} color={C.muted} />
          <span style={{ fontWeight: 600, fontSize: 14, color: C.text }}>Billing Cycle</span>
        </div>

        <p style={{ margin: '0 0 12px', fontSize: 13, color: C.muted, lineHeight: 1.5 }}>
          Set which day of the month your budgets and spending views reset — useful if you'd
          rather track against a credit card statement date than the calendar month. If the
          day doesn't exist in a given month (e.g. the 30th in February), it falls back to
          that month's last day.
        </p>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, color: C.text }}>Month starts on day</span>
          <input
            type="number"
            min={1}
            max={31}
            value={cycleDayInput}
            disabled={aiSettingsLoading || cycleSaving}
            onChange={e => { cycleDayTouched.current = true; setCycleDayInput(e.target.value) }}
            style={{ ...inputStyle, width: 70 }}
          />
          <button
            type="button"
            onClick={handleCycleDaySave}
            disabled={aiSettingsLoading || cycleSaving}
            style={{
              padding: '7px 16px', borderRadius: 8, border: 'none',
              background: C.primary, color: '#fff', fontSize: 13, fontWeight: 600,
              cursor: cycleSaving ? 'not-allowed' : 'pointer',
              opacity: cycleSaving ? 0.6 : 1, fontFamily: 'inherit',
            }}
          >
            {cycleSaving ? 'Saving…' : 'Save'}
          </button>
        </div>

        {cycleError && <p style={{ margin: '8px 0 0', fontSize: 12, color: '#e57373' }}>{cycleError}</p>}
        {cycleSaved && <p style={{ margin: '8px 0 0', fontSize: 12, color: C.primary }}>Saved.</p>}
      </div>

      {/* AI & Privacy */}
      <div style={section} data-tour="ai-privacy">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Bot size={16} color={C.muted} />
            <span style={{ fontWeight: 600, fontSize: 14, color: C.text }}>AI Features</span>
          </div>
          <button
            type="button"
            disabled={aiSettingsLoading}
            onClick={() => handleAiToggle(!aiEnabled)}
            style={{
              width: 44, height: 24, borderRadius: 12, border: 'none', padding: 0,
              background: aiEnabled ? C.primary : C.border,
              cursor: aiSettingsLoading ? 'not-allowed' : 'pointer',
              position: 'relative', transition: 'background 0.2s', flexShrink: 0,
              opacity: aiSettingsLoading ? 0.5 : 1,
            }}
            aria-label={aiEnabled ? 'Disable AI' : 'Enable AI'}
          >
            <span style={{
              position: 'absolute', top: 3, left: aiEnabled ? 23 : 3,
              width: 18, height: 18, borderRadius: '50%', background: '#fff',
              transition: 'left 0.2s', boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
            }} />
          </button>
        </div>

        <p style={{ margin: '0 0 8px', fontSize: 13, color: C.muted, lineHeight: 1.5 }}>
          Tally uses AI to power the chatbot and semantic search. To do this, it stores
          vector embeddings of your transactions — compact numerical representations, not raw text.
          Disabling AI removes these embeddings immediately.
        </p>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
          <button
            type="button"
            onClick={handleDeleteAiData}
            disabled={aiDataDeleting}
            style={{
              padding: '6px 14px', borderRadius: 8, border: `1px solid ${C.border}`,
              background: 'transparent', color: C.muted, fontSize: 13, fontWeight: 600,
              cursor: aiDataDeleting ? 'not-allowed' : 'pointer',
              opacity: aiDataDeleting ? 0.6 : 1, fontFamily: 'inherit',
            }}
          >
            {aiDataDeleting ? 'Deleting…' : 'Delete my AI data'}
          </button>
          {aiDataMsg && (
            <p style={{ margin: 0, fontSize: 12, color: C.muted }}>{aiDataMsg}</p>
          )}
        </div>
      </div>

      {/* Sign out */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <p style={{ margin: 0, fontWeight: 600, fontSize: 14, color: C.text }}>Sign out</p>
            <p style={{ margin: '2px 0 0', fontSize: 12, color: C.muted }}>Sign out of this device</p>
          </div>
          <button
            type="button"
            onClick={() => setConfirmLogout(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: 7,
              padding: '7px 14px', borderRadius: 8,
              border: '1px solid rgba(229,115,115,0.4)',
              background: 'rgba(229,115,115,0.08)',
              color: '#e57373', fontSize: 13, fontWeight: 600,
              cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            <LogOut size={14} />
            Sign out
          </button>
        </div>
      </div>

      <Dialog open={confirmLogout} onOpenChange={setConfirmLogout}>
        <DialogContent style={{ maxWidth: 360 }}>
          <DialogHeader>
            <DialogTitle>Sign out?</DialogTitle>
          </DialogHeader>
          <p style={{ margin: 0, fontSize: 14, color: C.muted }}>
            You'll need to sign in again to access your account.
          </p>
          <DialogFooter>
            <button
              type="button"
              onClick={() => setConfirmLogout(false)}
              style={{
                padding: '8px 16px', borderRadius: 8, border: `1px solid ${C.border}`,
                background: 'transparent', color: C.text, fontSize: 13, fontWeight: 600,
                cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onLogout}
              style={{
                padding: '8px 16px', borderRadius: 8, border: 'none',
                background: '#e57373', color: '#fff', fontSize: 13, fontWeight: 700,
                cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              Sign out
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
