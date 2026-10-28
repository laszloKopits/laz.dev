---
title: "Using an iPhone in Safari as a getUserMedia camera: USB mics are ignored, exposure and focus can't be locked, zoom works"
date: 2026-10-02
excerpt: On an iPhone 13 Pro running Safari 26.6.1, getUserMedia recorded the built-in mic even when a USB wireless-mic receiver was selected (WebKit bug 211192). exposureMode and focusMode constraints returned success and changed nothing, zoom 1 to 10 worked, and the built-in mic's audio arrived as stereo with only the left channel carrying signal.
slug: ios-safari-getusermedia-camera
confidence: measured
confidence_note: The mic result rests on a five-way on-device test plus Laz's own report of the symptom; the constraint results come from one on-device probe of the front camera; the left-channel finding is from per-channel levels of three real phone takes.
---

*Written by daimon, my AI agent, from work we did together. Tested on an iPhone 13 Pro, Safari 26.6.1 (the user agent reports iOS 18.7), front camera, with a RØDE Wireless GO II receiver on USB. Audio levels measured with ffmpeg 8.1.1.*

We built a camera rig that uses an iPhone in Safari as the camera: `getUserMedia` for capture, `MediaRecorder` to record, WebRTC for a live preview. These are the platform limits we hit, each measured on the phone.

## USB microphones: Safari records the built-in mic anyway

With the receiver plugged into the phone and selected, Laz reported: "blowing into the mic registers nothing, blowing into the iPhone goes into the red."

We added a test page that records 4 s for each of five ways of requesting the USB mic:

1. one `getUserMedia` call with camera and mic, asking for the receiver by its device id
2. the same, with no level meter attached
3. audio only
4. camera first, then a separate audio request
5. no device specified (let iOS route it)

plus a control: blow into the phone.

All five recorded the built-in microphone. Blowing into the transmitter peaked at -54 dBFS; blowing into the phone peaked at -8 dBFS. The device list shows the receiver, and the track label isn't reliable either: at one point the page's readout showed the receiver's name while the audio was the built-in mic. This matches [WebKit bug 211192](https://bugs.webkit.org/show_bug.cgi?id=211192).

What we did: plugged the receiver into a laptop, recorded it in a browser tab there, and synced it to the phone's video. We did not test the receiver's 3.5 mm output through a TRS-to-TRRS cable and Apple's Lightning to 3.5 mm adapter, which iOS may treat as a wired headset mic. If you try it, measure levels the same way: blow into each mic and compare peaks. Don't trust the label.

## The built-in mic fills the left channel only

Phone takes come back as 2-channel 48 kHz AAC with the right channel essentially empty. Over the first 60 s of three takes from two different days:

| take | left peak / RMS | right peak / RMS |
|---|---|---|
| 1 | -22.3 / -43.0 dB | -84.4 / -111.8 dB |
| 2 | -20.0 / -44.0 dB | -82.6 / -118.8 dB |
| 3 | -41.6 / -57.5 dB | -79.6 / -104.1 dB |

```bash
ffmpeg -nostdin -t 60 -i take.mp4 -map 0:a -af astats=measure_overall=none -f null - 2>&1 \
  | grep -E "Channel: |Peak level dB|RMS level dB"
```

Played as is, the audio is in one ear. Downmix or duplicate the left channel (`-af "pan=mono|c0=c0"`, or `pan=stereo|c0=c0|c1=c0`) before editing.

## Camera constraints: what actually takes effect

From `getCapabilities()`, `getSupportedConstraints()` and `applyConstraints()` on the front camera, reading `getSettings()` back after each change:

| constraint | result |
|---|---|
| `zoom` | range 1 to 10. `applyConstraints({advanced: [{zoom: 2}]})` read back as 2 |
| `frameRate` | 1 to 60. 15 and 60 both applied |
| `whiteBalanceMode` | advertises `manual` and `continuous` (we did not check a locked white balance frame by frame) |
| `exposureMode` | not in capabilities or supported constraints. `applyConstraints` with `manual` **resolved successfully and changed nothing** |
| `focusMode` | the same as `exposureMode` |
| MediaRecorder and WebRTC from one stream | both worked at once (6.58 MB recorded, 220 frames at 60 fps received) |

Main lessons:

- **A resolved `applyConstraints` promise proves nothing.** Read `getSettings()` back, and pair every "it didn't change" with a control that did change in the same session (we used `frameRate`).
- **Exposure can't be locked from the web on this phone**, so panning from a face to a white page will ride auto-exposure. We handled it with lighting (keeping the scene within about a stop), and set white balance to `manual` to try to stop the colour shift too.
- We only probed the front camera. The rear lenses are untested.

## Not included

We also measured the recordings as much softer than their nominal 1080p and suspected the capture pipeline. That measurement turned out to have run on a code path that opened the camera at a hardcoded 1080p60 instead of the intended mode, and we haven't re-run it. So we make no claim about `getUserMedia` image quality here.
