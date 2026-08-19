import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AccountPage from './AccountPage.jsx'

// This test is about AccountPage's own two-factor-auth section wiring
// (listFactors -> enabled/disabled state, unenroll flow) — not the enroll
// dialog's internals, which MfaSetupDialog.test.jsx already covers.
vi.mock('../dialogs/MfaSetupDialog.jsx', () => ({ default: () => null }))
vi.mock('../dialogs/ClearAllDialog.jsx', () => ({ default: () => null }))

const mockListFactors = vi.fn()
const mockUnenroll = vi.fn()
const mockChallengeAndVerify = vi.fn()
vi.mock('../../supabase.js', () => ({
  supabase: {
    auth: {
      mfa: {
        listFactors: (...args) => mockListFactors(...args),
        unenroll: (...args) => mockUnenroll(...args),
        challengeAndVerify: (...args) => mockChallengeAndVerify(...args),
      },
    },
  },
}))

const mockGet = vi.fn()
vi.mock('../../api.js', () => ({
  default: { get: (...args) => mockGet(...args) },
  getErrorMessage: () => 'error',
}))

beforeEach(() => {
  mockGet.mockReset()
  mockGet.mockResolvedValue({ data: { ai_enabled: true, cycle_start_day: 1 } })
  mockUnenroll.mockReset()
  mockUnenroll.mockResolvedValue({ data: {}, error: null })
  mockChallengeAndVerify.mockReset()
  mockChallengeAndVerify.mockResolvedValue({ data: {}, error: null })
})

function renderAccountPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AccountPage user={{ email: 'a@b.com' }} onLogout={() => {}} />
    </QueryClientProvider>
  )
}

describe('AccountPage two-factor authentication section', () => {
  it('shows an Enable button when the user has no enrolled factor', async () => {
    mockListFactors.mockResolvedValue({ data: { totp: [] } })
    renderAccountPage()

    expect(await screen.findByRole('button', { name: /^enable$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^disable$/i })).not.toBeInTheDocument()
  })

  it('shows a Disable button and enabled status when a factor is already enrolled', async () => {
    mockListFactors.mockResolvedValue({ data: { totp: [{ id: 'factor-1', factor_type: 'totp', status: 'verified' }] } })
    renderAccountPage()

    expect(await screen.findByRole('button', { name: /^disable$/i })).toBeInTheDocument()
    expect(screen.getByText(/enabled — your account requires/i)).toBeInTheDocument()
  })

  it('re-verifies with a fresh code before unenrolling the factor', async () => {
    mockListFactors.mockResolvedValue({ data: { totp: [{ id: 'factor-1', factor_type: 'totp', status: 'verified' }] } })
    renderAccountPage()

    fireEvent.click(await screen.findByRole('button', { name: /^disable$/i }))

    // Unenrolling a verified factor requires an aal2 session, which this app
    // never establishes during normal login (no step-up challenge exists) —
    // so the confirm dialog collects a fresh code and re-verifies first.
    const codeInput = await screen.findByPlaceholderText(/6-digit code/i)
    fireEvent.change(codeInput, { target: { value: '123456' } })

    // Opening the confirm dialog leaves the section's own "Disable" toggle button
    // in the DOM (Radix portals the dialog rather than replacing page content),
    // so there are now two "Disable" buttons — the confirm dialog's is the second.
    const disableButtons = await screen.findAllByRole('button', { name: /^disable$/i })
    fireEvent.click(disableButtons[disableButtons.length - 1])

    await waitFor(() => expect(mockChallengeAndVerify).toHaveBeenCalledWith({ factorId: 'factor-1', code: '123456' }))
    await waitFor(() => expect(mockUnenroll).toHaveBeenCalledWith({ factorId: 'factor-1' }))
  })

  it('shows the error and does not unenroll when re-verification fails', async () => {
    mockListFactors.mockResolvedValue({ data: { totp: [{ id: 'factor-1', factor_type: 'totp', status: 'verified' }] } })
    mockChallengeAndVerify.mockResolvedValue({ data: null, error: { message: 'Invalid TOTP code entered' } })
    renderAccountPage()

    fireEvent.click(await screen.findByRole('button', { name: /^disable$/i }))
    fireEvent.change(await screen.findByPlaceholderText(/6-digit code/i), { target: { value: '000000' } })
    const disableButtons = await screen.findAllByRole('button', { name: /^disable$/i })
    fireEvent.click(disableButtons[disableButtons.length - 1])

    expect(await screen.findByText(/invalid totp code entered/i)).toBeInTheDocument()
    expect(mockUnenroll).not.toHaveBeenCalled()
  })
})
