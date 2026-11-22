# JARVIS server — Phase 1 (Memory engine) design

**Status:** Approved by Manj 2026-11-22 (P1 engine only; no automatic parental access; dedicated server before real memories)
**Date:** 2026-11-21
**Feature slug:** `phase-1-memory-engine`
**Inputs:** Architecture v2 §11 and §16 (project doc `Jarvis_Architecture_v2.md`), decisions D1–D8, `phase-0-handover.md`, Phase 0 design (`2026-11-21-phase-0-foundations-design.md`)
**Decision record:** ADR-010 (memory store: markdown + git, source of truth, audit link). The desktop backlog's next ADR moves to 011.

## 1. Goal

Give Jarvis a memory it owns: who people are, what they prefer, what happened and how things are done. The memory is stored as readable markdown with a full git history, behind the interface that the rest of Jarvis will call.

**Done when** memory tests M1–M4 (§10) pass in CI and in the live deploy check.

| # | Test | Expected |
|---|---|---|
| M1 | Write then retrieve a preference | Same entry, ID and source returned |
| M2 | Update an entry | New git version; old version recoverable |
| M3 | Inferred entry used for an action | Blocked until confirmed |
| M4 | `retrieve_context(daughter, school trip)` | Returns only relevant, permitted entries |

**Not in Phase 1** (spec §11.6 and this design): embeddings or vector search, any AI call (the brain router arrives in Phase 2), automatic consolidation or inference, external memory products (Engram, Mem0 and others stay frozen), a network API (the gateway is Phase 2), encryption at rest, and real personal data (§2, P1).

## 2. Kick-off decisions (approved 2026-11-22)

| # | Question | Proposed | Why |
|---|---|---|---|
| P1 | What Phase 1 delivers | **A: the tested engine only.** Nothing writes real memories until Jarvis can, in Phase 2. Tests and the deploy check use made-up people. | Keeps the phase small. Real personal data stays off the temporary shared server until Phase 2 brings login-based access (S3) and the encryption-or-move decision (§9). |
| P2 | Source of truth | **The markdown files.** The engine loads them into memory and answers from there. No database copy. | The whole memory is small (thousands of lines, read in milliseconds). One source of truth, readable and diffable by a person. |
| P3 | Where the memory lives | A **separate private git repo** on a Docker volume (`memory`) on the server. **No remote, ever**: the engine refuses to start if one is configured. | The code repo is public. Memory must never be a folder in it or pushable to it. |
| P4 | Relevance without AI | **Fixed rules:** subject, category, topic words and visibility (§6). | No brain router until Phase 2. Rules are predictable and testable; Phase 2 can add AI ranking behind the same call. |
| P5 | Audit | Every memory change also writes **one audit row** in Postgres: ID, action, tag change, git commit — **never the content**. | Git history can be rewritten by anyone with file access; the audit trail cannot (ADR-008). |
| P6 | Who can see what | Visibility is checked against a **viewer** on every read (§5). | M4 "permitted" needs to know who is asking. Phase 2 sets the viewer from login (S3). |

## 3. What lives in memory and what does not

The rule from v2 §11.1 stands: **memory never becomes a second task database.**

| Memory (this phase) | Ledger (Phase 0) |
|---|---|
| Who people are, in words | People, relationships and roles as records (`ledger.person`, `ledger.relationship`) |
| Preferences and habits | Tasks, events, commitments, approvals |
| What happened (episodes) | Context: time-bound, expiring (`ledger.context`) — never copied into memory |
| How recurring things are handled | Responsibilities with recurrence |
| Lessons from verified outcomes | Outcomes and receipts |

Scope rule (v2 §2): personal, family and household only. There is no work category; work appears only as availability, in the ledger. Patient or clinical content must never be written. The engine cannot detect this reliably, so the Phase 2 intake filter enforces it. In Phase 1 only tests write.

## 4. Storage

### 4.1 Layout

```
/var/lib/jarvis/memory/            # Docker volume "memory", private git repo, no remote
├── index.md                       # generated: one line per file with entry counts
├── people/<subject>.md            # identity entries + the person header (below)
├── preferences/<domain>.md        # travel, restaurants, shopping, communication, lifestyle
├── household/<domain>.md          # services, vendors, vehicles, recurring_activities
├── operational/<domain>.md        # routines, responsibilities, workflows, standing_instructions
├── operational/lessons_learned.md # all "learned" entries
└── episodic/<YYYY>.md             # one file per year, entries in date order
```

| Category | File | `domain` |
|---|---|---|
| identity | `people/<subject>.md` | none |
| preference | `preferences/<domain>.md` | required: travel, restaurants, shopping, communication, lifestyle |
| episodic | `episodic/<YYYY>.md` (year the episode happened) | none; `happened_on` date required |
| operational | `household/<domain>.md` or `operational/<domain>.md` | required: services, vendors, vehicles, recurring_activities → household; routines, responsibilities, workflows, standing_instructions → operational |
| learned | `operational/lessons_learned.md` | none |

Changes from the v2 sketch: episodic entries go in one file per year (`episodic/2026.md`) instead of a folder per year, until a year grows too long to read. The `core/` folder is folded in. Identity lives per person in `people/`; standing instructions are an operational domain; relationships as data stay in `ledger.relationship`, and memory describes them in words. The domain lists are fixed in code; adding one is a code change with a test.

Files are grouped for people to read. The engine does not care which file an entry sits in: every entry carries its own subject.

### 4.2 Person header

Each `people/<subject>.md` starts with a header that links the memory subject to the ledger:

```
# Daughter
person_id: 6b1e…   (ledger.person.id)
```

A subject is a lower-case slug (`manj`, `daughter`, pattern `^[a-z][a-z0-9-]{1,30}$`) or the special subject `household` for entries about the whole family. Writing about an unknown subject is refused. New subjects are added with `add_person(slug, person_id, display_name)`, which checks that the person exists in the ledger.

### 4.3 Entry format

Two lines per entry, as in v2 §11.4, with `about`, `by` and `topics` added:

```
- [m-0042] [CONFIRMED] [preference] Prefers morning domestic flights
  (about: household; source: msg-7f3a, 2026-11-21; by: manj; confirmed by: manj, 2026-11-22; visibility: FAMILY_SHARED; topics: travel, flights)
```

| Field | Rule |
|---|---|
| ID | `m-` + at least 4 digits. Allocated as highest ever used + 1. **Never reused**: archived entries stay in their file. |
| Tag | STATED, CONFIRMED, INFERRED, HISTORICAL, EXPIRED |
| Category | identity, preference, episodic, operational, learned |
| Content | One line, 1–500 characters, no line breaks |
| about | Subject slug |
| source | Where it came from (`manual`, `msg-…`, `test`) plus the date. Restricted to letters, digits, `-_.:` |
| by | Who recorded it: a person slug, or `agent:<name>` / `system` |
| confirmed by | Present only once confirmed |
| visibility | PRIVATE, SELECTED_PERSON, FAMILY_SHARED (§5). `shared with: a, b` when SELECTED_PERSON |
| topics | 0–8 lower-case words used for retrieval (§6) |
| happened on | Episodic only: the date of the episode |

**Strict reading:** if any line in any file does not parse, the engine refuses to load and names the file and line. A silently skipped line would be a lost memory. In Phase 1 only the engine writes the files.

### 4.4 Git

- One change = one commit, made under a file lock (`fcntl`), so concurrent writers get distinct IDs and a linear history.
- Commit author `Jarvis <jarvis@localhost>`; message `memory: <action> m-0042 (<category>)` with an `Actor:` trailer. Content is in the file, not repeated in the message.
- Uses the `git` command-line tool (added to the server image).
- Before every read, the engine checks whether the repo's HEAD has moved and reloads if it has.
- On first start, the engine creates the repo with the empty layout and `index.md` in one initial commit.

## 5. Visibility

Every read takes a `viewer` (a person slug, or `system`). Phase 1 accepts three of the five ledger visibility values; the other two are refused on write until there are groups and organizations to share with.

| Visibility | Who can read it |
|---|---|
| PRIVATE (default) | The subject and the person who recorded it |
| SELECTED_PERSON | The above, plus the people listed in `shared with` |
| FAMILY_SHARED | Everyone in the household |
| SELECTED_GROUP, ORGANIZATION_SHARED | Refused on write in Phase 1 |

`system` (Jarvis's own background work) can read everything; it never shows anything to a person except through a read that names that person as viewer. The household membership comes from the ledger (`ledger.person.organization_id`).

## 6. `retrieve_context()` — designed first

```
retrieve_context(person, task, *, viewer) -> list[MemoryEntry]
```

Rules, in order:

1. **Candidates:** entries about `person`, plus entries about `household`.
2. **Drop** EXPIRED entries.
3. **Drop** entries the viewer may not see (§5).
4. **Keep identity entries** about `person` always: who someone is is always relevant.
5. **Keep other entries** only if a task word matches one of their topics or a word in their content. Task words are lower-cased, stop-words removed ("the", "for", "a" …) and plurals folded ("trips" → "trip"). A small fixed alias table, kept in code with tests, links common words to topics (for example trip ↔ travel, flight ↔ travel, exam ↔ school).
6. **Order:** CONFIRMED, then STATED, then INFERRED, then HISTORICAL; within that, the most recent first. Maximum 50 entries.

Every returned entry carries `drives_action`, which is true only for STATED and CONFIRMED (v2 §11.3). INFERRED and HISTORICAL entries are returned so Jarvis can mention them ("you seemed to prefer…"), but they cannot drive an action (§7, M3).

**Worked example (M4):** viewer `manj`, `retrieve_context("daughter", "school trip")` over the test memory returns the daughter's identity entry and her FAMILY_SHARED entry "Gets travel-sick on long bus rides" (topics: travel). It does **not** return her restaurant preference (not relevant), her PRIVATE entry recorded by herself (not permitted), an EXPIRED entry about her old school (expired), or Manj's own flight preference (different subject).

## 7. Tags and their rules

Tags change only through engine calls, like ledger states through `ledger.transition()`:

| From | Call | To | Allowed by |
|---|---|---|---|
| (new) | `write` | STATED, INFERRED or HISTORICAL | Anyone; agents and `system` may only write INFERRED or HISTORICAL |
| STATED or INFERRED | `confirm` | CONFIRMED | **A person only** — the subject or the recorder. Jarvis can never confirm its own inference. |
| CONFIRMED | `update` | STATED | New wording has not been confirmed yet |
| STATED, INFERRED, HISTORICAL | `update` | unchanged | — |
| any but EXPIRED | `archive` | EXPIRED | A person, or `system` with a reason |
| EXPIRED | anything | refused | Kept for history only |

Writing CONFIRMED or EXPIRED directly is refused. Refused calls raise a clear error and are audited as REJECTED, as in the ledger.

**The action guard (M3):** `assert_actionable(memory_ids)` raises `NotActionable` if any entry is INFERRED, HISTORICAL or EXPIRED. The Phase 2 policy engine calls it before any action that relies on memory. In Phase 1 the test calls it directly.

## 8. The interface

`server/src/jarvis_server/memory.py` becomes a package. The Phase 0 signatures gain the arguments the rules above need (`viewer`, `by`, visibility, topics, domain); nothing outside the module uses them yet, so this costs nothing.

```python
retrieve_context(person, task, *, viewer) -> list[MemoryEntry]
read(subject, category, *, viewer) -> list[MemoryEntry]
search(query, *, viewer, subject=None, category=None) -> list[MemoryEntry]
write(subject, category, content, tag, source, *, by,
      visibility=PRIVATE, shared_with=(), topics=(), domain=None,
      happened_on=None) -> str                       # returns memory_id
update(memory_id, content, source, *, by) -> None
confirm(memory_id, *, by) -> None
archive(memory_id, *, by, reason) -> None
history(memory_id) -> list[MemoryVersion]           # every git version of one entry (M2)
assert_actionable(memory_ids) -> None               # M3
add_person(slug, person_id, display_name, *, by) -> None
```

`MemoryEntry` gains: domain, by, recorded_at, confirmed_by, visibility, shared_with, topics, happened_on, version (git commit of its last change) and `drives_action`. A matching `contracts/memory-entry.schema.json` is added with examples, so Phase 2 and the desktop node share one definition.

`search` uses the same word matching as step 5 of §6, across everything the viewer may see, optionally limited to one subject or category.

## 9. Audit, backup and security

**Audit link (migration 0007):**
- `audit.event` accepts `entity_type = 'memory'`, and `audit.action` gains UPDATE, CONFIRM, ARCHIVE.
- `entity_id` is a UUID derived from the memory ID (UUID v5, so always the same for `m-0042`); the memory ID and git commit go in `details`. `from_state` / `to_state` hold the tag. **No content, ever.**
- Rows are written only by a new `SECURITY DEFINER` function, so the app role still cannot forge audit rows.
- The same migration closes S5 (revoke CONNECT on database `postgres` from PUBLIC).

**Order of a change:** take the lock → write the file → git commit → write the audit row with the commit hash. If the audit row fails, the engine undoes its own commit (`git reset --hard` to the previous commit, safe because it holds the lock) and raises. If the process dies between commit and audit row, the startup check finds commits with no audit row and adds them as `system`, reason `reconciled`.

**Backup:** a daily job in core writes a `git bundle` of the memory repo into the existing `backups` volume, keeping 7 days. Restoring is `git clone` from the bundle; added to the runbook. (P0-7 stays deferred; with P1 there is no real data yet.)

**Security:**
- The `memory` volume is mounted only into `core`, owned by uid 10001. `init`, `scheduler` and `backup` never see it. To write the daily bundle, `core` also mounts the `backups` volume, at `/backups/memory` only.
- No git remote: checked at every start.
- Test fixtures use made-up people only; the deploy check runs in a temporary repo, never the real volume (lesson from Q3).
- **Gate before real memories (end of Phase 1 → Phase 2):** decide either encryption at rest for the memory volume or moving JARVIS to its own server first. The memory is the most personal data Jarvis will hold, and the shared server's accepted risk S2 (a compromised neighbour) matters more for it than for the ledger.

## 10. Testing

pytest, with a fresh temporary git repo per test and the real Postgres from Phase 0 for people, households and audit rows.

| Test | How |
|---|---|
| **M1** | `write` a preference → `read` and `retrieve_context` return the same ID, content, source, tag |
| **M2** | `update` → a new commit exists; `history` returns both versions; the old wording is recoverable with `git show` |
| **M3** | `write` INFERRED → `assert_actionable` raises; `confirm` by an agent is refused; `confirm` by Manj → passes |
| **M4** | The worked example in §6, with each exclusion checked by name |
| Format | Write → read back round-trips exactly; a damaged line stops the load and names file and line |
| IDs | Allocation after archive never reuses an ID; 4 threads × 20 writes → 80 distinct IDs, 80 commits |
| Tag rules | Every row of §7, allowed and refused, against an independent copy of the table |
| Visibility | The §5 matrix: each visibility × subject / recorder / shared-with / other family member |
| Audit | One row per change, no content in any row; app role cannot insert directly; refused calls audited REJECTED |
| Recovery | Simulated crash between commit and audit row → startup reconciles; audit failure → commit undone |
| Safety | Engine refuses to start with a git remote configured; refuses unknown subjects, bad slugs, line breaks, SELECTED_GROUP |
| Migration | 0007 up/down keeps P0-6 green |

**Live check:** the `init` smoke step gains `SMOKE PASS M1…M4`, run in a temporary repo inside the container. `core` logs `memory ready: N entries, head <commit>` at start, or refuses to start if the memory does not load.

Speed target: `retrieve_context` under 50 ms with 5,000 entries (tested).

## 11. Deployment changes

- Server image: add `git`.
- `compose.yaml`: new volume `memory`, mounted read-write at `/var/lib/jarvis/memory` in `core` only; `core` also mounts `backups` at `/backups/memory` for the daily bundle.
- `init` runs migration 0007 and the memory smoke checks.
- Runbook (`server/deploy/README.md`): restoring memory from a bundle; what to do if memory refuses to load.

## 12. Approach considered

| Option | For | Against | Verdict |
|---|---|---|---|
| **A. Markdown is the truth, loaded into memory** | One source of truth; readable and diffable; history for free in git; tiny data | Search is a scan (fine at this size) | **Chosen** |
| B. Markdown is the truth, plus a Postgres search index | Faster search at large size; SQL filters | Two copies that can drift; rebuild logic; no need at this size | Later, if memory passes ~50,000 entries |
| C. Postgres is the truth, markdown exported | Transactions; one database | Markdown becomes a view nobody edits; loses "readable memory you own" from v2 | Rejected |

## 13. Plan (after approval)

| # | Task |
|---|---|
| T1.1 | ADR-010; interface, types and `memory-entry.schema.json` with examples and tests |
| T1.2 | Entry parser and writer (strict, round-trip tests) |
| T1.3 | Git store: create repo, lock, commit, reload on HEAD change, history, no-remote check |
| T1.4 | Engine: write, read, update, confirm, archive, `add_person`; tag rules (§7) |
| T1.5 | Visibility (§5), `retrieve_context` and `search` (§6); M1–M4 |
| T1.6 | Migration 0007 (memory audit + S5); audit hook, undo on failure, startup reconciliation |
| T1.7 | Deploy: git in image, `memory` volume, startup check, smoke M1–M4, daily bundle, runbook |
| T1.8 | QA report and security review |

## 14. Decisions on the open questions (Manj, 2026-11-22)

| # | Decision |
|---|---|
| P1 | **Engine only.** No real personal memories until Phase 2; tests use fictional family members. |
| Privacy 1 | **No automatic parental access** to a child's PRIVATE entries. Share with SELECTED_PERSON or FAMILY_SHARED. The §5 table stands exactly. |
| Privacy 2 | **Dedicated server before real memories.** Encryption at rest stays a later hardening requirement; dedicated hosting does not replace it. |

Boundary for Phase 1: no embeddings, AI-based inference, automatic consolidation, external memory services or real personal data.
