# The Anki collection — what the data actually showed

Findings from analysing the collection on 2026-08-30/31, before and while the Anki
subsystem was built. Recorded because the decisions in `anki_review.py`,
`stroke_validation.py` and the deck presets rest on these numbers, and re-deriving
them means re-running the analysis against a collection that has since moved on.

Baseline at the time: 4,784 notes, 4,937 cards, 142,899 review log entries, of which
4,052 cards are Japanese.

## The backlog was not what it looked like

694 cards due, and every one of them a *mature* card:

| Bucket | Cards | Mean interval |
|---|---|---|
| Not yet due | 2,738 | 972 d |
| 1–7 days late | 87 | 190 d |
| 8–30 days late | 194 | 227 d |
| 31–90 days late | 413 | 176 d |

Zero in learning, zero suspended.

## Six sessions of button-mashing

| Date | Reviews | Median answer time | Under 1.5 s | "Again" |
|---|---|---|---|---|
| 2025-12-30 | 372 | 1.0 s | 97.3% | 0% |
| 2026-02-06 | 284 | 1.0 s | 99.3% | 0% |
| 2026-02-25 | 460 | 1.0 s | 96.7% | 0% |
| 2026-03-26 | 304 | 1.0 s | 59.2% | 0% |
| 2026-04-24 | 585 | 1.0 s | 97.8% | 0% |
| 2026-06-26 | 868 | 1.0 s | 98.7% | 0.2% |

2,873 reviews over 1,706 distinct cards; **1,650 cards have one of these as their most
recent review**, so their current interval reflects a keystroke rather than a memory.
Every other day in the history sits at 4–7 s median with 80–96% retention, so the two
populations separate cleanly.

Interval inflation caused, by prior interval: ≤30 d, 536 reviews, 16 → 28 d · 31–90 d,
1,492 reviews, 57 → 86 d · 91–365 d, 714 reviews, 167 → 292 d · >365 d, 131 reviews,
771 → **1,470 d**.

**Consequences.** Retention excluding those days is 89.0% all-time, 85.4% over two
years, 93.7% over the last year — a healthy band, so the scheduler was working when it
was being used honestly. Genuine answer speed is 7–12 s per card including think time.
The damage was left to heal on its own rather than repaired: each affected card costs
one extra "Again" whenever it resurfaces, spread over months, which is cheaper than
converting a hidden problem into a visible wall.

## Ease is the real injury

| Ease band | Japanese review cards | |
|---|---|---|
| Floored at 130% | 1,123 | **32.7%** |
| 130–180% | 381 | 11.1% |
| 180–230% | 592 | 17.2% |
| 230–260% | 863 | 25.1% |
| 260%+ | 473 | 13.8% |

Mean ease 195% against a 250% default. Buttons pressed over the collection's life:
Again 9,136 · **Hard 24,571** · Good 48,798 · Easy 3,057 — Hard eight times more often
than Easy, each −15 pp against +15 pp. And **326 of 338 leech cards sit at or below
150% ease**: they are not merely hard, they are scheduled to grow at the minimum rate
indefinitely.

This is why grading is pass/fail everywhere (`anki_review.grade`). Under SM-2, Hard
costs 15 pp of ease *and* still extends the interval.

## Leeches cluster by shared kanji

338 Japanese leeches. By shape: 52% two-kanji compounds, 24% one kanji + kana, 19%
single kanji. They are not independently difficult — they share characters:

```
発  開発 発生 発言 発見 発表      大  大型 大使館 拡大 大幅 大抵 大勢 大歓迎
対  対立 対策 対象 絶対に        成  完成 成功 成績 構成
加  参加 増加 加える 加工        理  理由 修理 管理 処理
```

That is textbook retrieval interference (SuperMemo rule 11), and it argues for
contrasting a cluster explicitly rather than reviewing its members weeks apart. See the
interference-session item in the README.

## Redundancy and thin sentence coverage

**485 words appear in more than one deck**, 369 of them duplicated between Core 2000
and the JLPT N4 deck — most of that deck's untouched new cards are words already known
from Core 2000, as a second identical recognition card.

Sentences available per word, across the Anki corpora:

| Sentences | Words | |
|---|---|---|
| exactly 1 | 2,080 | **48.5%** |
| 2 | 738 | 17.2% |
| 3+ | 1,471 | 34.3% |

Only 52% of vocabulary has more than one example, and it is thinnest where it matters:
発生, 開発 and 対策 have one sentence each. Rendering a card against a *random* real
sentence — so that knowing the word is distinguishable from having memorised one
sentence — needs the mining work before it can apply everywhere.

## Audio coverage

| Notetype | Notes | Audio |
|---|---|---|
| Core 2000 | 2,008 | 2,005 word + 2,007 sentence |
| movies2anki (Ghibli) | 860 | 860 audio + 860 video |
| DaKanji | 308 | **0** |
| JLPT N4 | 640 | **0** |
| 部首 | 197 | **0** |

Also: 53 Howl's and 64 Spirited Away notes contain no dialogue at all — their
`Expression` is entirely sound-effect or speaker captions like `（ノックの音）` — so
they can teach no vocabulary and mining should skip them.

## Deck settings as found

Every Japanese preset used `REVIEW_CARD_ORDER_DAY` ("due date, then random") with
reviews/day at 9,999. With a backlog, due-date ordering means a card that lapsed
yesterday sorts *behind* hundreds of cards that went overdue months ago, so it is not
seen again for weeks — the vicious cycle that had prompted manually splitting Core 2000
into `relearn` / `should know` / `Hard` subdecks. Replaced with a single `Japanese`
parent, one preset, `RELATIVE_OVERDUENESS` ordering, interday learning shown before
reviews, and 50 reviews/day with 0 new while the backlog drains.

## Re-deriving any of this

The queries are ordinary reads against a collection opened with the `anki` library, or
against `anki_notes` / `anki_note_words` in `data/wakatta.db`. Nothing here is stored;
it was all measured. Numbers will drift as the collection is used — treat them as the
state that motivated the design, not as current facts.

---

# 2026-09-04 — first days of real reviewing, and a pending decision

**Sample size warning.** This covers five days (2026-08-31 to 09-04) and 243 reviews,
the first genuine study since 2026-04-28. Everything below is suggestive, not settled.
It is recorded so the decision at the end can be made against fresh numbers later
rather than re-derived, and so the trend can be compared against something.

## What the five days looked like

| Date | Reviews | Again | Good | Median answer |
|---|---|---|---|---|
| 08-31 | 1 | 1 | 0 | 18.1 s |
| 09-01 | 48 | 21 | 25 | 17.4 s |
| 09-02 | 27 | 10 | 17 | 21.0 s |
| 09-03 | 150 | 71 | 79 | 16.5 s |
| 09-04 | 17 | 7 | 10 | 10.9 s |

**Retention on mature (review-type) cards: 37%**, against an honest historical average
of 89%. Not attributable to new material — new cards are excluded from that figure.
Two plausible causes, both expected: four months away from the deck, and the 1,650
cards left on intervals fabricated by the six button-mashing sessions, which are now
coming due at intervals never actually earned.

By deck, last five days:

| Deck | Reviews | Again | Rate |
|---|---|---|---|
| Japanese::Core 2000 | 179 | 78 | 44% |
| Japanese::DaKanji | 31 | 19 | 61% |
| Japanese::Wakatta::Audio Writing | 22 | 13 | 59% |
| Japanese::Wakatta::Kanji | 10 | 0 | 0% |

The Audio Writing rate is inflated: the stroke validator was failing correct
handwriting during this period (thresholds fitted to synthetic jitter — see the
Calibration section of the README).

**The backlog grew**: 694 due on 08-30, **707 on 09-04**, despite 243 reviews. At a 44%
failure rate the returns outpace the drain.

## Most failures are near-misses

Of every "Again" on a review card in the window:

| Outcome | Count | |
|---|---|---|
| Passed the very next relearning step | 47 | **57%** |
| Failed again on the next step | 17 | 21% |
| No follow-up yet (session ended) | 18 | 22% |

Agains also take *longer* to answer than Goods — 23.3 s median against 16.6 s — the
signature of effortful near-miss rather than blank. Kornell, Hays & Bjork (2009) found
unsuccessful retrieval followed by feedback beats studying the answer outright, so
these are productive reviews, not waste. The problem is what happens to the card
afterwards, not that it failed.

Classified by behaviour (see `anki_triage.py`): **38 clean · 58 jog · 24 repeat**, out
of 120 cards touched. The repeat bucket is 20% and contains exactly who you would
expect — 原因 (19 lapses), 投資 (22), 給料, 完成, 発表.

## Why the intervals do not recover on their own

The expectation that spaced repetition self-corrects — a jog collapses to a day, then
climbs exponentially back out — is correct in principle and does not hold here, because
the base of the exponent is 1.30 rather than 2.5.

| Ease | Reviews to climb 1d → 30d | → 90d | → 365d |
|---|---|---|---|
| 130% | 13 | 18 | 23 |
| 195% | 6 | 7 | 9 |
| 250% | 4 | 5 | 7 |

And the cards that jog are the floored ones:

| Bucket | Median ease | At the 130% floor |
|---|---|---|
| jog | 130% | 49 of 58 (**84%**) |
| repeat | 130% | 19 of 21 (90%) |
| clean | 130% | 27 of 38 (71%) |

**47 of 58 jogged cards need 8+ consecutive successes to regain the interval they just
lost** — and they are by definition the cards that fail, so most will reset partway.
A treadmill rather than a ramp. This is the ease damage documented above, arriving as a
throughput problem.

## Pending decision: FSRS

Recommended, not yet done, deferred until more review data exists.

1. Back up the server and bridge collections.
2. Delete the 2,873 fake reviews — six dates, identified by sub-1.5 s answer times, 2%
   of the log. FSRS trains on review history, and sub-second perfect recalls would
   teach it a superhuman memory.
3. Enable FSRS and optimise against the cleaned history.
4. Keep triage: FSRS schedules better but will not pull chronic failures out of daily
   rotation.
5. Revisit pass/fail grading. Its justification was SM-2's ±15 pp ease mechanics; with
   ease gone that reasoning lapses, though "do not ask a human to grade what a machine
   measured" still stands.

**Why FSRS over raising `lapse.mult`** (currently 0%, so every lapse collapses to one
day): raising it to ~30% is a real improvement but works around a broken exponent.
FSRS has no ease factor at all, so the 130% floor and everything downstream of it stop
existing. A third of the Japanese collection is in that floor.

**Unresolved risk, to test before touching the real collection:** it is not known
whether revlog *deletions* propagate through Anki's sync, or whether the server keeps
the rows. If they do not propagate, the clean-up needs a full upload to the sync server
followed by a full download on every other device. Testable against throwaway
collections and the live sync server without risking anything.

## Also worth noting on this date

- **200 derived notes** exist (141 kanji, 59 audio-writing) of which **189 are still
  new**, at `new/day = 5` — roughly a month of fresh introductions queued behind an
  undrained backlog. Worth an explicit decision rather than accumulation.
- **`stroke_samples` is empty.** The calibration capture is on disk but the running
  server predates it, so no handwriting samples have been recorded and `/calibrate` has
  nothing to tune against. Restarting the server starts collection.
- **No review is logged above the 60 s cap.** The long pauses are absent rather than
  inflated: they hit the "queue moved on" bug and were refused, so they were never
  logged at all.

## What to re-measure before deciding

Mature-card retention over a fortnight of consistent reviewing; whether the jog share
stays near 57%; whether the repeat bucket keeps growing or plateaus; and whether the
backlog turns over once triage has pulled the chronic failures out. If retention
recovers toward 85% on its own as the fabricated intervals get re-anchored, the case
for FSRS weakens considerably — the argument rests on the ease floor, not on the
retention figure.
