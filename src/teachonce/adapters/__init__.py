"""Adapter layer — isolated bridges to third-party open-source projects.

Design contract (AGENTS.md §8, docs/ARCHITECTURE.md §9):

- Adapters only translate protocols/data. The core engines
  (learning / matching / correction / evaluation) never import an adapter
  and never know which external project produced the input.
- Third-party packages are **optional extras**, imported lazily inside the
  adapter. A missing package raises :class:`AdapterUnavailableError` with
  the exact extra to install — the core library works without any of them.
- Capabilities with safety implications (GUI observation) are gated behind
  explicit user consent, checked at call time, not at import time.
- Nothing here fabricates success: if the external project cannot run, the
  adapter raises instead of returning canned output.

Current adapters:
- ``gepa_optimizer`` — GEPA (gepa-ai/gepa, MIT) reflective evolution of a
  skill's decision rules, fed by TeachOnce's own held-out evaluation.
  Output is always a *candidate* version; human approval still required.
- ``openadapt`` — converts an OpenAdapt (MIT) GUI recording export into a
  TeachOnce Demonstration. Requires ``TEACHCONE_GUI_OBSERVATION`` opt-in.
"""
