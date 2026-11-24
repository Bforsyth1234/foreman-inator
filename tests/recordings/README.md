# Recorded tickets (synthetic)

Data for `tests/test_replay.py` (todo #28). Nothing here came from a real run, a real
repo, a real issue or a real model: every issue, prompt, model answer, tool output and
check result is made-up text written by `make_cases()` in `tests/test_replay.py`. There
are no tokens or keys of any kind (none are needed: no model or GitHub call is replayed).

How they were made: `make_cases()` drives the real code that writes recordings and spans,
in the order the worker uses it:

- the real `tracing.Recorder` (fde-kit's `RunRecorder` underneath) writes the attempt and
  review recordings;
- the real span helpers (`ticket_span`, `route_span`, `attempt_span`, `record_llm_call`,
  `record_tool_call`, `verify_span`, `review_span`, `review_check_span`, `github_span`,
  `add_kickback`) inside `capture_spans(traces_dir)` write the span files through the
  real `TraceFileProcessor`; ticket 2 is routed by the real `rules.route` (a `tier:2`
  override, so no classifier call);
- the store rows are written to a real SQLite `Store` (fixed clock), moved through the
  real `lifecycle.apply`, and exported with `replay.rows_to_json(replay.ticket_rows(...))`.

Regenerate (after a deliberate format change) with
`uv run python tests/test_replay.py --regenerate`;
`test_checked_in_cases_match_the_generator` fails when these files and the generator
drift apart (ids and timestamps aside).

Layout (the worker's own layout under `~/.foreman-inator/`):

| Path | What |
| --- | --- |
| `recordings/<ticket>/<attempt>.jsonl` | an attempt's recording |
| `recordings/<ticket>/<attempt>.review.jsonl` | the AI review of that attempt |
| `traces/<ticket>/<trace_id>.jsonl` | the ticket's span file, one `ReadableSpan.to_json` per line |
| `rows/<ticket>.json` | the ticket's store rows (ticket, attempts, kickbacks, reviews, ladder, uncounted attempts) |

The recordings are checked in as plain `.jsonl` (gunzipped), so they can be read in review
and are scanned by gitleaks; the recorder itself writes `.jsonl.gz`, and the tests gzip a
copy back into that form before replaying it.

The cases:

1. Ticket 1, issue 7: classified `small` → tier 1. Attempt 1 fails its checks; attempt 2
   (same-tier retry) passes and the AI reviewer requests changes on a hard finding (a new
   function no test runs, so no model call); attempt 3 (`ai_kickback`) is approved by the
   reviewer, then kicked back by the user as `wrong`, with failure step `wrong_edit` stored
   at the index of its `edit` tool row; attempt 4 escalates to tier 2, is approved and
   merged.
2. Ticket 2, issue 8: a `tier:2` label; the issue, the model's answer, a tool output and the
   review note carry markup and a script tag, and one tool output is longer than the
   timeline's clip limit. One attempt, approved and merged. Used for the viewer's escaping
   and clipping tests.
