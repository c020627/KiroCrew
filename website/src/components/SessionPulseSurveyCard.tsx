import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle, ChevronRight, MessageSquare, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { ratingOptions } from './sessionPulseWireValues'
import { secureRandomId } from '../utils/secureId'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

// Kiro Crew is self-hosted, open-source software — every install runs on its
// own arbitrary origin, which Aperture's browser-CORS allowlist model (a
// finite, known set of domains) cannot accommodate. These calls go to the
// Kiro Crew backend's own same-origin routes instead
// (src/kiro_crew/dashboard/handlers/feedback.py), which forward to Aperture
// server-to-server, where CORS does not apply.
const FEEDBACK_SUBMIT_URL = '/api/feedback/submit'
const FEEDBACK_ELIGIBLE_URL = '/api/feedback/eligible'
const FEEDBACK_IDENTITY_URL = '/api/feedback/identity'

// Matches the backend's auto-minted slot-key shape (see `_mint_slot_key` in
// dashboard/state.py: `f"{prefix}-{counter}-{ts}"` with `prefix` hardcoded to
// "chat"). That shape is produced by exactly ONE code path — creating a slot
// with no explicit name — which is what happens when someone clicks "New" on
// the dashboard's own Chat page. Every other surface passes its own explicit
// name instead: a Slack-linked slot is `slack_<ts>`, a cron-triggered one is
// `cron-<job_id>`, a spec-builder task is `spec-builder-task-<slug>`, an
// exec-ticket automation is `exec-ticket-<n>-...`, and so on for every other
// app. This is deliberately a POSITIVE allowlist rather than a denylist of
// surfaces to exclude: a future app that mints its own slot names is
// automatically excluded without this file needing an update, whereas a
// denylist would need one every time a new surface is added — which is
// exactly how the survey ended up firing on Slack threads and spec-builder
// tasks in the first place (see the Aug 2026 revert/incident: eligibility had
// no surface gate at all, so it fired for anyone viewing ANY session for 3+
// turns, however that session started).
const ORDINARY_CHAT_SESSION_RE = /^chat-\d+-\d+$/

function isOrdinaryChatSession(sessionId: string): boolean {
  return ORDINARY_CHAT_SESSION_RE.test(sessionId)
}

const RATING_LABEL_KEYS: Record<string, string> = {
  'Very Poor': 'components.sessionPulseSurveyCard.rating_very_poor',
  Poor: 'components.sessionPulseSurveyCard.rating_poor',
  Fair: 'components.sessionPulseSurveyCard.rating_fair',
  Good: 'components.sessionPulseSurveyCard.rating_good',
  Excellent: 'components.sessionPulseSurveyCard.rating_excellent',
}

// How long the "Thanks for your feedback" confirmation stays visible after a
// successful submit, before the card fades away on its own.
const CONFIRMATION_DISPLAY_MS = 3000

// Aperture tracks its own per-user eligibility server-side, but its form-level
// cooldown isn't something we control from the client, and Mia wants a firm
// 30-day cooldown regardless of Aperture's configured default. We ask
// Aperture first (so it still gets accurate per-user, cross-device dedup
// data), then additionally require our own 30-day gate before showing —
// whichever check is stricter wins.
const COOLDOWN_KEY = 'kirocrew_survey_last_shown'
const COOLDOWN_DAYS = 30

function localCooldownElapsed(): boolean {
  const lastShown = safeGetItem(COOLDOWN_KEY)
  if (!lastShown) return true
  const elapsed = Date.now() - new Date(lastShown).getTime()
  return elapsed > COOLDOWN_DAYS * 24 * 60 * 60 * 1000
}

function markLocalCooldown(): boolean {
  return safeSetItem(COOLDOWN_KEY, new Date().toISOString())
}

// The dashboard's own `userId` (the auth token subject) is NOT usable as-is
// for this survey: on any install with no `owner_id` configured — the
// default single-user setup — every session authenticates as the literal
// subject "local-app", so every user on that install would share one
// Aperture identity. That defeats both layers of eligibility this card
// relies on: Aperture's own per-user cooldown/dedup, and the distinctness
// "how many different people responded" is supposed to have.
//
// The backend's /api/feedback/identity resolves this two ways: when a real
// owner IS configured, it returns a hash of that real identity — the same
// person then gets the same value across every browser/machine hitting this
// install, fixing the cross-instance duplicate-survey report (the same
// person on a desktop app + a browser tab used to look like two different
// respondents). When no real owner is configured, it returns null, and this
// per-browser random id (generated once, persisted in this browser's
// storage) is the fallback — a real per-browser identity regardless of
// dashboard config, though not a real per-*person* one (two browsers = two
// ids for the same person, one shared browser = one id for multiple
// people). See resolvedIdentity in the component below for how the two are
// combined.
const SURVEY_IDENTITY_KEY = 'kirocrew_survey_identity'

function getSurveyIdentity(): { id: string; persisted: boolean } {
  const existing = safeGetItem(SURVEY_IDENTITY_KEY)
  if (existing) return { id: existing, persisted: true }
  const generated = secureRandomId()
  // `persisted` is a fail-closed signal: if storage is denied/full the write
  // returns false, so the next mount would generate a DIFFERENT id and this
  // browser's repeat submissions would register as distinct respondents. The
  // show effect declines to show the card in that case (only when this random
  // fallback is the identity in use), rather than collecting un-dedupable data.
  const persisted = safeSetItem(SURVEY_IDENTITY_KEY, generated)
  return { id: generated, persisted }
}

/** Ask (via our own backend) whether Aperture considers this user due for the
 * survey. A failure of any kind (network, non-2xx) fails closed — don't show
 * the survey rather than guessing eligibility. */
async function checkSurveyEligible(userId: string): Promise<boolean> {
  try {
    const res = await fetch(
      `${FEEDBACK_ELIGIBLE_URL}?userId=${encodeURIComponent(userId)}`
    )
    if (!res.ok) return false
    const body = await res.json().catch(() => null)
    return body?.eligible === true
  } catch {
    return false
  }
}

/** Ask our backend for the real-owner identity hash (see /api/feedback/identity
 * and its comment on the backend for the full rationale). Returns null on
 * any failure or when this install has no real configured owner — the
 * caller falls back to the per-browser random id either way. */
async function fetchSurveyIdentityHash(): Promise<string | null> {
  try {
    const res = await fetch(FEEDBACK_IDENTITY_URL, { credentials: 'include' })
    if (!res.ok) return null
    const body = await res.json().catch(() => null)
    return typeof body?.identityHash === 'string' ? body.identityHash : null
  } catch {
    return null
  }
}

interface SessionPulseSurveyCardProps {
  sessionId: string
  kiroCrewVersion: string
  turnCount: number
  /** Notifies the parent whenever the card's rendered height may have
   * changed — mount, unmount, expand/collapse, or the post-submit collapse
   * to the thank-you row — so it can re-anchor scroll position the same way
   * it does for other in-flow bands (see the activeTip re-anchor effect in
   * ChatPage). This component sits outside the virtualizer's measured rows,
   * so any height change here moves the scroll viewport's real content
   * height without the virtualizer knowing. Fires on every visible /
   * expanded / submitted transition, not just show/hide, because the
   * collapsed-by-default disclosure pattern below changes height on its own
   * while still mounted. */
  onLayoutChange?: () => void
}

export default function SessionPulseSurveyCard({
  sessionId,
  kiroCrewVersion,
  turnCount,
  onLayoutChange,
}: SessionPulseSurveyCardProps) {
  const { t } = useTranslation()
  // Generated once, on first mount, and reused for the life of the browser
  // profile — see getSurveyIdentity()'s own comment for why this must be
  // independent of the dashboard's auth-derived userId.
  const [surveyIdentityInfo] = useState(getSurveyIdentity)
  const surveyIdentity = surveyIdentityInfo.id
  // Real-owner identity hash, when this install has one configured (see
  // fetchSurveyIdentityHash / /api/feedback/identity). Fires on mount,
  // independent of eligibility — by the time liveTurnCount can reach 3 this
  // has long since settled, so resolvedIdentity below is stable before it's
  // ever read for an eligibility check or a submission.
  const identityHashQuery = useQuery({
    queryKey: ['sessionPulseIdentityHash'],
    queryFn: fetchSurveyIdentityHash,
    staleTime: Infinity,
  })
  // Prefer the real, install-scoped identity hash — same person, same value,
  // across every browser/machine hitting this install — over the per-browser
  // random fallback. Null only when no real owner is configured for this
  // install, in which case the random id is the best available signal.
  const resolvedIdentity = identityHashQuery.data ?? surveyIdentity
  const [visible, setVisible] = useState(false)
  // One-time latch: set the first time the card is shown, and never cleared
  // for the life of this mount. The eligibility query result (isEligible) is
  // cached `true` with staleTime Infinity, so without this latch the show
  // effect below re-fires every time `visible` flips back to false — which is
  // exactly what dismiss() and the post-submit auto-close do — and slams the
  // card straight back open. That made the dismiss (X) and the "Thanks"
  // auto-close appear dead, and re-showed the survey to the same identity.
  const [handled, setHandled] = useState(false)
  // Whether the card is showing its full form or just the slim trigger row.
  // Starts collapsed: the card should announce itself as one low-key line,
  // not open the entire rating/feedback/email form uninvited the instant it
  // becomes eligible (the "less invasive" redesign — see the collapsed
  // trigger row / expanded form / collapsed thank-you row below).
  const [expanded, setExpanded] = useState(false)
  const [selectedRating, setSelectedRating] = useState<string | null>(null)
  const [feedback, setFeedback] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [submitError, setSubmitError] = useState(false)
  // Baseline captured once per session mount (this component remounts on
  // session switch via `key={activeSlot}`). turnCount includes every
  // assistant turn already loaded from history, so without a baseline,
  // reopening any session with >=3 prior turns would pop the survey on every
  // visit — merely re-reading old messages, not a fresh interaction. Only
  // turns completed live past this baseline count toward eligibility.
  const [baselineTurnCount] = useState(turnCount)
  const liveTurnCount = turnCount - baselineTurnCount
  // Without this guard, crossing the turn-3 threshold re-fires the query on
  // every subsequent turn (4th, 5th, ...) as long as the card stays hidden —
  // each one re-hitting /api/feedback/eligible for an answer that cannot
  // change mid-session. `enabled` covers that (React Query only fetches once
  // while the gate stays true and the result is cached), so no separate
  // "already checked" flag is needed the way the old effect-based version
  // required one.
  const eligibilityGate =
    isOrdinaryChatSession(sessionId) &&
    liveTurnCount >= 3 &&
    !visible &&
    localCooldownElapsed() &&
    identityHashQuery.isFetched
  const { data: isEligible } = useQuery({
    // sessionId is part of the key, not just resolvedIdentity: without it, an
    // eligible result cached for one session would be reused verbatim after
    // switching to a different session -- reopening the card and attributing
    // the eventual submission to whichever session happens to be active,
    // rather than the one that was actually checked and found eligible.
    queryKey: ['sessionPulseSurveyEligible', resolvedIdentity, sessionId],
    queryFn: () => checkSurveyEligible(resolvedIdentity),
    enabled: eligibilityGate,
    staleTime: Infinity,
  })

  // Reacts to the query's own result rather than fetching itself, so there is
  // no cleanup-sets-cancelled race: React Query owns the fetch's lifecycle
  // (including in-flight cancellation on unmount/key change), and this effect
  // only ever reads settled data.
  useEffect(() => {
    // Gate the SHOW on the full eligibilityGate, not just the cached query
    // result. `isEligible` is cached with staleTime Infinity keyed by
    // identity+session, so after a submit + session switch + return within the
    // cache lifetime the disabled query still yields a stale `true`; on remount
    // `handled` has reset, so without re-checking the gate here the card would
    // reopen and accept a second response inside the 30-day window. Requiring
    // `eligibilityGate` re-applies localCooldownElapsed() (and the turn/surface/
    // identity checks) at show time, so a not-yet-elapsed cooldown keeps it shut.
    if (eligibilityGate && isEligible && !visible && !handled) {
      // Fail closed on unavailable storage. If we cannot persist a stable
      // identity (only a concern for the per-browser random fallback — a
      // configured-owner hash is stable server-side regardless of storage)
      // OR cannot persist the 30-day cooldown timestamp, do NOT show. A
      // storage-denied browser would otherwise reopen the card on every
      // remount and its repeat submissions would register as distinct
      // respondents — the duplicate-submission failure this survey exists to
      // prevent. `setHandled(true)` still fires so the gate is not
      // re-evaluated every render; the card simply stays closed here.
      const identityPersisted =
        identityHashQuery.data != null || surveyIdentityInfo.persisted
      if (!identityPersisted || !markLocalCooldown()) {
        setHandled(true)
        return
      }
      setVisible(true)
      setHandled(true)
    }
  }, [eligibilityGate, isEligible, visible, handled, identityHashQuery.data, surveyIdentityInfo.persisted])

  useEffect(() => {
    onLayoutChange?.()
  }, [visible, expanded, submitted, onLayoutChange])

  const dismiss = () => setVisible(false)

  const submit = async () => {
    if (!selectedRating) return
    setSubmitting(true)
    setSubmitError(false)

    try {
      const res = await fetch(FEEDBACK_SUBMIT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rating: selectedRating,
          feedback: feedback.trim(),
          email: email.trim(),
          sessionId,
          kiroCrewVersion,
          userId: resolvedIdentity,
        }),
      })
      if (!res.ok) {
        // Aperture rejected the submission or is unavailable — keep the form
        // visible so the user can retry, rather than showing a false
        // confirmation and burning the 30-day cooldown on lost feedback.
        setSubmitting(false)
        setSubmitError(true)
        return
      }
    } catch {
      setSubmitting(false)
      setSubmitError(true)
      return
    }

    setSubmitting(false)
    setSubmitted(true)
    setTimeout(() => setVisible(false), CONFIRMATION_DISPLAY_MS)
  }

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.95 }}
          transition={{ type: 'spring', bounce: 0, duration: 0.28 }}
          className="border border-accent/30 rounded-xl bg-card shadow-sm mt-3 overflow-hidden"
        >
          {submitted ? (
            /* Collapsed thank-you row: the confirmation gets the same slim
             * footprint as the trigger row rather than lingering as a full
             * open card for CONFIRMATION_DISPLAY_MS. */
            <div className="flex items-center gap-2.5 px-4 py-2.5">
              <CheckCircle size={15} className="text-ok shrink-0" />
              <p className="flex-1 min-w-0 text-[13px] text-text">
                {t('components.sessionPulseSurveyCard.thanks')}
              </p>
              <button
                onClick={dismiss}
                aria-label={t('components.sessionPulseSurveyCard.dismiss')}
                className="bg-transparent border-none text-muted hover:text-text cursor-pointer shrink-0"
              >
                <X size={14} />
              </button>
            </div>
          ) : (
            <>
              {/* Header row — also the expand/collapse toggle. This is the
               * whole card at rest: one line, same height as a line of
               * chat, until someone opts in by clicking it. */}
              <div className="flex items-center gap-2.5 px-4 py-2.5">
                <button
                  onClick={() => setExpanded((e) => !e)}
                  aria-expanded={expanded}
                  className="flex items-center gap-2.5 flex-1 min-w-0 text-left bg-transparent border-none cursor-pointer p-0"
                >
                  <MessageSquare size={15} className="text-accent shrink-0" />
                  <span className="flex-1 min-w-0 truncate text-[13px] font-medium text-text">
                    {t('components.sessionPulseSurveyCard.rating_question')}
                  </span>
                  <ChevronRight
                    size={14}
                    className={`text-muted shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
                  />
                </button>
                <button
                  onClick={dismiss}
                  aria-label={t('components.sessionPulseSurveyCard.dismiss')}
                  className="bg-transparent border-none text-muted hover:text-text cursor-pointer shrink-0"
                >
                  <X size={14} />
                </button>
              </div>

              {/* Expanded form. Collapsed by default (see `expanded` state
               * above) — clicking the header row again collapses it back
               * without submitting, so there's no separate Cancel button
               * duplicating that same action. */}
              <AnimatePresence initial={false}>
                {expanded && (
                  <motion.div
                    key="survey-form"
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    className="overflow-hidden"
                  >
                    <div className="px-4 pb-4">
                      {/* Rating */}
                      <div
                        className="mb-4 flex gap-2 flex-wrap"
                        role="radiogroup"
                        aria-label={t('components.sessionPulseSurveyCard.rating_question')}
                        onKeyDown={(e) => {
                          const arrows = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp']
                          if (!arrows.includes(e.key)) return
                          e.preventDefault()
                          const forward = e.key === 'ArrowRight' || e.key === 'ArrowDown'
                          const idx = selectedRating ? ratingOptions.indexOf(selectedRating) : -1
                          const next =
                            idx < 0
                              ? forward
                                ? 0
                                : ratingOptions.length - 1
                              : (idx + (forward ? 1 : -1) + ratingOptions.length) %
                                ratingOptions.length
                          setSelectedRating(ratingOptions[next])
                          const radios =
                            e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]')
                          radios[next]?.focus()
                        }}
                      >
                        {ratingOptions.map((option, i) => {
                          const checked = selectedRating === option
                          // Roving tabindex: Tab lands on the selected radio
                          // (or the first, when none is chosen yet); arrows move
                          // among the rest. This is the ARIA radiogroup contract
                          // the old `aria-pressed` toggle buttons only pretended
                          // to honour.
                          const roving = checked || (!selectedRating && i === 0) ? 0 : -1
                          return (
                            <button
                              key={option}
                              type="button"
                              role="radio"
                              aria-checked={checked}
                              tabIndex={roving}
                              onClick={() => setSelectedRating(option)}
                              className={`text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-bg ${
                                checked
                                  ? 'border-accent text-text bg-accent-subtle/60'
                                  : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg font-normal'
                              }`}
                            >
                              {t(RATING_LABEL_KEYS[option])}
                            </button>
                          )
                        })}
                      </div>

                      {/* Data-egress disclosure, promoted to normal weight
                       * directly under the rating (not muted under the email
                       * field): on a self-hosted product a user must see that
                       * their rating + feedback leave for the Kiro Crew team
                       * BEFORE they submit, not read it as an email-only note. */}
                      <p className="mb-4 text-[12px] text-text">
                        {t('components.sessionPulseSurveyCard.email_disclosure')}
                      </p>

                      {/* Open feedback */}
                      <div className="mb-4">
                        <p className="text-[13px] text-text mb-2">
                          {t('components.sessionPulseSurveyCard.feedback_question')}
                        </p>
                        <textarea
                          value={feedback}
                          onChange={(e) => setFeedback(e.target.value)}
                          placeholder={t('components.sessionPulseSurveyCard.optional')}
                          className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none resize-vertical min-h-[60px]"
                        />
                      </div>

                      {/* Email */}
                      <div className="mb-4">
                        <p className="text-[13px] text-text mb-2">
                          {t('components.sessionPulseSurveyCard.email_prompt')}
                        </p>
                        <input
                          type="email"
                          value={email}
                          onChange={(e) => setEmail(e.target.value)}
                          placeholder={t('components.sessionPulseSurveyCard.email_placeholder')}
                          className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none"
                        />
                      </div>

                      {/* Submit */}
                      <div className="flex items-center justify-end gap-3">
                        {submitError && (
                          <p className="text-[12px] text-danger">
                            {t('components.sessionPulseSurveyCard.submit_error')}
                          </p>
                        )}
                        {!submitError && !selectedRating && (
                          <p className="text-[12px] text-muted">
                            {t('components.sessionPulseSurveyCard.select_rating_hint')}
                          </p>
                        )}
                        <button
                          onClick={submit}
                          disabled={!selectedRating || submitting}
                          className="px-3.5 py-1.5 rounded-md text-[13px] font-medium bg-accent text-accent-fg hover:bg-accent-hover border-none disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer transition-all"
                        >
                          {submitting
                            ? t('components.sessionPulseSurveyCard.submitting')
                            : t('components.sessionPulseSurveyCard.submit')}
                        </button>
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
