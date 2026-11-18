---
name: safety-scoring
description: Read and tune the scored safety report per token, apply the thresholds table, and mark blind checks blind instead of passing them.
bots:
  - bot-05-rug
gates:
  - G-2
required_tools:
  - safety.score_report
required_data:
  - solana_rpc
permissions:
  max_side_effect: read
  network: true
  filesystem: none
fixtures:
  - tests/fixtures/http/authority_ok.json
---

# Safety Scoring

## L1 - Summary

RUG scores every candidate with `safety.score_report` before it can proceed: authorities, holders, liquidity, and the bundler/insider cluster each report pass, fail, or blind with reasons. Blind is not a pass; a blind check routes to RISK judgment, never to silent approval. RISK consumes the verdict through `risk.check_limits`, not by re-running the checks.

```
all checks pass -> scored report attaches to the ticket -> RISK rules
any check fails -> candidate blocked with reasons
any check blind -> marked blind, routed to RISK; never treated as pass
```

## L2 - Method

1. S1 - Run the report: call `safety.score_report` for the candidate mint. Read every per-check outcome plus its reason; the top-line score never overrides a failing check.
2. S2 - Apply the thresholds table: compare each component against the pinned thresholds (authority flags, top-holder concentration, liquidity floor multiple, bundler cluster share). The bundler check warns at 10 percent of supply in a flagged cluster and fails at 25 percent. A same-slot cluster needs 3 buyers, so a lone dev buy is not a bundle. The funding hop looks back 600 seconds. The scan reads at most 6 earliest transactions, 24 RPC calls, and 12 seconds. Thresholds change only through a reviewed config change, not per-ticket judgment.
3. S3 - Handle blind honestly: when a check cannot run (missing data, degraded RPC, a launch window past the signature cap, or an exhausted bundler call or time budget), mark it blind with the cause and route the ticket to RISK. Bundler blind is a required blind and forces KILL. Blind plus urgency is still blind.
4. S4 - Attach the verdict: store the full report with the ticket so RISK, WHALE, and the journal all read the same reasons. Summaries without reasons are not verdicts.
5. S5 - Tune by evidence: threshold changes require replay evidence across the fixture sets showing fewer misses without new false safety. Tune the table, never the individual ticket.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| Blind treated as pass | Score-first reading of the report | Re-grade; blind always routes to RISK |
| Threshold edited per ticket | Pressure to clear a favorite | Revert; thresholds change only by reviewed config |
| Missing reasons | Truncated or summarized report | Re-run the full report; reasons are the verdict |
| Stale data scored as fresh | Degraded RPC behind the checks | Mark the affected checks blind with the data age |

## References

- Tool: `safety.score_report` (scored report with per-check reasons; `bundler_cluster` is a live RPC reading, not a blind-only row)
- Reader: `onchain.bundler_cluster` (earliest transactions, same-slot buyers, one-hop funding, deployer overlap)
- Fixture: `tests/fixtures/http/authority_ok.json` (recorded authority response shape)
- Gate G-2: scored verdicts feed approval-gated flow
- Companion skills: `risk-audit` (authority checklist), `holder-and-flow-analysis` (concentration detail)
