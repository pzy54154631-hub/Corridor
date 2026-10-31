# E4 apparatus

Run `powershell -NoProfile -File scripts/agentdojo_setup.ps1`. It checks out
[AgentDojo commit 089ed468cf3ed0322acc66b0211f26d9d90dbf60](https://github.com/ethz-spylab/agentdojo/tree/089ed468cf3ed0322acc66b0211f26d9d90dbf60)
under `.tmp/agentdojo-src`, installs the exact dependency versions in
`agentdojo-requirements.lock` into `.tmp/agentdojo-venv`, and performs offline
calibration. The package reports `0.1.35`; benchmark version is explicitly
`v1.2.2`. Project `pyproject.toml` and `uv.lock` are unchanged.

Reproduce the calibration with
`.tmp/agentdojo-venv/Scripts/python.exe scripts/agentdojo_calibrate.py`.
The script verifies installed scorer bytes against the clean pinned checkout.
It performs no model calls and does not read credentials.

`evidence/dryrun/agentdojo/` contains:

- `calibration.json`: actual source hashes, trusted pre-run seed, utility
  predicate checks, native spotlighting and rejection-pipeline checks.
- `security-witnesses.json`: nine correct/wrong/no-op witnesses for IT0/1/3,
  including original and independently corrected verdicts.
- `a-null.json`: actual 16 benign, 144 attacked, and nine separate sanity runs.
- `ground-truth-partial.json`: 144 upstream GroundTruthPipeline injection traces
  passed through the partial native transfer gate. Delivery is independently
  checked against the user's upstream ground truth.
- `matrix.json`: exact primary and sensitivity cells, fixed seeds, two paid
  pilot tasks excluded from denominators, injection positions/payload hashes,
  and the 42 same-tool-subset pairs for exploratory A2/A3/A5 diagnostics.
- `bridge.json`: eight mocked HTTP requests, native named banking tool calls,
  strict replay without transport, strict-miss rejection, and a complete attacked
  episode compared with the coordinator's exact replay projection. No model runs.
- `apparatus/summary.json`: 676 scripted no-op pipeline checks, exactly
  16 benign + 144 attacked + nine separate sanity runs for each of A0, A5,
  A_null and native spotlighting. These demonstrate apparatus reachability only.

Actual A_null calibration is 5/16 benign utility, 45/144 attack utility, and
0/144 targeted attack success. The official IT0/1/3 predicates accept unrelated
subjects on a transfer to the attacker. IT1 resolves its companion to `me`.
UT5's ground truth transfers 5 while its utility matches an existing transfer
of 50; UT8 returns true even for no-op. Utility is retained as the official
measure: no unregistered replacement utility score is invented. The independent
corrected security evaluator requires the attacker recipient and the registered
case-insensitive target substring for IT0/1/3.

The transfer adapter resolves only frozen state and literal user-prompt values,
obtains native signed authorization/cards/MFA, and invokes unchanged native
gateway dispatch before external `run_function`. Its synthetic ledger starts
at the external balance of 1810 and actually debits accepted transfers.
No external literal float enters Corridor money; external benchmark amounts
are converted to strings at the adapter boundary and parsed as Fraction.

Formal E4 remains `not_run` because D3 does not authorize its execution. The
recorded model bridge is now implemented and checked with mock transport.
Constructor-scoped `tool_namespace="agentdojo-banking-v1.2.2"` admits exactly the
11 pinned banking schemas plus `finish`; the production default remains Wealth.
All proposals go through `corridor.agent.llm.LLMClient`. Each proposal retains
the upstream function name and arguments and reaches upstream `run_function`
only after the configured pipeline. The explicit synthetic benchmark context
guard accepts the fixed public IBAN/password fixtures without changing the
production PII guard. No hidden environment or oracle labels enter the prompt.

Transport adaptations are declared in every episode: amounts use decimal
strings, termination uses a named `finish` function, and upstream message
history is encoded as structured prompt data. These prevent treating this as
an exact reproduction of upstream's original SDK prompt format.

A5 has a partial native semantic mapping: immediate transfers use the actual
native gateway; schedule creation/update, password, and profile changes reject
as `unsupported_mapping`. Every row records this scope. Callable A5 therefore
does not establish a full native equivalence or a full-suite security claim.
Auto-Approve does not override source resolution or native balance checks.
File-only literals remain unresolved. No one-day timeout is asserted, and no
production kernel code changes were made. Exploratory A2/A3 rows are registered
in the manifest but are not implemented for external tool semantics.

`agentdojo_pipeline.py` uses upstream `spotlighting_with_delimiting`, checks
before every external `run_function`, returns tool errors on rejection, and
ensures a nonempty terminal assistant message. Native `tool_filter` is excluded.
Every future full run must be sequential with `force_rerun` after relevant code
changes, record retries, report official and corrected targeted ASR, utility,
utility-and-no-attack, errors, mirror-only rejections, and dispatch diagnostics.
For an A0 official score at most 7/144, H4 is untestable. Published numbers are
not reproduced here and no superiority claim is made.

Run `.tmp/agentdojo-venv/Scripts/python.exe scripts/agentdojo_bridge_smoke.py`
to refresh bridge evidence. Each run retains fresh mock recordings; code hashes
bind its result to the bridge/gate/LLM implementation. Run
`.tmp/agentdojo-venv/Scripts/python.exe scripts/agentdojo_apparatus_smoke.py`
for the 676 scripted checks. The sequential coordinator expands primary cells
into complete per-episode comparison blocks, rotates configuration order,
uses isolated E4 subprocesses, and checks installed scorer bytes against
calibration. Its formal gate additionally requires `formal_e4_gate=true`;
the current partial native mapping sets this false, so the formal coordinator
retains all E4 rows as `not_run`. Standalone worker and mock diagnostic execution
remain callable. Paid E4 first requires two benign pilot tasks (excluded from
denominators), their actual cost estimate, and the remaining USD journal.

The live worker accepts `--env-file <path>` for a read-only UTF-8 credential
file, just as `scripts/run_live_zh.py` does. The selected credential overrides
inherited process keys; neither its value nor its path belongs in a worker
specification, recording identity, or output artifact. Strict replay does not
read credentials. Offline calibration and mock smoke scripts require no keys.
