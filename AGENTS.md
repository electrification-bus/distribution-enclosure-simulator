# Emitter — Agent Rules

Rules in this file apply to all AI coding agents working in this repository.

## No AI Attribution in Commits

Do **not** attribute work to Copilot, Claude, or any AI agent in commit messages. Commits represent human direction and decision-making; AI assists in implementation but does not co-author.

**Rule:**

- Never include `Co-authored-by: Copilot` or any AI co-author trailer in commit messages.
- Never mention AI tools or attribution in commit messages.
- Commits belong to the human author directing the work.

This rule takes precedence over any default tool behavior that would add AI attribution.

## Sign Frames — this package publishes a panel's view

This emitter models a distribution enclosure, and an enclosure is an **interface** to the devices around it. Its reading of a device is therefore the mirror
image of that device's reading of itself: what the device calls "out of me", the enclosure calls "into me". Two frames coexist on purpose and disagree about
the same instant.

| | the device's own view | the enclosure's view (what this publishes) |
|---|---|---|
| PV | positive = generating | **negative** while generating into the enclosure |
| BESS | positive = discharging | `power-flows/battery`: **positive** while charging, out of the enclosure. The hosted BESS's `meter/active-power` matches it only on a span-variant panel on firmware before release 202639; otherwise it is the battery's own frame |
| grid | positive = supplying the home | **positive** while exporting, out of the enclosure |
| circuit | positive = consuming | **negative** while consuming, out of the busbar |
| `site` | — not a device at the interface | positive = consuming; no mirror to take |

`site` is the exception because no device sits on the other side of it to mirror — which is why it was the one `power-flows` property already correct while
the other three were inverted.

A standalone BESS, PV or EVSE elsewhere on the same bus publishes its **own** meter in its **own** frame. That device is not this device, and the two
describing the same battery with opposite signs at the same instant is correct.

**Rules:**

- **Never "reconcile" the two frames.** An enclosure exporting publishes `lugs-upstream/meter/active-power` negative and `power-flows/grid` positive
  simultaneously. Both are right; making them agree removes information.
- **The snapshot is device-frame; the wire layer mirrors it.** `EbusCircuitSnapshot.instant_power_w` is positive while consuming and
  `EbusBatterySnapshot.active_power_w` is positive while discharging. The negation lives in the `bag_builder` resolver (`_circuit_wire_active_power`; for
  the hosted BESS's `meter/active-power`, `_bess_enclosure_frame_active_power`, used only by a span-variant panel on firmware before release 202639 —
  `_bess_wire_active_power` publishes the battery's own frame on purpose; see DESIGN.md 'Firmware-keyed conventions'), next to the docstring explaining it —
  never by redefining a snapshot field, which would silently change every other reader.
- **The four `power-flows` values sum to zero.** They are four terms of one balance at one node, not four independent meters. Derive `power_flow_grid` from the
  lugs and the BESS, never by back-solving from the other three — a residual satisfies the balance by construction and detects nothing.
- **`meter/imported-energy` and `meter/exported-energy` integrate their own `active-power`,** not some other signal that happens to be nearby. Only one of the
  pair may advance in a tick; both advancing means the registers are integrating something that is not what the meter reads.
- **A new metered surface states its frame in a docstring before it is published.** These defects are silently wrong at the consumer, and the damage is
  **persisted, not displayed**. A renamed or removed property fails loudly; an inverted sign keeps producing plausible numbers, and these values feed
  long-term statistics — energy dashboards, cost attribution, utility reconciliation. A consumer records the wrong direction for weeks, and fixing the
  publisher afterwards does not repair what was already aggregated and stored. Energy registers are worse still: `imported-energy` and `exported-energy`
  are monotonic, so a tick that advances both writes an import and an export that never happened, and neither can be subtracted back out later.

**Note on the catalogs.** `wire/catalogs/power-flows.json` states the opposite convention for `grid`, `pv` and `battery`, and reference direction has no
machine-readable form in the catalogs at all — it lives inside free-text `description` strings, and `meter.json` defers to prose that never reaches the JSON.
So this rule cannot currently be expressed as a conformance check, which is exactly how it was broken here for months with every check green.
