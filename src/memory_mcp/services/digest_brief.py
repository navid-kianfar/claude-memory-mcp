"""The brief handed to the agent running a digest.

A digest is the one operation in this server that rewrites knowledge the user
has already approved once. The store can hold the line on process - nothing is
written without an explicit per-op approval, nothing is ever hard-deleted, every
merge is checked clause by clause - but it cannot hold the line on judgment. It
does not know that two rules about "the snapshot" are the same rule, or that a
path a rule names moved last month rather than being deleted.

So the brief is where the judgment is specified: what to look for, what to
propose, what to refuse to propose, and where to stop and ask. It is returned by
`memory_digest` and is meant to be read as instructions, not as context.
"""

_HEADER = (
    "MEMORY DIGEST for project '{project}': {total} memories in force "
    "({rules} of them rules), {findings} signals found, ~{tokens} tokens of "
    "corpus. Nothing has been changed. You are reviewing, not editing."
)

_NO_FINDINGS = (
    "MEMORY DIGEST for project '{project}': {total} memories in force, no "
    "signals. The corpus is clean - say so and stop. Do not manufacture "
    "cleanups to look useful; a digest that changes nothing is a good digest."
)

_STEPS = (
    "1. READ the corpus below in full before proposing anything. The signals "
    "are evidence, not conclusions - they were computed from embeddings, "
    "timestamps and the filesystem, and none of them understands what a rule "
    "means.",

    "2. VERIFY every signal that makes a claim about the code. A "
    "`stale_reference` says a path is not in the project folder now; check "
    "whether it MOVED before you propose archiving the memory that names it - "
    "search the repo, read the file. A rule whose path is wrong needs the path "
    "fixed (rewrite), not the rule deleted.",

    "3. Then propose operations with memory_digest_propose. One op per change, "
    "each with a `reason` a person can judge:\n"
    "   - `rewrite`  same rule, said clearly. Use it for a rule that is vague, "
    "contradicts itself, or names a path that changed.\n"
    "   - `merge`    several memories unified into one. This is the point of a "
    "digest: two rules that say the same thing in different words are both in "
    "force, and the agent reading them last effectively wins.\n"
    "   - `split`    one memory that holds two separate rules becomes two, so "
    "each can be edited, revoked or followed on its own.\n"
    "   - `recategorize` a rule stored as a decision becomes a rule (that is "
    "how it starts being enforced), and a rule that is really a one-off record "
    "becomes a decision (that is how it stops costing rule-block tokens every "
    "session).\n"
    "   - `retag` / `reprioritize`  metadata only.\n"
    "   - `archive`  the thing it describes is gone from the system. Archive is "
    "the strongest op there is: the row and its history survive, and "
    "memory_digest_revert brings it back.\n"
    "   - `keep`     record an explicit decision to leave something alone. Use "
    "it for a signal you investigated and rejected, so the next digest does not "
    "re-litigate it.",

    "4. NEVER LOSE A BUSINESS RULE. A merge or rewrite must carry every "
    "normative clause of every source into the replacement - the constraint, "
    "the exception, the scope, the 'why', the numbers. `memory_digest_propose` "
    "checks this clause by clause and returns any clause it cannot find in your "
    "replacement as `unmatched`. Treat an unmatched clause as a bug in your "
    "proposal: fold it back in and re-propose. Only when a clause is genuinely "
    "obsolete may it be dropped, and then you must say so to the user in words, "
    "quoting the clause, and let them decide.",

    "5. When two memories contradict each other, do NOT pick a winner on your "
    "own - only the user knows which one is current. Propose nothing for that "
    "pair, show both texts, and ASK. The same goes for any rule whose intent "
    "you cannot reconstruct: propose `keep` and ask.",

    "6. SHOW THE USER THE DIFF that memory_digest_propose returns, grouped by "
    "operation, with the reason for each. Say plainly what is being unified, "
    "what is being archived and what the corpus will cost afterwards. Then "
    "WAIT. Do not call memory_digest_apply until the user has told you which "
    "operations they approve.",

    "7. Apply with memory_digest_apply(digest_id, approve=[op ids]) - only the "
    "ops they actually approved. If they approved everything, approve_all=True "
    "is allowed, but an op with unmatched clauses still needs its own id listed "
    "in `approve`: nobody gets to wave through a change that drops a clause. "
    "Tell the user afterwards that memory_digest_revert(digest_id) undoes the "
    "whole thing exactly.",
)

_SCOPE_NOTE = (
    "Scope: this digest covers only project '{project}'. Never pull a memory "
    "from another project into a proposal, and never propose a change to one."
)


def digest_brief(
    project: str, total: int, rules: int, findings: int, tokens: int,
) -> str:
    """Instructions for the agent that just ran the analysis."""
    if not findings:
        return "\n".join([
            _NO_FINDINGS.format(project=project, total=total),
            _SCOPE_NOTE.format(project=project),
        ])
    header = _HEADER.format(
        project=project, total=total, rules=rules, findings=findings, tokens=tokens,
    )
    return "\n".join([header, *_STEPS, _SCOPE_NOTE.format(project=project)])


#: Returned with a proposal that has unmatched clauses, so the warning travels
#: with the diff rather than living only in this module.
UNMATCHED_WARNING = (
    "{count} operation(s) drop a clause that no part of the replacement "
    "accounts for. Each one is listed with the dropped text verbatim under "
    "`coverage.unmatched`. Either fold the clause back in and re-propose, or "
    "quote it to the user and let them decide - approve_all will refuse these."
)
