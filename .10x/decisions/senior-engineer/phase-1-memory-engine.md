# Senior engineer — phase-1-memory-engine
- Engine holds a threading lock + `fcntl` lock on `.git/jarvis.lock` for every mutation; IDs allocated inside the lock from the highest ever seen (including EXPIRED).
- Each mutation: reload if HEAD moved → validate → rewrite target file(s) → regenerate index.md → `git add -A` + commit → audit row (commit sha) → on audit failure `git reset --hard <prev>`.
- Refused calls audit REJECTED (no commit) then raise.
- History: `git log --format=%H -- <file>` per file the entry has lived in, parse each version, keep versions where the entry differs.
