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
