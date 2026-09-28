# TEST-001 public fixture thesis v2

The original synthetic rehearsal used an older underwriting fixture and
recorded year-2 NOI 1,200,004.80. That snapshot remains a record of that run.
The new [v2 thesis fixture](../tests/fixtures/test001_public_thesis_v2.json)
records a distinct run from public underwriting `0d106d6` and costmodel
`518142e`. Its saved engine inputs are bundled in
`src/plat_agent/lifecycle/fixtures/test001_underwriting_inputs.json` and
tested for exact parity with the public underwriting test fixture.
The runtime refuses a modified engine or fixture source tree at that SHA.

The engine now returns year-2 unlevered NOI **1,039,354.80**. With purchase
price 13,500,000 and explicitly priced interior plus roof capex 1,758,150,
the harness yield on cost is **6.8118009064%**. The yield formula and 7–8%
target band are unchanged. The engine's 5.5% exit cap still triggers the
harness numeric reasonability withhold; the recorded thesis has no bid.
This is synthetic evidence, not a market review or a live deal result.

The [v3 thesis fixture](../tests/fixtures/test001_public_thesis_v3.json)
records a new run against underwriting `10a88ed393e6d6611c8c710b5e15ef64e128af6b`.
That commit packages the `plat.underwriting.mcp/1` adapter and fixes CI and a
mixed T12 subtotal. The saved TEST-001 inputs and deterministic year-2 NOI
are unchanged. The v2 fixture remains frozen as evidence of the prior run.
