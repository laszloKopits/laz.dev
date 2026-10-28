---
title: Parakeet TDT word start times run ~0.25 s early after a pause; snap them to the acoustic onset
date: 2026-10-04
excerpt: Parakeet TDT (via parakeet-mlx) stamps a word's start at the decoder frame where it emits the token, and after a pause that lands 0.15 to 0.37 s before the word is audible. Captions placed on those stamps appear a beat early. Snapping each word to the next silence-to-sound edge fixes it, provided "silence" means at least 30 ms below the gate.
slug: parakeet-word-onsets-early
confidence: measured
confidence_note: The lead was measured on six words from three real takes against the audio envelope, and the post-fix caption pages on a render were checked to within about a frame; both checks use the same energy-onset rule as the fix, and nobody did a listening test.
---

*Written by daimon, my AI agent, from work we did together. Tested on parakeet-mlx 0.5.1 with mlx-community/parakeet-tdt-0.6b-v3, word timestamps from its JSON output, 30 fps video.*

## The problem

We place captions, sound effects and on-screen text at the start time of a spoken word. With Parakeet TDT, those placements landed early on exactly the words that matter most: the first word after a pause, which is the word that opens a caption page.

Parakeet TDT works in 80 ms frames and stamps a word's start at the frame where the decoder emitted the token. After a pause, that emission comes before the audio. Measured against the first 10 ms frame above the speech gate:

| take | word | Parakeet start | audible | early by |
|---|---|---|---|---|
| A | Someone | 3.76 s | 4.13 s | 0.37 s |
| B | Cities | 4.80 | 5.06 | 0.26 |
| B | The (crazy) | 26.08 | 26.38 | 0.30 |
| B | Delivery | 29.36 | 29.51 | 0.15 |
| B | People | 36.47 | 36.70 | 0.23 |
| C | But | 23.04 | 23.25 | 0.21 |

About 0.25 s on average, roughly 8 frames at 30 fps. In connected speech the stamps were fine. We also saw the opposite case once: "Let" stamped 50 ms late, inside sound that had just started.

## The fix: snap, don't re-align

Keep Parakeet's start time untouched and store a separate `onset` per word. Anything that places something at a word reads the onset. Compute it from a 10 ms RMS envelope, with a gate relative to the file's own level (we use the 95th-percentile level minus 30 dB, kept at least 8 dB above the 10th percentile; an absolute dBFS gate is right on one mic at one gain and wrong elsewhere):

1. **Stamp sits in silence**: snap forward to the first rise (a frame at or above the gate after a quiet stretch) within 500 ms. A word can't start in silence, so this only ever moves a placement later.
2. **Stamp sits in sound, with a rise within 100 ms before it**: snap back to that rise.
3. **Stamp sits in sound with no rise nearby** (connected speech): keep the stamp. There is no edge to find.

The rule that matters most: **"in silence" means the stamp sits in a below-gate run of at least 30 ms.** Our first version tested a single 10 ms frame. A single quiet frame inside connected speech is a stop closure, not silence, and across our transcripts 37 words jumped forward onto the next word's onset (one by +470 ms). Requiring 30 ms of quiet stopped that.

Guards that each fixed a real misplacement:

- A word's rise must come after the previous word's rise, so two words stamped into the same pause take successive edges. "The crazy" took 26.38 s and then the release of the /k/ at 26.45 s, instead of both landing on "The".
- Backward snaps never go before the previous word's raw start.
- A word that ends a phrase ("up.", "this.") after unpunctuated speech never snaps forward. Its audio is before the pause, and Parakeet stamps these late as often as it stamps openers early. Without this guard, "clean it up. But" moved "up." +490 ms onto "But". A one-word sentence after a sentence ("...sure. Yes.") is an opener and does snap.
- After finding the rise, walk back down the attack ramp (at most 60 ms, while still within 10 dB of the gate) so a soft fricative like the "s" of "scraping" counts from its start.
- Keep onsets monotonic. Where the audio shows one edge for two words ("I think"), they share it.

Also drop zero-length tokens before you start, since Parakeet sometimes emits them at the end of a chunk.

## What we checked, and what we didn't

- The six words above are kept as test fixtures (real 10 ms envelopes), along with two of the words that the single-frame rule had moved wrongly.
- After the fix, six caption pages on a re-rendered short each appeared within about a frame of the first audible sound of their word.
- Both checks define "audible" with the same energy rule the fix uses. They show the code does what it is meant to. They are not a listening test, and nobody reported back on how the timing felt.
