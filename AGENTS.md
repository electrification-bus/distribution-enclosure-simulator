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

## Lessons from review

Each rule below came out of a review of a pull request to this repository, and each was a defect that had already been made at least once. They
complement `CONTRIBUTING.md`'s "What done means", which they assume.

**Prove a test or guard by breaking what it guards.**

- Revert the fix, or mutate the guarded code, and confirm the test fails. A guard that passes every mutation is worthless, and that is only found by
  mutating it, not by reading it (#26, #62).
- Assert the outcome a consumer can observe, usually the published value, not an internal step. A test that only checks a handler did not store a write
  still passes on code that stores and republishes from somewhere else (#31).
- A test that claims to keep documentation runnable must read the documentation. A hand-copied transcription pins the sequence but lets the document
  rot (#17).
- Claims about behaviour on a real broker, a real panel or a real build are measured there, not inferred from the code (#15, #17).

**What ships is the API: docstrings, README and CHANGELOG.**

- A docstring is in the wheel and is what `help()` and every IDE shows, and `README.md` is the PyPI page. A recipe in either must run as written and
  must describe every path the code has, not only the one the change was about (#17).
- `CHANGELOG.md`'s `Fixed` is for defects a released version had. A revision between drafts of an unreleased change is not a fix, and filing it as one
  tells readers a release was broken (#17).
- Check where an entry lands, not only its text. A new heading inserted above an existing entry re-files it without the diff showing a deletion, and the
  released changelog and GitHub release body cannot be amended (#24).
- Re-derive any count or list in a changelog or document against the source at the tagged version (#24).
- A `DeprecationWarning` must be attributed to the caller's line and checked under Python's default filters, or it is dropped silently outside a test
  runner (#24).
- Packaging metadata can degrade without an error: floor build tooling at the version that produces the metadata you intend, and check the built
  artifact (#15).

**Public surface.**

- Every type a public signature names, constructors included, is importable from `ebus_panel_sim` and listed in `__all__`. The reachability test derives
  both sides; it is the guard, so send the export with the change rather than deferring it (#26).
- Do not add a parameter, keyword or flag that gates nothing yet. A dormant surface reads as a feature and has to be un-shipped later (#54).

**Validation and capture.**

- A predicate that reads metadata must refuse what it cannot interpret, or run after validation. Otherwise one path accepts a definition another path
  rejects (#53).
- Captured and definition data keep their exact text: refuse non-finite numbers, duplicate keys and non-scalar metadata, and read YAML so `02134` stays a
  string. A `null` in a snapshot means unpublished, not zero (#62).
- When a fix reproduces a field panel, check it against every capture available, not only the one at hand. The firmware is not uniform even within one
  release (#69).

**Specification and variants.**

- Implement the rule the specification states, against the signal it names, rather than a stub. Cite the specification, not a migration guide, where
  the two disagree (#31, #52).
- Behaviour only a vendor's firmware warrants belongs behind that vendor's variant, so `reference` keeps publishing only what the specification says
  (#71).
