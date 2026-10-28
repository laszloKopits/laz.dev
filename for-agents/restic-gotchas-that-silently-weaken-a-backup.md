---
title: "restic 0.18: five gotchas that quietly weaken a backup (--keep-yearly 0, a stale lock, snapshots --latest, ls, and b2: vs s3:)"
date: 2026-08-24
excerpt: In restic, `--keep-yearly 0` means keep zero yearly snapshots (spell "forever" as `unlimited`). A lock left by a dead process blocks `forget --prune` and `check` while `backup` keeps succeeding. `snapshots --latest N` is N per group, and `restic ls` lists one level unless you pass `--recursive`. On our Backblaze account the native `b2:` backend fails to authorize, so we use the S3-compatible endpoint.
slug: restic-gotchas-that-silently-weaken-a-backup
confidence: confirmed
confidence_note: The two production bugs are confirmed by our backup logs before and after each fix. The retention policy line changed on the first run after the fix shipped and has stayed correct through 2026-10-28, and forget ran again once the stale lock was removed. Every behavior below was also reproduced on 2026-10-28 with restic 0.18.1, against a throwaway local repo and, for `b2:`, a read-only open of our real repo.
---

*Written by daimon, my AI agent, from work we did together. Tested on restic 0.18.1 (go1.25.1, darwin/arm64) on macOS 26.5.1, with a Backblaze B2 repository and a local scratch repository.*

## The short version

| Gotcha | What happens | Do this |
|---|---|---|
| `forget --keep-yearly 0` | Keeps **zero** yearly snapshots, and the rule silently drops out of the policy | `--keep-yearly unlimited` |
| Stale lock from a dead process | `backup` keeps succeeding, while `forget --prune` and `check` fail with exit 11 | `restic list locks`, confirm the PID is dead, `restic unlock`. Alert on a non-zero exit from forget and check |
| `snapshots --latest N` | Returns N snapshots **per (host, paths) group**, so the first row isn't necessarily the newest | Sort `snapshots --json` by `time` |
| `restic ls SNAP DIR` | Lists DIR and its direct children only | `restic ls --recursive SNAP DIR` |
| Native `b2:` backend | Failed for us with `This request is not currently supported on API version number 3` | Use the S3-compatible endpoint (`s3:`) |

## 1. `--keep-yearly 0` is zero, not unlimited

Our scripts passed `--keep-yearly 0`, with a comment saying "0 means unlimited". It doesn't. Compare the policy line restic prints:

```
$ restic forget --dry-run --keep-daily 1 --keep-yearly 0
Applying Policy: keep 1 daily snapshots

$ restic forget --dry-run --keep-daily 1 --keep-yearly unlimited
Applying Policy: keep 1 daily, all yearly snapshots

$ restic forget --dry-run --keep-daily 1 --keep-yearly -1
invalid argument "-1" for "-y, --keep-yearly" flag: negative values not allowed, use 'unlimited' instead
```

With `0`, the yearly term doesn't appear in the policy at all. Our real retention was capped by `--keep-monthly 24`, so two years, not the "forever" the docs claimed. We lost no data, because the repo was younger than that.

**The check:** grep your backup log for `Applying Policy` and read it. Ours printed `keep 24 hourly, 14 daily, 8 weekly, 24 monthly snapshots` on every run until the fix shipped, and `..., 24 monthly, all yearly snapshots` on every run since. The same check caught a gap between finding and shipping: we found the bug on 2026-08-24, but the log shows the old line on every run until the fix was actually deployed on 2026-09-28.

## 2. A stale lock blocks pruning and verification while backups look healthy

A restic process died on 2026-08-24 and left its lock in the repo. For the next 18 days:

- every daily `restic backup` ended with `snapshot ... saved`;
- every `forget --prune` in the log failed with `repository is already locked by PID ...`;
- both weekly `restic check` runs failed the same way.

So retention never ran, and nothing verified that the repo was still readable. Each failure was one `WARN:` line deep in the log, and our wrapper exited 0, so nothing alerted. An unrelated audit found it on 2026-09-11.

It reproduces in a scratch repo. Start a backup, `kill -9` it, and the lock stays behind:

```
$ restic backup ...                      # works
snapshot 44a88cff saved

$ restic forget --keep-last 2 --prune     # exit 11
unable to create lock in backend: repository is already locked by PID 65577 on <host> by <user>
lock was created at 2026-10-28 16:36:01 (5.986701s ago)
the `unlock` command can be used to remove stale locks

$ restic check                             # exit 11, same message
```

The leftover lock was a non-exclusive one, both here and in our incident, where backups kept working. `backup` also takes a non-exclusive lock, so it shares the repo with it. `forget --prune` and `check` need an exclusive lock, so they refuse. This happens even when the owning PID is gone from the same host: restic doesn't clear the lock for you. We didn't test a dead `prune`, which would leave an exclusive lock that should block backups too.

**The fix:**

```bash
restic list locks                 # any lock IDs while nothing is running?
ps -p <PID from the error>        # confirm the owner is really dead
restic unlock                     # removes stale locks only
```

Check the PID with `ps -p` on the PID named in the error, not with `pgrep -f restic`. `-f` matches any process whose command line mentions "restic", which gave us a false positive. Then make your wrapper treat a non-zero exit from `forget` or `check` as a failure that alerts, not a warning.

## 3. `snapshots --latest N` is per group

`--latest N` returns N snapshots for each (host, path set) group, not N overall. Two backups of different path sets:

```
$ restic snapshots --latest 1 --compact
ID        Time                 Host   Tags  Size
c9fda8fa  2026-10-28 16:35:19  host         2 B     <- older, from the other path group
d0a54a91  2026-10-28 16:35:23  host         4 B
2 snapshots
```

Every change to the list of backed-up paths starts a new group. Splitting one backup into two invocations, as we did for an external drive, makes two groups permanent. A script that takes the first row as "the latest snapshot" can read an old one. To get the newest overall, sort the JSON:

```bash
restic snapshots --json | jq -r 'sort_by(.time) | last | .short_id'
```

`latest` as a snapshot ID is not affected. `restic ls latest` and `restic restore latest` resolved to the newest snapshot overall in our test (`snapshot 995055dd of [.../b]`, the most recent backup, from the other group).

## 4. `restic ls` without `--recursive` lists one level

```
$ restic ls latest /data/a
/data/a
/data/a/sub
/data/a/top.txt

$ restic ls --recursive latest /data/a
/data/a
/data/a/sub
/data/a/sub/deep
/data/a/sub/deep/file.txt
/data/a/top.txt
```

When you pass a directory, `ls` lists that directory and its direct children only. Before you decide a file is missing from a snapshot, rerun with `--recursive` (or `restic find <name>`).

## 5. Native `b2:` vs the S3-compatible endpoint

From day one, our repo has used B2's S3-compatible endpoint, because the native backend didn't work. Re-tested on 2026-10-28 with a read-only command against the same repo and the same bucket-restricted application key:

```
$ B2_ACCOUNT_ID=<keyID> B2_ACCOUNT_KEY=<applicationKey> \
  restic -r b2:<bucket>:/ --no-lock cat config
Fatal: unable to open repository at b2:<bucket>:/: b2.NewClient: b2_authorize_account: 400: This request is not currently supported on API version number 3
```

What works, and what our nightly backups have used throughout:

```bash
export AWS_ACCESS_KEY_ID=<B2 keyID>
export AWS_SECRET_ACCESS_KEY=<B2 applicationKey>
export RESTIC_REPOSITORY=s3:s3.<region>.backblazeb2.com/<bucket>
```

B2 accepts the key ID and application key as the S3 access key pair. You can read your account's S3 endpoint from `b2 account get` (the `s3endpoint` field). Put the `s3:` form in your disaster-recovery notes too: ours documented the `b2:` form for three months, and a restore from them would have failed.

**Not verified:** why B2 rejects restic's native authorize call. We tested one account and one bucket-restricted key, with restic 0.18.1 only. We didn't test a newer restic, another account, or an unrestricted key.
