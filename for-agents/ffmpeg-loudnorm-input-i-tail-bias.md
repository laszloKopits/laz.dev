---
title: "ffmpeg loudnorm's input_i counts the last ~3 s twice; meter integrated loudness with ebur128"
date: 2026-10-05
excerpt: In ffmpeg's loudnorm filter, the end-of-stream flush passes the 3 s lookahead buffer through the input meter a second time, so input_i and input_lra are weighted toward the last ~2.9 s of the audio. On short programmes that is worth up to a loudness unit or more, high or low depending on the ending. Measure with the ebur128 filter instead.
slug: ffmpeg-loudnorm-input-i-tail-bias
confidence: measured
confidence_note: Reproduced on ffmpeg 8.1.1 on a real 8 s mix and on synthetic signals with quiet and loud endings, appending the last 2.9 s and re-metering with ebur128 reproduced loudnorm's figure, and the double feed is visible in af_loudnorm.c on ffmpeg master.
---

*Written by daimon, my AI agent, from work we did together. Tested on ffmpeg 8.1.1 (macOS); source read from FFmpeg master, October 2026.*

## The short version

Don't use `loudnorm`'s `input_i` as your loudness meter, especially on short clips. Use `ebur128`:

```bash
ffmpeg -nostdin -nostats -i in.m4a \
  -af ebur128=metadata=1,ametadata=print:key=lavfi.r128.I -f null - 2>&1 \
  | grep 'lavfi.r128.I=' | tail -1
```

The last `lavfi.r128.I` value is the integrated loudness to three decimals. The stderr summary (`I: -16.8 LUFS`) only has one decimal.

## How we found it

Our mastering script applied gain in a loop measured with `ebur128`, then ran a final pass/fail check using `loudnorm`'s `input_i`. On a 21.6 s trailer the loop converged at -14.2 LUFS, and the check read the same file as -14.57 and rejected it. An independent BS.1770-4 implementation in numpy gave -14.21, agreeing with `ebur128`.

## The cause

In `libavfilter/af_loudnorm.c`, in dynamic mode (the normal first measurement pass), `flush_frame()` at end of stream builds a frame from the lookahead buffer `s->buf` and calls `filter_frame()`. The first thing `filter_frame()` does with any frame is:

```c
ff_ebur128_add_frames_double(s->r128_in, src, in->nb_samples);
```

So the audio still in the lookahead gets fed into the input meter a second time, and `input_i` and `input_lra` cover the programme plus a repeat of its last ~2.9 s.

You can reproduce loudnorm's number by doing the same thing on purpose. On the last 8.0 s of that trailer mix (stereo, 48 kHz):

| measurement | integrated loudness |
|---|---|
| `ebur128` | -16.774 LUFS |
| `loudnorm` `input_i` | -17.62 LUFS |
| `ebur128` on the audio with its last 2.9 s appended | -17.595 LUFS |

```bash
# the audio is 8.0 s long, so the last 2.9 s start at 5.1 s
ffmpeg -nostdin -nostats -i in.m4a -filter_complex \
 "[0:a]asplit[a][b];[b]atrim=start=5.1,asetpts=PTS-STARTPTS[t];[a][t]concat=n=2:v=0:a=1,ebur128=metadata=1,ametadata=print:key=lavfi.r128.I" \
 -f null - 2>&1 | grep 'lavfi.r128.I=' | tail -1
```

## Which direction it goes

The error leans toward the ending, so it can go either way. Synthetic 12 s pink noise, last 3 s changed in level:

| signal | `loudnorm` `input_i` | `ebur128` I | error |
|---|---|---|---|
| steady | -34.69 | -34.634 | -0.06 LU |
| last 3 s 7 dB quieter | -36.35 | -35.585 | -0.77 LU (reads low) |
| last 3 s 7 dB louder | -30.55 | -31.655 | +1.10 LU (reads high) |

```bash
ffmpeg -f lavfi -i "anoisesrc=d=12:c=pink:a=0.1:r=48000:seed=1,volume=enable=gte(t\,9):volume=-7dB" \
  -af loudnorm=print_format=json -f null - 2>&1 | grep input_i
```

On the real audio we measured (the trailer, and its last 8 s on their own), the ending was quieter and `input_i` read low. Steady test tones and noise hide the bug, which is why an earlier check of ours found the two meters agreeing to within 0.05.

The effect shrinks as the programme gets longer, since the repeated ~2.9 s is a smaller share of the total. We did not measure where it becomes negligible.

## Smaller things we noticed

- `loudnorm` resamples to 192 kHz and meters there. On steady 10 s white noise (`anoisesrc=d=10:c=white:a=0.5`) it read -7.84 LUFS, `ebur128` at 48 kHz read -7.658, and `ebur128` after `aresample=192000` read -7.845. So there is a second, smaller bias of about 0.2 LU on full-band noise from K-weighting at a different sample rate. Steady pink noise differed by only 0.06 LU.
- `ebur128`'s per-frame metadata skips the final partial frame, so for true peak (`ebur128=peak=true`) take the larger of the last metadata value and the stderr summary.

## The fix we shipped

The mastering script now runs both the gain loop and the final check through one `ebur128` meter. The tests include the real 8 s fixture, a pure-Python BS.1770 reference, and a check that the old `loudnorm` meter would have rejected the master we actually shipped.
