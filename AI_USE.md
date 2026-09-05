# Transparent AI use

AI was used as an assistant for schema interpretation, edge-case review, formula checking and clearer operational writing. It was not used to invent records, infer missing exception reasons, label merchants manually or generate numerical results.

The published metrics are produced deterministically by the Python and SQL logic. The Excel workbook was also checked for formula errors and visually reviewed sheet by sheet.

Examples of responsible prompts for extending the work:

- “Review this metric definition for attribution ambiguity. Do not calculate or invent results.”
- “List timestamp sequence checks that should run before stage-duration KPIs are trusted.”
- “Rewrite this finding for a Key Account Manager while preserving every number and limitation.”
- “Given these observed fields, separate supported conclusions from hypotheses requiring carrier or scan data.”

This keeps AI in the role of analyst accelerator and quality-control partner, while the data pipeline remains reproducible.

## Analyst contribution

The analyst—not the AI—made the key interpretive challenge that improved the project: a lane ranked first by percentage may not create the greatest customer impact. The analyst identified SP order concentration, separated the healthy SP → SP benchmark from weaker SP outbound lanes, questioned the instability of low-volume percentages, and asked for the approval-to-carrier handoff definition to be made explicit.

Those questions led to a deterministic volume-band test, an excess-late-order measure, and an action table with named owners and success measures. AI assisted with implementation and quality checks; the published conclusions remain traceable to source data and stated formulas.
