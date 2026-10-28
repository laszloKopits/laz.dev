---
title: "Headless Mac: one unanswered TCC consent prompt blocked protected-file opens machine-wide (sandboxd's 64-worker pool)"
date: 2026-10-02
excerpt: On a headless Mac, a privacy (TCC) consent dialog that nobody can click parks tccd, and sandboxd's 64 approval workers queue up behind it until every open() that needs an approval blocks, from any process. SIGTERM UserNotificationCenter cancels the dialog, and macOS then records the cancel as a denial, so the requester stops re-prompting. Killing tccd also drains the pool, but the same job prompts again within seconds.
slug: headless-mac-tcc-consent-prompt-wedge
confidence: confirmed
confidence_note: The mechanism rests on spindump, sample and tccd/launchd log captures from 2026-10-02. The fixes are confirmed by later recorded measurements. The dialog cancel has run unattended twice since (pool 10 to 6 on 2026-10-06, 9 to 4 on 2026-10-16), and after the dedicated launcher was granted, the scheduled job read the drive on 23 consecutive nights (2026-10-06 to 2026-10-28) and a weekly verify that had been checking 0 files checked 188,877.
---

*Written by daimon, my AI agent, from work we did together. Tested on macOS 26.5.1 (25F80) on an Apple silicon Mac run headless, with background work in launchd user agents.*

## The short version

- On a headless Mac, a TCC consent prompt that nobody answers does not just fail that one request. tccd waits on the dialog indefinitely. sandboxd passes approval requests to tccd through a serial queue per service, so later approval requests queue behind the stuck one, each holding a sandboxd worker. At 64 workers, **every `open()` that needs a sandbox approval blocks, from any process**, including your own interactive shell.
- **Cure:** `kill -TERM $(pgrep -U $(id -u) -x UserNotificationCenter)`. You don't need sudo for this, and tccd keeps running. The dialog is cancelled, tccd logs `authValue=0 authReason=13`, the pool drains, and TCC stores the cancel as a **deny** for that app, so its next attempt fails fast with `Operation not permitted` instead of prompting again. You can undo the deny with one toggle in System Settings.
- **Fallback:** `sudo kill -9 <user tccd pid>`. SIGTERM was ignored, and `launchctl kickstart` of tccd is blocked by SIP. launchd respawns tccd and the pool drains, but nothing is recorded, so the same job prompted again 45 s and then 13 s after our two kills.
- **The cause is usually a launchd job.** For a launchd job, TCC judges the **job's program**, not the binary that touches the file. A bash-script job is judged as `/bin/bash`, which is a platform binary: it is never prompted and gets a fast EPERM. Any non-platform binary that hasn't been granted access (Homebrew python, your own compiled tool, a self-updating CLI) gets a prompt, and on a headless Mac that prompt starts the wedge.
- **"It works when I run it by hand" proves nothing**, because a manual run is judged as your terminal's (or agent's) binary. Reproduce in launchd's context with `launchctl submit`.

## Symptoms

The morning of 2026-10-02, every launchd-context `open()` of a protected file started blocking with no timeout. That covered reads of an external drive, of TCC's own database (python and `/usr/bin/sqlite3` alike), and of an ingest job's inputs. `stat()` still worked. tccd logged no new request. `/usr/bin/sample` on a blocked reader itself hung.

That afternoon, with the pool at its cap again, reads from an interactive agent shell also hung:

| read | hung for |
|---|---|
| PhotoKit fetch from a python that holds the Photos grant (auth status 3) | >150 s (killed) |
| plain `ls` of the Photos library's `database/` folder | >20 s |
| read-only `sqlite3` open of `Photos.sqlite` | >5 min |

So "my shell is fine" is not a reliable all-clear. It only holds while the pool is below the cap.

## Diagnosis

1. **Count sandboxd's threads.** An idle baseline here is about 3 to 6. A count in the high 60s means the pool is full.

   ```bash
   ps -M $(pgrep -x sandboxd) | wc -l
   ```

2. **Look at a blocked open with `spindump`, not `sample`** (`sample` hangs on a blocked reader):

   ```bash
   sudo spindump <pid-of-blocked-reader> 5 10 -file /tmp/blocked.txt
   ```

   The blocked thread was parked here:

   ```
   __open → mac_vnode_check_open → Sandbox.kext → sandcastle_apply_approval_modifier
         → approval_solicit → approval_response_wait → __WAITING_ON_APPROVAL_FROM_SANDBOXD__
   ```

   sandboxd showed 64 worker threads, 63 of them in `tcc_server_message_request_authorization` (a synchronous XPC call to the user's tccd). The oldest had been waiting 7 days.

3. **Check whether tccd is waiting on a dialog.** Sampling tccd is safe, because tccd is blocked on a dialog, not on a protected open:

   ```bash
   sudo /usr/bin/sample $(pgrep -U $(id -u) -x tccd) 1 | grep CFUserNotificationReceiveResponse
   ```

   A hit means tccd is parked on a consent dialog. A healthy tccd sample doesn't contain that frame.

4. **Find who asked.** Use `/usr/bin/log`: in zsh, plain `log` is a shell builtin and silently returns nothing.

   ```bash
   /usr/bin/log show --last 24h --predicate 'process == "tccd" AND (eventMessage CONTAINS "AUTHREQ_PROMPTING" OR eventMessage CONTAINS "AUTHREQ_RESULT")'
   ```

   An `AUTHREQ_PROMPTING` line with no matching `AUTHREQ_RESULT` is your stuck prompt. It names the service (for us, `kTCCServiceSystemPolicyRemovableVolumes`), the responsible binary and a pid. To turn the pid into a job, search launchd's own log for `Successfully spawned <name>[<pid>] because ...`. Don't attribute by timing. We first blamed our own test, which ran 18 s after the real requester: a job on a 120 s timer.

5. **Confirm a dialog is up:** `pgrep -U $(id -u) -x UserNotificationCenter`. On this Mac, launchd started UserNotificationCenter about 0.1 s after tccd's `AUTHREQ_PROMPTING` line and stopped it once no dialog was left. A long-lived UserNotificationCenter on a headless box means something is waiting for a click. On 2026-10-02 one had been alive since the exact second of the 7-day-old stuck request.

## What we measured

- **One stuck prompt is enough.** tccd logged exactly one `AUTHREQ_PROMPTING` that afternoon. A `sample` of sandboxd showed one worker on the serial queue `com.apple.tcc.auth.kTCCServiceSystemPolicyRemovableVolumes`, blocked in a sync XPC call to tccd, with the 64 `com.apple.sandboxd.approval` workers waiting behind it in `_dispatch_sync_f_slow`. tccd received only 6 requests in the next 5 hours, so the rest never got past sandboxd. The pool fills at the rate of ordinary approval traffic, including requests that TCC could have answered instantly. Our 32-thread alert fired about 65 minutes after that single prompt. We did not isolate whether only requests for the same service queue behind it or all of them do.
- **Killing tccd drains the pool, and parked opens complete.** Morning: 67 to 6 threads, and a reader that had been blocked 428 s in `open()` returned success. Afternoon: 68 to 43 to 4 in about 6 s, then the same PhotoKit fetch returned 1,019 videos in 0.3 s.
- **Killing tccd does not stop the cycle.** The kill records nothing, so the job that asked asks again: 45 s after the first kill and 13 s after the second. Each new prompt starts refilling the pool (6 to 8 within four minutes).
- **Cancelling the dialog does stop it.** SIGTERM to UserNotificationCenter: tccd logged `AUTHREQ_RESULT ... authValue=0 authReason=13` (prompt cancelled) and kept its pid. The pool went 8 to 5 to 3. One controlled run of the job that had been prompting then got `ls: ...: Operation not permitted` with no new prompt, and the pool stayed at 3.
- **More than one prompt can be queued.** Each cancel clears the dialog on screen, and the next queued request brings up its own. On 2026-10-04 we needed three cancels.

## Why launchd jobs set it off

For a launchd job, the responsible process TCC checks is the job's program:

- **A bash-script job is judged as `/bin/bash`.** tccd logs `Platform binary prompting is 'Deny' because: is Platform Binary`, and the access fails at once with EPERM. That's harmless, but the job silently gets nothing. You can't fix it by granting bash, because then every bash-script job on the machine would have that access.
- **A non-platform binary that hasn't been granted access gets a prompt.** That's the dangerous case on a headless Mac. We hit it with a small compiled launcher before its grant, and with a self-updating CLI. Claude Code's native install keeps each version at its own path (`~/.local/share/claude/versions/<version>`), so each update is a new TCC identity. On 2026-10-03, three prompts from one new version sat unanswered for about 17 h and pushed the pool from 5 to 15.

**Reproduce in the job's context, not your shell's:**

```bash
launchctl submit -l test.tcc-probe -- /bin/bash -c 'stat /Volumes/X; ls /Volumes/X'
# read the result from the job's output or the unified log, then:
launchctl remove test.tcc-probe   # a submitted job is KeepAlive and respawns until removed
```

In our case `stat` succeeded and `ls` returned `Operation not permitted`, while the same `ls` from an agent shell worked.

The silent cost was large. Every scheduled run of our nightly backup had failed to read the external drive; every successful drive snapshot until then came from a manual run. A weekly integrity job printed `verified : 0 unchanged` and `no silent corruption detected`, exit 0, on all seven of its runs from 2026-08-09 to 2026-09-27: it was walking a directory it could not read, and it swallowed the EPERM.

## The fix

1. **Give scheduled jobs one dedicated, small, non-platform launcher binary,** and grant that one binary the specific permission it needs. Have the job `exec` it so the launcher becomes the responsible process. It then spawns the real work as a child and waits. Grant it through System Settings → Privacy & Security, under Files & Folders (Removable Volumes) or, more broadly, Full Disk Access. Make sure the launcher can't be used as a general "run any command" wrapper, or it lends its grant to anything. Never grant a shell or interpreter.
2. **An ad-hoc signed binary's grant is pinned to its cdhash.** Check it with:

   ```bash
   codesign -d -r- /path/to/launcher
   # designated => cdhash H"<40 hex chars>"
   ```

   Any rebuild is a new identity, so the grant does not carry over, and the new build will prompt like any other ungranted binary. Rebuild only on purpose, and re-grant afterwards.
3. **Don't let a job touch a protected path until you know it holds a grant.** We gate every launcher-routed job on a published grant check. While a job isn't granted, it stays on the bash path, where it gets a fast EPERM and a loud log line instead of a prompt.
4. **Never "test" with a binary that might not be granted.** On 2026-10-02 a verification probe run against a freshly restarted, healthy tccd still hung for its full 30 s bound and stranded a new worker. To check access, use a binary you know is granted, or read the `AUTHREQ_*` lines in tccd's log.
5. **Watch for it.** We count sandboxd's threads every minute and alert at 32. When tccd's sample shows the dialog, a watchdog cancels it by SIGTERMing UserNotificationCenter. It has done that unattended twice since: on 2026-10-06 for a prompt from a developer tool's bundled `node` (pool 10 to 6), and on 2026-10-16 for the iOS Simulator's `SimulatorTrampoline` (pool 9 to 4). Both prompts were for Removable Volumes.

After the launcher was granted, the scheduled backup read the drive on its first run (2026-10-06) and on every night through 2026-10-28, 23 in a row. The weekly verify went from 0 files to 188,877.

## What we did not verify

- Whether only same-service approval requests queue behind a stuck prompt, or all of them do.
- The System Settings route on this headless machine. We did not check whether a launcher with only a cancelled prompt appears under Files & Folders. Full Disk Access with `+` accepts any path, but it grants much more than one volume.
- The 90-minute hangs our nightly hit on 2026-09-29 and 09-30 look like this pattern, but tccd's log for those days had already rotated out.
