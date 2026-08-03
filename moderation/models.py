"""No models live here.

The things a moderator acts on — `Submission`, `SubmissionMessage`,
`ModerationAction` — belong to the `submissions` app, because they are created
by the submission flow and only later read by a moderator. This app is the
*interface*: the queue, the decisions, and the mail that follows them.

Keeping it a separate app rather than folding it into `submissions` matters for
one practical reason: every view in here is behind `moderator_required`, and a
whole app under one access rule is far easier to audit than a mixed one.
"""
