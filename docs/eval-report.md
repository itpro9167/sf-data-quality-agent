# sfdq eval report

- **Agent:** `mock` (`mock-claude`) (canned deterministic baseline; set `ANTHROPIC_API_KEY` to evaluate Claude)
- **Dataset:** seeded mock org, seed 42, as of 2026-11-04: 200 Accounts, 500 Contacts, 232 labeled problems, 10 look-alike decoys
- **Evaluated:** all findings (232 of 232 scanner findings)
- **Cost:** 464 model calls, 550,850 input / 75,810 output tokens (simulated), 0.2s

## Headline

- **Auto-fix precision 100%**: 58 of 58 automated proposals (merge / update) were exactly right. A wrong auto-fix corrupts data, so this is the number to protect.
- **Auto-fix recall 95%**: it found the right automated fix for 58 of 61 problems that have one.
- **Restraint 100%**: it asked the owner on 171 of 171 problems that need a human, with 0 unsafe auto-fixes.
- **Detection** (deterministic scanners): precision 100%, recall 100%; 0 of 10 look-alike decoys wrongly reported as duplicates.

## By issue type

| Issue type | Seeded | Detection P / R | Auto-fix precision | Auto-fix recall | Auto-fix F1 | Flag accuracy | Unsafe auto-fixes |
|---|---:|---:|---:|---:|---:|---:|---:|
| Duplicates | 30 | 100% / 100% | 100% (30/30) | 100% (30/30) | 100% | n/a (0/0) | 0 |
| Missing fields | 68 | 100% / 100% | 100% (8/8) | 100% (8/8) | 100% | 100% (60/60) | 0 |
| Stale records | 98 | 100% / 100% | n/a (0/0) | n/a (0/0) | n/a | 100% (98/98) | 0 |
| Invalid formats | 36 | 100% / 100% | 100% (20/20) | 87% (20/23) | 93% | 100% (13/13) | 0 |
| **All** | 232 | 100% / 100% | 100% (58/58) | 95% (58/61) | 97% | 100% (171/171) | 0 |

## By difficulty

Recall of the *automated* fix, split by how much the problem requires of the agent.

| Tier | Problems | Correct | Recall |
|---|---:|---:|---:|
| Mechanical (fix follows from the bad value) | 14 | 14 | 100% |
| Needs context (other fields / records) | 47 | 44 | 94% |
| Judgment (only the owner can decide) | 171 | 171 flagged | 100% |

## Where the agent and the labels disagree (3)

Most serious first (unsafe auto-fix, then wrong auto-fix, then missed fixes and detection gaps).

| Problem | Expected | Got | Why |
|---|---|---|---|
| Contact 'James Roberts': Email 'james.roberts@silverlinebrewing' is not a valid email (domain has no top-le... | update Email='james.roberts@silverlinebrewing.com' | flag_for_review | flag_for_review instead of update_field (fix was possible) |
| Contact 'Nicholas Martin': Email 'nicholas martin@cascadesoftware.io' is not a valid email (contains spaces) | update Email='nicholas.martin@cascadesoftware.io' | flag_for_review | flag_for_review instead of update_field (fix was possible) |
| Contact 'Ethan Larsen': Email 'ethan.larsen.northwindlogistics.com' is not a valid email (missing '@') | update Email='ethan.larsen@northwindlogistics.com' | flag_for_review | flag_for_review instead of update_field (fix was possible) |

## Method

- The seeded mock org is generated deterministically and every corrupted value is recorded with its *correct* value. Labels (`src/sfdq/eval/data/expected_fixes.json`) say what a careful analyst would do for each seeded problem, derived from that ground truth rather than from any scanner or agent output.
- The scanners run first; the agent then proposes a fix for each finding through the same tool-use loop and write policy as production, against a read-only Salesforce client. Nothing is persisted.
- A proposal is **correct** when its action matches the label and the details match: the right survivor and record set for a merge, the correct value (websites compared by domain) for an update, or a flag where only the owner can decide.
- **Precision** = correct automated proposals / all automated proposals. **Recall** = correct automated proposals / problems that have an automated fix. **Unsafe auto-fix** = an automated proposal where the label says to ask the owner.
- Re-generate the labels with `python -m sfdq.eval --write-labels`; a test fails if they drift from the generator.
