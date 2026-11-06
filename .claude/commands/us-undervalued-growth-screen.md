---
description: "Run the Claude Code-native US undervalued-growth/GARP discovery pipeline, underwrite every selected name, and publish an audited scoped ranking."
argument-hint: "[optional config JSON path or explicit scope override]"
---

# US Undervalued Growth Screen

Use the repository's local direct-FMP pipeline. Do not call bulk FMP MCP tools and do not paste raw provider JSON into the conversation.

## Arguments

```text
$ARGUMENTS
```

Interpretation:

- Empty: use `.claude/skills/us-undervalued-growth-screener/assets/claude-code-config.example.json`.
- Existing JSON path: pass it as `--config`.
- Natural-language scope override: change the default USD 500M–20B range only when the user explicitly supplies another range; first write a temporary config recording the requested range.

## Execution

1. Confirm that `FMP_API_KEY` exists without printing its value.
2. Run the runtime preflight, including `run_pipeline.py --version`. Every helper must report skill 3.6.1, schema 3, contract 3.5, and fingerprint `ug-v3.6.1-claude-code-direct-fmp-20260830`.
3. Run:

```bash
python3 .claude/skills/us-undervalued-growth-screener/scripts/run_pipeline.py \
  --config .claude/skills/us-undervalued-growth-screener/assets/claude-code-config.example.json \
  --output-dir reports/us-undervalued-growth-screener
```

Substitute an explicit JSON argument path only when supplied.

4. Parse the single compact JSON object printed by the runner. Read only:

```text
<run>/run-summary.json
<run>/NEXT_ACTION.json
<run>/audit/listing-enumeration-audit.json
<run>/audit/provider-prefilter-audit.json
<run>/audit/broad-screen-audit.json
<run>/candidate-packets/*.fmp-packet.json
```

Do not read the `provider/` or `provider-raw/` trees into model context. Open one raw provider artifact only to investigate a named discrepancy.

5. Follow `NEXT_ACTION.json` without asking for confirmation. Complete primary-source underwriting for every `selected_symbol` using accession-specific SEC filings and official company IR. Provider packets are discovery evidence, not primary-source verification.
6. Save every selected symbol as a verified candidate record, including `review_required`, `screened_out`, and `excluded` outcomes.
7. Run the existing final sequence:

```text
manage_run_state.py assemble
evaluate_candidates.py --strict --require-final
prepublish_audit.py
bundle_run_artifacts.py
```

8. Publish only after strict evaluation and prepublication audit both exit 0.
9. Never ask the user to send “Continue.” Exit code 2 means repair or continue within the same run. When the environment genuinely cannot finish, emit and attach a diagnostic/resume bundle rather than claiming a final ranking.
10. State the scope exactly. A provider-prefilter result is a scoped ranking, not a market-wide conclusion.

## Output

Return:

- concise ranking and status summary,
- scope, listing-enumeration, estimate-coverage, and liquidity-coverage statements,
- key reasons for eligible, conditional, review, screened-out, and excluded outcomes,
- links to the Markdown report, JSON result, and self-contained audit ZIP.
