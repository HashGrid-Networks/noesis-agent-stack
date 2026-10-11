# Reviewer readiness — noesis-sentinel / noesis-skeptic (Phase 2, PROPOSAL ONLY)

Status: **not applied.** `resolution.state` stays blocked; no approval/status
change is made by this document or by the offline work. This proposes the
smallest reconciliation that resolves the documented OpenRouter contradiction.

## Blocker (confirmed, read-only)

`platform/runtime-reconciliation.yaml` lists both reviewers `resolution.state: blocked`:

- `profile_id: noesis-sentinel` — target_routing_class `security_adversarial_review`
- `profile_id: noesis-skeptic` — target_routing_class `qa_validation`
- rationale: *Repository-declared profile uses OpenRouter as a primary route while
  global catalog/policy keeps OpenRouter disabled/fallback-only.*
- required_gates: `independent_provider_review`, `qa_or_policy_gate`,
  `resolve_openrouter_catalog_contradiction`, `human_review_before_any_runtime_change`.

### The contradiction (local evidence)

`profiles/live/noesis-sentinel/config.yaml` and `profiles/live/noesis-skeptic/config.yaml`
both declare, as the **primary** default:

```yaml
model:
  default: anthropic/claude-sonnet-5
  provider: openrouter
[... api_key: ${OPENROUTER_API_KEY}]
fallback_providers: '["openai-codex"]'
```

This is exactly what the global policy forbids as a primary route
(`platform/inference-routing.yaml`; regression `test_openrouter_denied_regardless_of_requested_route`).
The **active runtime** profiles (`~/.hermes/profiles/noesis-sentinel|skeptic/config.yaml`)
do **not** use openrouter primary; they resolve `anthropic` primary with kimi-coding
available as an alternate — so the contradiction is between the repo-declared file and
policy, not between two live providers.

## Per-profile readiness (established)

| Aspect | noesis-sentinel | noesis-skeptic |
|---|---|---|
| Canonical identity | security reviewer (reviewer-only) | adversarial/QA reviewer (reviewer-only) |
| Runtime install | Hermes profile installed (`~/.hermes/profiles/noesis-sentinel`) | Hermes profile installed |
| Launch mechanism | `hermes -p noesis-sentinel ...` (HermesCliAdapter) | `hermes -p noesis-skeptic ...` |
| Allowed lane (reconciled target) | KIMI_CODE / kimi-k2.7-code (eligible for security_adversarial_review) | KIMI_CODE / kimi-k2.7-code (eligible for qa_validation) |
| Tool/data permissions | reviewer-only: no terminal/code execution | reviewer-only: no terminal/code execution |
| Independent review context | yes (separate profile, distinct from implementer) | yes |
| Routing/QA coverage | security/adversarial review role | QA/adversarial review role |
| Required security evidence | independent_provider_review + qa_or_policy_gate (**human**) | same |
| Outstanding findings | OpenRouter-primary contradiction (open) | OpenRouter-primary contradiction (open) |

The two reviewer gates (`security_adversarial_review`, `qa_validation`) both list
KIMI_CODE as an `eligible_primary_lane` in runtime-reconciliation.yaml. Provider
recovery is orthogonal to this block: the block is the route/registry contradiction,
not Kimi availability.

## Proposed reconciliation patch — for human approval, NOT applied

Resolve the contradiction by making the repo-declared default the approved KIMI_CODE
route (mirroring the active runtime's available providers) while demoting OpenRouter to
fallback-only (which policy permits). Applies identically to
`profiles/live/noesis-sentinel/config.yaml` and `profiles/live/noesis-skeptic/config.yaml`:

```diff
 model:
-  default: anthropic/claude-sonnet-5
-  provider: openrouter
+  default: kimi-k2.7-code
+  provider: kimi-coding
```

(the `${OPENROUTER_API_KEY}` block and `fallback_providers` remain fallback-only; the
`resolve_openrouter_catalog_contradiction` gate is satisfied by removing OpenRouter as
the **primary** route — it stays a permitted fallback.)

With that applied, `platform/runtime-reconciliation.yaml` `resolution.state` for both
profiles may be reconsidered from `blocked` → `approved`. **That status change and the
live-profile default change are NOT applied here**; they require the human
`independent_provider_review` + `qa_or_policy_gate` decision, as the block demands.

## Wording about security evidence

Reconciliation to `approved` does **not** itself constitute an adversarial/security
review having occurred. If approval of these reviewer profiles is granted, that is a
policy/routing decision about which lane reviews travel on — independent review sessions
still have to happen and pass per-task gates before any reviewed result is accepted.
