# TEST-001 public fixture thesis v2

The original synthetic rehearsal used an older underwriting fixture and
recorded year-2 NOI 1,200,004.80. That snapshot remains a record of that run.
The new [v2 thesis fixture](../tests/fixtures/test001_public_thesis_v2.json)
records a distinct run from public underwriting `0d106d6` and costmodel
`518142e`. Its saved engine inputs are bundled in
`src/plat_agent/lifecycle/fixtures/test001_underwriting_inputs.json` and
tested for exact parity with the public underwriting test fixture.

The engine now returns year-2 unlevered NOI **1,039,354.80**. With purchase
price 13,500,000 and explicitly priced interior plus roof capex 1,758,150,
the harness yield on cost is **6.8118009064%**. The yield formula and 7–8%
target band are unchanged. The engine's 5.5% exit cap still triggers the
harness numeric reasonability withhold; the recorded thesis has no bid.
This is synthetic evidence, not a market review or a live deal result.
