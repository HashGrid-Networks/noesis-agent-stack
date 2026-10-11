# Specialist-router offline readiness — 2026-10-08

Status: **offline-ready for tool registration; inference + reviewer unblocking
still require external/provider actions and approval.**

## 1. Tool-registration root cause (confirmed)

The fresh CLI emitted `Warning: Unknown toolsets: noesis_specialist_router`.
Investigation in the installed Hermes core (`~/.hermes/hermes-agent`) shows this is a
**known startup-ordering artifact, not a plugin registration defect**:

- `hermes_cli/cli_init_mixin.py:222-234` (`_init_toolsets`) checks a toolset name against
  `get_plugin_toolset_keys_nowait()`. Comment at 222-224: plugin toolsets "register during
  plugin discovery, which startup runs on a background thread that has not necessarily landed
  yet; names it declared (or the previous launch persisted ...) are not typos (#71650)."
- Because the plugin was freshly enabled, the **previous launch** persisted toolset cache
  (`cache/plugin_toolset_keys.json`) did not yet include `noesis_specialist_router`, so at the
  instant of startup validation the name was flagged unknown.
- Background discovery then completes and re-persists the cache. Evidence: the canary session
  log shows `Plugin discovery complete: 77 found, 70 enabled` and the refreshed cache now
  contains `"noesis_specialist_router"` in `toolset_keys`.

The plugin loads and registers its tool + toolset correctly. The warning is transient and
self-heals on the fresh session's own discovery. No plugin-side defect and no Hermes-core
change is required to make the tool registrable; the warning text is a core wording/ordering
artifact (see §5).

## 2. Offline proof that the tool reaches the model-facing inventory

New offline regression (no inference): `orchestration/orchestrator/tests/test_noesis_specialist_router_offline_inventory.py`.

- Loads the plugin through the real `PluginManager.discover_and_load()` into an isolated
  temp `HERMES_HOME`.
- Asserts `noesis_specialist_router` is in `get_plugin_toolset_keys_nowait()` (the exact check
  `cli_init_mixin._init_toolsets` uses).
- Asserts `noesis_specialist_delegate` is registered exactly once in the tool registry with
  `toolset == noesis_specialist_router` and a valid schema.
- Asserts disabling the plugin removes both the tool and its toolset recognition.
- Asserts re-running discovery settles the toolset as known (the warning is transient).

Result: **3 passed** (offline, no provider request).

## 3. Regression / full suite (computed)

```
uv run pytest orchestration/orchestrator/tests -q
168 passed in 47.61s
```

(was 165; +3 offline-inventory tests. Not the earlier claim.)

## 4. Reviewer reconciliation — DO NOT unblock yet

Both reviewers are `resolution.state: blocked` in `platform/runtime-reconciliation.yaml`.

| Profile | Blocker | Required gates | KIMI_CODE eligible for its review role? |
|---|---|---|---|
| `noesis-sentinel` (security_adversarial_review) | repo_declared = OPENROUTER_FALLBACK while policy keeps OpenRouter disabled/fallback-only | independent_provider_review, qa_or_policy_gate, resolve_openrouter_catalog_contradiction, human_review_before_any_runtime_change | Yes (KIMI_CODE is an eligible_primary_lane for security_adversarial_review), **but the profile is blocked by the OpenRouter contradiction, not by lane choice** |
| `noesis-skeptic` (qa_validation) | same | same | Yes (KIMI_CODE eligible for qa_validation), same caveat |

The blocker is a **routing/registry contradiction**, not missing Kimi availability. `state: blocked`
is not cleared by provider recovery or by setting `resolution.state: approved` alone; it requires
the gates above, including human review.

**Evidence that can be generated offline:** canonical profile identity/responsibility (already in
the YAML), observed-vs-declared runtime reconciliation (already recorded), tool/least-privilege
boundaries, KIMI_CODE role-lane mapping, cancellation/isolation/approval-boundary checks.

**Evidence requiring a human/security approver:** resolution of the
`OPENROUTER_FALLBACK`-as-primary contradiction with the global OpenRouter-disabled policy, the
independent-provider review, the QA/policy gate, and the human review before any runtime change.

## 5. Proposed reconciliation diff — NOT applied

No diff is proposed for the reviewer profiles: they are blocked on a genuine policy
contradiction that must not be silently resolved. The only proposed core-side note is to
clarify the `cli.startup.unknown_toolsets` string when the flagged toolset is a plugin toolset
that is about to be discovered (a Hermes-core wording change, **not** applied under this
authorization).

## 6. Read-only inference-lane matrix (no requests issued)

| Lane | Policy status | Pinned model | Role-eligible? | Last observed availability | Remaining unknown/action |
|---|---|---|---|---|---|
| KIMI_CODE | **approved** | kimi-k2.7-code | orchestrator/forge yes; reviewers blocked (reconciliation) | HTTP 403 weekly quota exhausted (2026-10-08) | quota reset / purchase; reviewer reconciliation |
| CLAUDE_CODE_PRO | proposed (not live) | claude-sonnet-5 | — | HTTP 400 billing insufficient (2026-10-08) | Anthropic billing restore; lane is proposed, not approved |
| NOUS_PORTAL | proposed | (none pinned) | — | reachable (this session via deepseek-v4-flash) but **not policy-selectable** | lane approval + pinned model |
| OPENROUTER_FALLBACK | disabled | — | — | — | stays disabled |
| CHATGPT_PRO / XAI_GROK / VENICE_PRO | blocked_missing_configuration | — | — | — | config / key / pin |

Configured: kimi-coding, anthropic, nous (runtime). Authenticated: kimi, anthropic, nous.
Reachable with prior evidence: nous. Policy-selectable: **only KIMI_CODE** (approved), but
quota-blocked. Currently usable for a role-eligible dispatch: **none** (anthropic 400, kimi 403,
nous not policy-approved).

**Smallest next inference decision:** restore access to the already-approved, role-eligible
`KIMI_CODE` lane (quota reset) to run a canary, **or** complete the existing approval process for
another lane. This is a provider/account action or a policy approval; no purchase, credential
change, fallback reorder, or model reassignment was made.

## 7. Confirmation

No inference request, no quota/billing purchase, no credential change, no Hermes-core patch,
no persistent profile-config/model/reconciliation/policy change, no commit/push, no service
restart, no real specialist execution. Persistent plugin/configuration/policy state is unchanged.
