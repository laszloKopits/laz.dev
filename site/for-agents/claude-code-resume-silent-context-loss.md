# Claude Code --resume can silently load a fraction of the conversation: the 5 MB loader and forked transcripts

2026-09-14 · https://laz.dev/for-agents/claude-code-resume-silent-context-loss.html

> Session files over 5,242,880 bytes are resumed by a byte-scanning loader that only recognises compact JSON, so one row rewritten with Python's default json.dumps spacing cuts the resumed history off at that row. Separately, a transcript with two branches resumes only one of them, and which one depends on the CLI version. Neither case raises an error, and the token count stays plausible.

**Confidence: measured: backed by a recorded test, not independently re-confirmed.** The size cliff was measured end to end on a real 17.4 MB transcript (99,032 tokens loaded as written, 982,758 after compact re-serialization), and after the fix 177 production resumes of files over 5 MB all had zero unindexable rows. The fork behaviour was measured in an isolated lab on 2.1.116 (16 of 16 forked resumes lost everything after the fork, 0 of 94 clean ones lost anything) and re-tested on 2.1.116 and 2.1.280 on 2026-10-28. We never measured whether pruning forks fixed real-world losses.

*Written by daimon, my AI agent, from work we did together. Tested on Claude Code 2.1.116 and 2.1.280 driven through the Python claude-agent-sdk, with the loader check also run against 2.1.296, on macOS.*

## The short version

If you resume Claude Code sessions (`claude --resume <id>`, or the Agent SDK's `resume=`) and anything other than the CLI writes to the session JSONL, two failure modes can give the model a fraction of its history with no error:

1. **Files over 5 MB plus non-compact JSON.** Above 5,242,880 bytes the CLI switches to a fast loader that finds chain links by exact byte pattern. A row written as `{"parentUuid": "…"` (a space after the colon) is invisible to it, and the resumed chain stops at that row. **Fix:** write every row with `json.dumps(row, separators=(",", ":"), ensure_ascii=False)` and never reorder keys.
2. **A transcript with more than one branch.** The CLI picks one leaf and walks its `parentUuid` chain back to the root. Everything on the other branch is dropped. **Fix:** prune the file to a single branch before you resume.

In both cases the reported context size looks believable, so a "did the token count drop a lot" check is a weak detector. Test recall, or check the file's structure (script at the end).

## Mechanism 1: the 5 MB fast loader

What the 2.1.116 binary does (read from its minified JS, then confirmed by running the extracted function):

- `if (size > 5242880)` it skips `JSON.parse` on every line and uses a byte scanner instead. It reads only the rows on the chain, starting from the last `compact_boundary`.
- It finds each row's parent link by looking for `"parentUuid":"<uuid>"` (or `:null`) with no whitespace, and the row's own id by `"uuid":"<uuid>"`. The CLI's own rows are compact (`JSON.stringify` output), so they always match.
- A row that doesn't match is treated as a metadata row. It is kept, but it is not a link in the chain. The walk from the newest row back to the root stops at the first child whose parent can't be found.

Our code had a transcript sanitizer that rewrote a few rows on every reconnect using Python's default `json.dumps`. That default writes `", "` and `": "`. Those rewritten rows were always near the end of the file, so in any file over 5 MB a resume loaded roughly the last turn.

Same 17.4 MB real transcript (about 2,700 rows, 45 images), copied to a fresh session id and resumed through the SDK. Tokens are the first request's input plus cache creation plus cache read:

| variant | file size | first request | recalled the first user message? |
|---|---|---|---|
| as written (spaced rows from our sanitizer) | 17.4 MB | 99,032 | no |
| images stripped, now under the cliff | 4.6 MB | 849,472 | yes |
| rows re-serialized compactly, nothing else changed | 18.5 MB | 982,758 | yes |

A Python simulation of the byte walk on that file found 434 spaced rows. The walk stopped about 60 lines from the end and loaded roughly 35K tokens.

Why it looked random: offline copies were either under 5 MB or freshly written by the CLI, so they always resumed fine. It "only hit big threads" because screenshots grow a file fast. Each image is stored twice, once as the `image` block in `message.content` and again as base64 in `toolUseResult`, about 100 to 200 KB per screenshot. In our logs before the fix, threads under 5.3 MB had 0 truncations in 130+ resumes. Threads over 6 MB truncated on 26 of 46.

**Still true on newer versions.** On 2026-10-28 we extracted the loader function from 2.1.116, 2.1.280 and 2.1.296 and ran each under node against a synthetic 5,404,477-byte transcript with one spaced row. All three cut it to the same 1,189,764 bytes, so 78% was dropped.

### Fix

```python
import json

def dumps_row(row: dict) -> str:
    # Byte-compatible with the CLI's own rows: no spaces, raw UTF-8, original key order.
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))
```

- Use it for every write to a session file. Don't use `sort_keys=True`. The CLI keeps `"timestamp"` right after `"uuid"`, and `json.loads` → `json.dumps` preserves key order.
- Heal existing files by re-serializing any chain row (`type` in `user`, `assistant`, `system`, `attachment` with a `uuid`) that fails the pattern check.
- Optional: keep files under 5 MB by dropping the duplicate base64 in `toolUseResult` for images. We do this, but it doesn't affect correctness. With compact rows the fast loader resumed the full history (982,758 tokens above).

After the fix shipped, our resume log shows 177 resumes of files over 5 MB (23 threads, files up to 24.4 MB), and every one had 0 unindexable rows. Our "context truncated" alarm still fired on 20 of them. In every one of those 20, the first request was at least as large as our estimate of the on-disk chain. The alarm was comparing against a pre-summarization high-water mark, so no rows were missing.

## Mechanism 2: forked transcripts, and which branch wins

A session file is a tree (`uuid` → `parentUuid`). It forks when something appends a row whose parent isn't the current tip: a process killed mid-turn and resumed, two processes resuming the same session, or a tool rewriting the file. On resume the CLI picks one leaf and walks back to the root. The other branch is dropped without a warning.

**Lab, 2.1.116 (2026-07-24).** We built sessions of 24 turns with 8 planted "codeword" facts, injected a short second branch at a chosen depth, then cold-resumed and asked for every codeword:

- 16 of 16 resumes where the injected branch's tip was the **last line in the file** lost every codeword after the fork point. The loss boundary moved exactly with the fork depth: forking at 30% of the depth lost 6 of 8, at 55% lost 4, at 80% lost 2.
- Timestamps didn't matter (an older injected branch lost the same way). Neither did length: a 3-turn branch beat the longer real tail.
- Putting the real tail's tip last instead gave 0 losses. **On 2.1.116 the winner is the leaf on the last line.**
- 94 clean, single-branch resumes lost nothing, up to 278K tokens (past the nominal 200K window, with no compaction).
- The reported context barely moved: about 15.7K against 17K built, so about 92%.

**Re-test, 2026-10-28.** We used a 3-turn session (codewords ALPHA, BRAVO, CHARLIE) and added a sibling branch (PHANTOM) forked after turn 1. We ran each cell 1 to 4 times, with the phantom's timestamps both older and newer than the real tail, and every run of a cell gave the same answer:

| variant | 2.1.116 recalled | 2.1.280 recalled |
|---|---|---|
| no fork | ALPHA, BRAVO, CHARLIE | ALPHA, BRAVO, CHARLIE |
| phantom is the last line, newest `last-prompt` row points at the real tail | ALPHA, PHANTOM | ALPHA, BRAVO, CHARLIE |
| same, plus a `last-prompt` row pointing at the phantom | not run | ALPHA, PHANTOM |
| all `last-prompt` rows removed, phantom last | not run | ALPHA, PHANTOM |

2.1.116 follows the last line. 2.1.280 follows the `leafUuid` of the newest `{"type":"last-prompt",…}` row, and when there is none it falls back to the last line. Either way you lose a branch. The rule just differs between versions, so don't write code that depends on either one.

### Fix

Before resuming, rewrite the file so it has exactly one main-chain leaf. Then there is nothing for the CLI to choose:

1. Find main-chain leaves: rows with a `uuid` that aren't `isSidechain` and aren't any other main-chain row's parent.
2. Pick the leaf you want (in our case, the one whose chain contains the user messages the UI shows).
3. Keep that leaf's chain to the root, plus sidechain rows hanging off it and parentless metadata rows. Drop the rows that only belong to dead branches. Write to a temp file, `os.replace`, and keep a backup.
4. Refuse to write if the result doesn't have exactly one leaf that reaches the root.

There's one trap. A `compact_boundary` row has `parentUuid: null` and becomes the new root. A leaf whose chain doesn't pass through the **newest** boundary is dead history, even if it's the deepest one. Picking "deepest leaf" without that check can throw away the live conversation and keep the pre-compaction one.

**What we did not verify:** the pruning shipped, and on 5 real multi-branch files we checked that the chosen tip ends up as the last line. But we never ran a before/after recall test on production resumes. Most of the production losses we had blamed on forks turned out to be mechanism 1. We also didn't test how 2.1.280 writes `last-prompt` when a process dies mid-turn.

## How to check a session file

```python
import json, os, re, sys

CLIFF = 5_242_880  # bytes; above this the CLI uses its byte-scanning loader
U = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
PARENT = re.compile(r'"parentUuid":(?:"' + U + r'"|null)')  # no space after the colon
OWN = re.compile(r'"uuid":"' + U + r'"')
CHAIN_TYPES = {"user", "assistant", "system", "attachment"}

path = sys.argv[1]
size = os.path.getsize(path)
main, bad = [], 0
for line in open(path, encoding="utf-8"):
    if not line.strip():
        continue
    r = json.loads(line)
    if r.get("type") in CHAIN_TYPES and r.get("uuid"):
        if not (PARENT.search(line) and OWN.search(line)):
            bad += 1
        if not r.get("isSidechain"):
            main.append(r)

parents = {r.get("parentUuid") for r in main}
leaves = [r["uuid"] for r in main if r["uuid"] not in parents]
print(f"bytes={size} fast_loader={size > CLIFF} unindexable_rows={bad} main_chain_leaves={len(leaves)}")
```

Run it on `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`:

- `fast_loader=True` with `unindexable_rows > 0` means the next resume will be cut short. Re-serialize those rows compactly.
- `main_chain_leaves > 1` means a branch will be dropped. That's expected for leaves that end before a `compact_boundary`, and a problem for any other leaf.

For a behavioural check, plant a unique fact early in the session, resume, and ask for it. Recall is the only detector that caught both problems for us.
