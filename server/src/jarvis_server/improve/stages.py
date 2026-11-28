"""The orchestrator: one stage at a time from the team's own queue.

It holds the keys and never runs an agent tool. Each paid stage is one round trip to the sandbox,
which runs the agents and hands back text or a patch. The orchestrator checks every patch with the
pre-push check (gate.py) in its own clone, and pushes only after the Reviewer passes it. It never
merges: Manj merges on GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from jarvis_server.improve import gate, inbox, plan, review_loop, tickets
from jarvis_server.improve.ports import (
    GitHub,
    Job,
    Repo,
    Sandbox,
    StageRequest,
    StageResult,
    Store,
    Ticket,
)
from jarvis_server.improve.tickets import State

STAGE_CAP: Mapping[str, float] = {"design": 3.0, "tests": 1.5, "build": 3.0, "review": 1.5}
WATCH_EVERY_S = 300
LEASE_S = 120
_VERDICT = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b", re.MULTILINE)


class Refused(Exception):
    """Manj's command doesn't fit the ticket as it stands."""


class Orchestrator:
    def __init__(
        self,
        store: Store,
        sandbox: Sandbox,
        repo: Repo,
        github: GitHub,
        *,
        owner: str,
        now: Callable[[], datetime],
    ) -> None:
        self.store, self.sandbox, self.repo, self.github = store, sandbox, repo, github
        self.owner = owner
        self.now = now

    # ---- Manj's commands -------------------------------------------------------------------

    def open_ticket(self, words: str, evidence: Mapping[str, Any]) -> int:
        tid = self.store.open_ticket(words, tickets.slug(words), evidence)
        self.store.enqueue(tid, "design")
        return tid

    def approve(self, ticket_id: int, fingerprint_prefix: str, *, actor: str) -> None:
        t = self.store.get(ticket_id)
        fp = t.design_fingerprint or ""
        if t.state is not State.DESIGN_READY:
            raise Refused(f"ticket {ticket_id} has no design waiting for a yes")
        if len(fingerprint_prefix) < 8 or not fp.startswith(fingerprint_prefix):
            raise Refused("that fingerprint doesn't match the design as it stands now")
        self.store.update(ticket_id, approved_fingerprint=fp)
        self.store.move(ticket_id, State.APPROVED, actor, "design approved")
        self.store.resolve_inbox(ticket_id, inbox.Kind.DECISION)
        self.store.enqueue(ticket_id, "tests")

    def change(self, ticket_id: int, words: str, *, actor: str) -> None:
        if self.store.get(ticket_id).state is not State.DESIGN_READY:
            raise Refused(f"ticket {ticket_id} has no design waiting for a yes")
        self.store.update(
            ticket_id, approved_fingerprint=None, feedback=(f"Manj asked for a change: {words}",)
        )
        self.store.move(ticket_id, State.DESIGNING, actor, "change asked")
        self.store.resolve_inbox(ticket_id, inbox.Kind.DECISION)
        self.store.enqueue(ticket_id, "design")

    def resume(self, ticket_id: int, *, actor: str) -> None:
        t = self.store.get(ticket_id)
        if t.state is not State.PAUSED or t.paused_from is None:
            raise Refused(f"ticket {ticket_id} isn't paused")
        stage = t.paused_stage
        if stage is not None:  # paused at the cap: allow this one stage more
            cap = max(t.cap_usd, self.store.spent(ticket_id) + STAGE_CAP[stage])
            self.store.update(ticket_id, cap_usd=cap, paused_stage=None)
        self.store.move(ticket_id, t.paused_from, actor, "resumed")
        self.store.resolve_inbox(ticket_id, inbox.Kind.PAUSED)
        if stage is not None:
            self.store.enqueue(ticket_id, stage)

    def stop(self, ticket_id: int, *, actor: str) -> None:
        self.store.move(ticket_id, State.STOPPED, actor, "Manj said stop")
        for kind in inbox.Kind:
            self.store.resolve_inbox(ticket_id, kind)

    def complaint_back(self, ticket_id: int, words: str, *, actor: str) -> None:
        self.store.update(ticket_id, feedback=(f"The complaint came back: {words}",))
        self.store.move(ticket_id, State.DESIGNING, actor, "complaint came back")
        self.store.enqueue(ticket_id, "design")

    # ---- the queue -------------------------------------------------------------------------

    def tick(self, worker: str) -> bool:
        """Run one queued job. Returns False when the queue is empty."""
        job = self.store.claim(worker, LEASE_S)
        if job is None:
            return False
        try:
            self._run(job)
        except Exception as e:
            self.store.fail(job.id, f"{type(e).__name__}: {e}")
            raise
        self.store.finish(job.id)
        return True

    def sweep(self) -> None:
        now = self.now()
        for t in self.store.in_state(State.DESIGN_READY):
            if t.design_ready_at and tickets.design_expired(t.design_ready_at, now=now):
                self._stop(t.id, "the design waited 14 days without an answer")
        for t in self.store.in_state(State.WATCHING):
            if t.watch_started_at and tickets.watch_over(t.watch_started_at, now=now):
                self.store.move(t.id, State.DONE, "jarvis", "seven days without a repeat")

    def _run(self, job: Job) -> None:
        t = self.store.get(job.ticket_id)
        if t.state in tickets.TERMINAL or t.state is State.PAUSED:
            return  # stopped or paused since it was queued
        handler = {
            "design": self._design,
            "tests": self._tests,
            "build": self._build,
            "review": self._review,
            "pr_watch": self._pr_watch,
        }[job.stage]
        handler(job, t)

    # ---- stages ----------------------------------------------------------------------------

    def _design(self, job: Job, t: Ticket) -> None:
        if t.state is State.OPEN:
            self.store.move(t.id, State.DESIGNING, "jarvis", "design started")
        result = self._paid(job, t, design=None, patches=())
        if result is None:
            return
        for url in result.blocked_urls:
            self.store.add_inbox(inbox.blocked_site(t.id, url))
        if not result.ok or not result.design:
            self._record(t, "design", result, "FAILED")
            self._retry(t.id, "design", f"The design stage failed: {result.error or 'no design'}")
            return
        try:
            plan.parse(result.design)
            if gate.quotes(result.design, _evidence(t)):
                raise plan.BadPlan("the design quotes the ticket's private evidence")
        except plan.BadPlan as e:
            self._record(t, "design", result, "REFUSED", str(e))
            self._retry(t.id, "design", f"The design was refused: {e}")
            return
        self._record(t, "design", result, "PASSED")
        fp = plan.fingerprint(result.design)
        self.store.update(
            t.id,
            design=result.design,
            design_fingerprint=fp,
            design_ready_at=self.now(),
            feedback=(),
        )
        self.store.move(t.id, State.DESIGN_READY, "jarvis", "design written")
        self.store.add_inbox(inbox.design_ready(t.id, _title(result.design, t), fp))

    def _tests(self, job: Job, t: Ticket) -> None:
        p = _plan(t)
        base = self.repo.base_sha()
        self.store.update(t.id, base_sha=base)
        t = self.store.get(t.id)
        result = self._paid(job, t, design=t.design, patches=())
        if result is None:
            return
        if not result.ok:
            self._record(t, "tests", result, "FAILED")
            self._retry(t.id, "tests", f"The tests stage failed: {result.error}")
            return
        seen = self.repo.inspect(base, result.patch, p.tests)
        tests_only = gate.ApprovedDesign(files=frozenset(p.tests), packages=frozenset())
        problems = self._check(t, tests_only, seen, {}, ())
        problems += [f"{path} was not written" for path in p.tests if path not in seen.hashes]
        if problems:
            self._record(t, "tests", result, "REFUSED", "; ".join(problems))
            self._retry(
                t.id, "tests", "The pre-push check refused the tests: " + "; ".join(problems)
            )
            return
        self._record(t, "tests", result, "PASSED")
        self.store.update(
            t.id, tests_patch=result.patch, test_fingerprints=dict(seen.hashes), feedback=()
        )
        self.store.enqueue(t.id, "build")

    def _build(self, job: Job, t: Ticket) -> None:
        p = _plan(t)
        result = self._paid(job, t, design=t.design, patches=(t.tests_patch or b"",))
        if result is None:
            return
        if not result.ok:
            self._record(t, "build", result, "FAILED")
            self._retry(t.id, "build", f"The build stage failed: {result.error}")
            return
        seen = self.repo.inspect(_base(t), result.patch, tuple(t.test_fingerprints))
        problems = self._check(t, p.approved(), seen, t.test_fingerprints, (_message(t),))
        if problems:
            self._record(t, "build", result, "REFUSED", "; ".join(problems))
            self._retry(
                t.id, "build", "The pre-push check refused the patch: " + "; ".join(problems)
            )
            return
        self._record(t, "build", result, "PASSED")
        self.store.update(t.id, build_patch=result.patch)
        self.store.enqueue(t.id, "review")

    def _review(self, job: Job, t: Ticket) -> None:
        p = _plan(t)
        scope = (
            "The orchestrator's pre-push check passed: every changed file is in the plan, the"
            " fingerprinted tests are unchanged, and no unnamed package was added.",
            f"Done when: {p.done_when}",
        )
        result = self._paid(
            job, t, design=t.design, patches=(t.build_patch or b"",), feedback=scope
        )
        if result is None:
            return
        verdicts = _VERDICT.findall(result.text)
        if not result.ok or verdicts[-1:] != ["PASS"]:
            outcome = "FAILED" if not result.ok else "REFUSED"
            self._record(t, "review", result, outcome)
            why = result.error if not result.ok else result.text.strip()
            self._retry(t.id, "build", f"The Reviewer: {why}")
            return
        self._record(t, "review", result, "PASSED")
        branch = tickets.branch(t.id, t.slug)
        head = self.repo.push(_base(t), t.build_patch or b"", branch, _message(t))
        number, url = self.github.open_or_update_pr(branch, _title(t.design or "", t), _body(t, p))
        self.store.update(
            t.id, reviewed_sha=head, pr_number=number, pr_url=url, told_ready=False, feedback=()
        )
        if t.state is State.APPROVED:
            self.store.move(t.id, State.PR_OPEN, "jarvis", "review passed; PR open")
        self.store.enqueue(t.id, "pr_watch")

    def _pr_watch(self, job: Job, t: Ticket) -> None:
        if t.state is not State.PR_OPEN or t.pr_number is None:
            return
        pr = self.github.snapshot(t.pr_number)
        step = review_loop.next_step(
            pr,
            owner=self.owner,
            reviewed_sha=t.reviewed_sha or "",
            handled=t.handled_comments,
            told=t.told_ready,
        )
        a = review_loop.Action
        if step.action is a.TELL_READY:
            self.store.add_inbox(
                inbox.ready_to_merge(t.id, t.pr_url or "", _title(t.design or "", t))
            )
            self.store.update(t.id, told_ready=True)
            self.store.enqueue(t.id, "pr_watch", delay_s=WATCH_EVERY_S)
        elif step.action is a.WAIT:
            self.store.enqueue(t.id, "pr_watch", delay_s=WATCH_EVERY_S)
        elif step.action is a.REBUILD:
            self.store.update(t.id, handled_comments=t.handled_comments | set(step.comment_ids))
            self.store.resolve_inbox(t.id, inbox.Kind.READY_TO_MERGE)
            self.store.move(t.id, State.APPROVED, "jarvis", "changes asked on the PR")
            self._retry(t.id, "build", *step.feedback)
        elif step.action is a.WATCH:
            self.store.resolve_inbox(t.id, inbox.Kind.READY_TO_MERGE)
            self.store.update(t.id, watch_started_at=self.now())
            self.store.move(t.id, State.WATCHING, "jarvis", "merged by Manj")
        elif step.action is a.STOP:
            self._stop(t.id, "the pull request was closed without merging")
        else:
            self.store.move(t.id, State.PAUSED, "jarvis", "merged code differs from the review")
            self.store.add_inbox(
                inbox.warning(
                    t.id,
                    f"Ticket {t.id}: the code on the PR is not the commit the Reviewer passed",
                    (
                        f"Check the pull request: {t.pr_url}",
                        f"Then run: jarvis-improver resume {t.id}",
                    ),
                )
            )

    # ---- helpers ---------------------------------------------------------------------------

    def _paid(
        self,
        job: Job,
        t: Ticket,
        *,
        design: str | None,
        patches: tuple[bytes, ...],
        feedback: tuple[str, ...] = (),
    ) -> StageResult | None:
        """One sandbox round trip, unless it could take the ticket past its cap."""
        spent = self.store.spent(t.id)
        if spent + STAGE_CAP[job.stage] > t.cap_usd:
            self.store.update(t.id, paused_stage=job.stage)
            self.store.move(t.id, State.PAUSED, "jarvis", "spend cap")
            self.store.add_inbox(inbox.paused(t.id, spent=spent, cap=t.cap_usd))
            return None
        return self.sandbox.run(
            StageRequest(
                job_id=job.id,
                ticket_id=t.id,
                stage=job.stage,
                base_sha=t.base_sha or self.repo.base_sha(),
                words=t.words,
                evidence=t.evidence,
                design=design,
                patches=patches,
                feedback=(*t.feedback, *feedback),
            )
        )

    def _check(
        self,
        t: Ticket,
        design: gate.ApprovedDesign,
        seen: Any,
        fingerprints: Mapping[str, str],
        messages: tuple[str, ...],
    ) -> list[str]:
        patch = gate.Patch(seen.changed, seen.diff, messages, seen.lock_texts, seen.special)
        return gate.check(
            design,
            patch,
            fingerprints=fingerprints,
            current_hashes=seen.hashes,
            evidence=_evidence(t),
        )

    def _record(
        self, t: Ticket, stage: str, r: StageResult, outcome: str, error: str | None = None
    ) -> None:
        self.store.record_stage(
            t.id,
            stage,
            cost_usd=r.cost_usd,
            turns=r.turns,
            outcome=outcome,
            error=error or r.error,
        )

    def _retry(self, ticket_id: int, stage: str, *feedback: str) -> None:
        if self.store.use_round(ticket_id) >= tickets.ROUNDS:
            self._stop(ticket_id, f"the retry budget of {tickets.ROUNDS} rounds is spent")
            return
        self.store.update(ticket_id, feedback=tuple(feedback))
        self.store.enqueue(ticket_id, stage)

    def _stop(self, ticket_id: int, why: str) -> None:
        self.store.move(ticket_id, State.STOPPED, "jarvis", why)
        for kind in inbox.Kind:
            self.store.resolve_inbox(ticket_id, kind)
        self.store.add_inbox(inbox.stopped(ticket_id, why))


def _plan(t: Ticket) -> plan.Plan:
    if t.design is None or t.approved_fingerprint != plan.fingerprint(t.design):
        raise RuntimeError(f"ticket {t.id} has no approved design")  # the DB trigger agrees
    return plan.parse(t.design)


def _base(t: Ticket) -> str:
    if t.base_sha is None:
        raise RuntimeError(f"ticket {t.id} has no base commit")
    return t.base_sha


def _evidence(t: Ticket) -> list[str]:
    out = [t.words]

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, list | tuple):
            for x in v:
                walk(x)

    walk(t.evidence)
    return out


def _title(design: str, t: Ticket) -> str:
    for line in design.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return f"Ticket {t.id}"


def _message(t: Ticket) -> str:
    return f"improve: {_title(t.design or '', t)} (ticket {t.id})"


def _body(t: Ticket, p: plan.Plan) -> str:
    prose = plan.strip_block(t.design or "")
    packages = ", ".join(f"{x.name} ({x.licence})" for x in p.packages) or "none"
    files = "\n".join(f"- `{f}`" for f in (*p.files, *p.tests))
    return (
        f"Ticket {t.id}, built by JARVIS's self-improvement team from the design you approved"
        f" (fingerprint {(t.approved_fingerprint or '')[:8]}).\n\n{prose}\n\n"
        f"**Done when:** {p.done_when}\n\n**Files:**\n{files}\n\n**New packages:** {packages}\n\n"
        "JARVIS never merges. Approve and merge here, or leave a review comment and it goes back"
        " to the Builder.\n"
    )
