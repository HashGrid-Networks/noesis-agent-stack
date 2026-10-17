"""Runtime launch adapters for specialist dispatch.

The control plane selects an agent profile first; this module turns that
canonical profile into an explicit runtime launch target and verifies launcher-
side provenance before the task may be considered dispatched. A real task
launch must carry an explicit approved provider/model pair, a validated
deadline, and a fallback-deny boundary. Installed Hermes has ``-p``,
``--provider``, ``-m``/``--model`` and ``-z`` (verified in
hermes_cli/_parser.py) but no supported flag that disables ``fallback_providers``.
When the process boundary cannot enforce fallback denial, launch returns a
typed blocker and does not start inference.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.isolated_attempt_home import (
    AttemptHomeError,
    IsolatedAttemptHome,
    IsolatedAttemptHomeDigest,
)
from app.transport_contract import CapabilityObservation, TransportRequest


_AMBIENT_INFERENCE_ENV = (
    "HERMES_INFERENCE_MODEL",
    "HERMES_MODEL",
    "HERMES_PROVIDER",
    "HERMES_INFERENCE_PROVIDER",
)
_SECRET = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|Bearer\s+\S+)"
)
_OUTPUT_LIMIT = 8192
_UNSET = object()


@dataclass(frozen=True)
class LaunchAttestation:
    requested_profile: str
    loaded_profile: str
    runtime: str
    profile_path: str
    manifest_sha256: str


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    requested_profile: str
    loaded_profile: str | None
    command: list[str]
    attestation: LaunchAttestation | None
    error: str | None = None
    latency_ms: float | None = None
    provider: str | None = None
    model: str | None = None
    timeout_s: float | None = None
    artifacts: tuple[str, ...] = ()
    disposition: str | None = None
    verification_pending: bool = False
    accepted: bool = False
    process_id: int | None = None
    session_id: str | None = None
    cleanup_state: str | None = None
    candidate_digest: str | None = None
    usage_tokens: int | None = None
    blocker_code: str | None = None
    model_driven_canary: bool = False
    workspace_reusable: bool | None = None
    # Execution-evidence binding. ``candidate_digest`` identifies the collected,
    # verified candidate artifact; ``stdout_digest`` identifies the captured
    # stdout (a distinct execution-evidence identity), and ``captured_stdout``
    # preserves the (redacted) output so partial evidence is never lost.
    task_id: str | None = None
    attempt_id: str | None = None
    contract_revision: str | None = None
    assignment_epoch: int | None = None
    baseline: str | None = None
    stdout_digest: str | None = None
    captured_stdout: str | None = None
    collection_error: str | None = None
    # True only when no inference ran (profile-resolution rehearsal). Real
    # launches carry False so the control plane can distinguish a rehearsal
    # from a live launch in the durable event (policy_decision).
    dry_run: bool = False


class RuntimeAdapter(Protocol):
    """Narrow runtime boundary used by tests and live dispatch."""

    def profile_installed(self, profile_id: str) -> bool:
        """Return true when this runtime can explicitly select ``profile_id``."""
        ...

    def launch(
        self,
        *,
        profile_id: str,
        prompt: str,
        task_id: str,
        cwd: str | None = None,
        dry_run: bool = True,
    ) -> LaunchResult:
        """Launch or dry-run the explicit profile target."""
        ...


def _session_scalar(value: str) -> str:
    """Reject values that could smuggle a fallback entry into the isolated config."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty session scalar")
    if any(char in value for char in "\n\r\t:{}[]#&*!|>%@`'\""):
        raise ValueError("unsafe session scalar")
    return value


def write_isolated_fallback_session(workspace: Path, profile_id: str, provider: str, model: str) -> Path:
    """Write a discardable session home. Does not touch the persistent profile config.

    Installed Hermes loads ``$HERMES_HOME/config.yaml`` after ``-p`` re-homes onto
    ``$HERMES_HOME/profiles/<id>``. An explicit ``fallback_providers: []`` with no
    ``fallback_model`` is what ``get_fallback_chain`` treats as no chain. A pinned
    ``--provider/-m`` argument is not this proof.
    """
    safe_profile = _session_scalar(profile_id)
    safe_provider = _session_scalar(provider)
    safe_model = _session_scalar(model)
    root = workspace / ".noesis-fallback-session"
    profile = root / "profiles" / safe_profile
    profile.mkdir(parents=True, exist_ok=True)
    config = (
        "model:\n"
        f"  provider: {safe_provider}\n"
        f"  default: {safe_model}\n"
        "fallback_providers: []\n"
    )
    (profile / "config.yaml").write_text(config, encoding="utf-8")
    if not (profile / "SOUL.md").is_file():
        (profile / "SOUL.md").write_text(f"name: {safe_profile}\n", encoding="utf-8")
    return root


def isolated_fallback_chain(config_text: str) -> list[dict[str, str]]:
    """Parse the session config the same way an empty installed chain is recognized.

    ``fallback_providers: []`` alone is not enough: installed ``get_fallback_chain``
    still appends a usable ``fallback_model``. Any fallback_model entry is a chain.
    """
    providers_empty = False
    legacy: list[dict[str, str]] = []
    for raw in config_text.splitlines():
        line = raw.strip()
        if line.startswith("fallback_providers:"):
            providers_empty = line.split(":", 1)[1].strip() == "[]"
        elif line.startswith("fallback_model:"):
            legacy.append({"provider": "present", "model": "present"})
    if not providers_empty or legacy:
        return [{"provider": "unproven", "model": "unproven"}]
    return []


def fallback_denial_proven(config_text: str) -> bool:
    return isolated_fallback_chain(config_text) == []


class FallbackDenied(RuntimeError):
    """A provider request was refused before it was sent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize_inference_request(
    *,
    approved_provider: str,
    approved_model: str,
    requested_provider: str,
    requested_model: str,
    fallback_chain: list,
    attempts: int = 0,
    blocked: bool = False,
) -> None:
    """Refuse a lane switch or a repeat after billing failure before any request is counted."""
    if blocked or attempts >= 1:
        raise FallbackDenied("provider_attempt_exhausted", "billing or quota failure already closed this attempt")
    if fallback_chain:
        raise FallbackDenied("fallback_unenforced", "fallback chain is not empty")
    if requested_provider != approved_provider or requested_model != approved_model:
        raise FallbackDenied("unauthorized_fallback", "requested route is not the approved pair")


def _authorize_launch_request(
    *,
    approved_provider: str,
    approved_model: str,
    requested_provider: str,
    requested_model: str,
    fallback_chain: list,
    attempts: int,
    blocked: bool,
) -> None:
    """Fail-closed authorization for one launch request at the real inference boundary.

    Mirrors ``authorize_inference_request`` but is keyed to a single launch and
    refuses when authorization is absent or denied, before any child process is
    started. It requires the persisted approved provider/model pair, the
    effective empty fallback chain for this launch, and a durable attempt
    counter / block state so one failed provider attempt cannot cycle again.
    """
    authorize_inference_request(
        approved_provider=approved_provider,
        approved_model=approved_model,
        requested_provider=requested_provider,
        requested_model=requested_model,
        fallback_chain=fallback_chain,
        attempts=attempts,
        blocked=blocked,
    )


# Per-launch credential-attempt budgeting. F5: a same-provider billing/quota
# failure closes the budget for this attempt; a new launch gets a fresh counter.
# Durable block state survives within this process handle (no provider/policy
# change, no persistent config edit), and is intentionally distinct from the
# provider fallback chain and wall-clock timeout.
_ATTEMPT_BUDGET: dict[str, dict[str, object]] = {}


def _attempt_budget_key(*, provider: str, model: str, task_id: str | None) -> str:
    return f"{provider}|{model}|{task_id or 'generic'}"


def _credential_attempt_lock(provider: str, model: str, task_id: str | None) -> dict[str, object]:
    key = _attempt_budget_key(provider=provider, model=model, task_id=task_id)
    entry = _ATTEMPT_BUDGET.get(key)
    if entry is None:
        entry = {"attempts": 0, "blocked": False}
        _ATTEMPT_BUDGET[key] = entry
    return entry


def _credential_budget_closed(provider: str, model: str, task_id: str | None) -> bool:
    """True when the budget for this launch attempt is already closed.

    F5: after one launch completes (success or a billing/quota failure the
    adapter records), the budget closes, so a second launch of the same
    attempt is refused before any child is started. A fresh attempt opens
    the budget and does not block.
    """
    entry = _credential_attempt_lock(provider=provider, model=model, task_id=task_id)
    return bool(entry.get("blocked") is True)


def _close_credential_budget(provider: str, model: str, task_id: str | None) -> None:
    """Record a completed launch: close the budget so a repeat is refused."""
    entry = _credential_attempt_lock(provider=provider, model=model, task_id=task_id)
    entry["attempts"] = int(entry["attempts"]) + 1
    entry["blocked"] = True


def reset_credential_attempt_budget() -> None:
    """Clear the per-attempt budget cache for test isolation.

    F5 block-state persists across launch calls inside one test, but resets
    between tests so no test sees another test's closed budget.
    """
    _ATTEMPT_BUDGET.clear()


def redact(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    cleaned = _SECRET.sub("[REDACTED]", text or "")
    if len(cleaned) > limit:
        return cleaned[:limit] + "\n[truncated]"
    return cleaned


def workspace_contains(workspace: Path, relative: str) -> Path | None:
    """Resolve a workspace-relative artifact, rejecting traversal and symlinks."""
    root = workspace.resolve()
    if workspace.is_symlink():
        return None
    candidate = (root / relative).resolve() if not Path(relative).is_absolute() else Path(relative)
    raw = root / relative
    if raw.is_symlink() or candidate.is_symlink():
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


class HermesCliAdapter:
    """Hermes profile launcher with explicit inference and fallback denial.

    ``dry_run=True`` verifies the selected profile without inference and does
    not consume an execution attempt. ``False`` refuses to spawn unless an
    approved provider/model pair, a positive remaining lease, and a process
    boundary that can enforce fallback denial are all present. An omitted
    remaining lease is not replaced by ``timeout_s``.
    """

    requires_approved_pair = True

    def __init__(
        self,
        *,
        hermes_bin: str = "hermes",
        profiles_root: Path | None = None,
        process_runner: Any | None = None,
        requires_approved_pair: bool = True,
        materialise_attempt_home: bool = False,
        attempt_home_destination: Path | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin
        self.profiles_root = profiles_root or Path.home() / ".hermes" / "profiles"
        self.hermes_home = self.profiles_root.parent
        self.process_runner = process_runner
        self.requires_approved_pair = requires_approved_pair
        # B2 (contract §3.6): the launcher must materialise and attest
        # the isolated attempt home before any real spawn when the
        # caller opts in. The default is opt-in so the existing
        # regression suite (which uses synthetic ``profiles_root``
        # paths without the canonical SOUL.md/config.yaml/honcho.json
        # trio) continues to pass. B2 dispatch paths and tests enable
        # the integration explicitly.
        self.materialise_attempt_home = materialise_attempt_home
        self.attempt_home_destination = attempt_home_destination
        # Public observation surface: tests assert on the call list to
        # confirm the new path actually invokes materialise/attest.
        self.attempt_home_calls: list[str] = []

    def _env(self) -> dict[str, str]:
        """Resolve sibling profiles from the base Hermes home, never ambient inference."""
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        for key in _AMBIENT_INFERENCE_ENV:
            env.pop(key, None)
        return env

    def profile_installed(self, profile_id: str) -> bool:
        return (self.profiles_root / profile_id).is_dir()

    def observe_capabilities(self, profile_id: str) -> CapabilityObservation:
        """Describe the existing native boundary without claiming worker features."""
        supported = ("profile-attestation", "prompt", "cancel-process-group")
        return CapabilityObservation(
            transport="native",
            worker_id=profile_id,
            advertised=supported,
            supported=supported if self.profile_installed(profile_id) else (),
            assumptions_verified=self.profile_installed(profile_id),
        )

    def _materialise_and_attest(
        self,
        *,
        profile_id: str,
        workspace: Path | str | None,
    ) -> IsolatedAttemptHomeDigest:
        """Materialise the isolated attempt home for ``profile_id`` and attest it.

        B2 (contract §3.6): the launcher's pre-send check now actually
        invokes ``IsolatedAttemptHome.materialise`` and ``.attest``. The
        digest's ``profile_id`` is compared to ``profile_id`` by the
        caller so a cross-profile mismatch is a typed refusal. ``self.attempt_home_calls``
        records the call list for tests.
        """
        if not self.materialise_attempt_home:
            self.attempt_home_calls.append("materialise_skipped")
            return IsolatedAttemptHomeDigest(
                profile_id=profile_id,
                sha256="skipped",
                file_digests=(),
                source_digest="skipped",
                approved_files=(),
            )
        source_root = self.profiles_root / profile_id
        destination_root = (
            Path(workspace) / ".noesis-attempt-home"
            if workspace is not None
            else (self.attempt_home_destination or Path.home() / ".noesis-attempt-home" / profile_id)
        )
        home = IsolatedAttemptHome(
            profile_id=profile_id,
            source_root=source_root,
            destination_root=destination_root,
        )
        self.attempt_home_calls.append("materialise")
        materialised = home.materialise()
        self.attempt_home_calls.append("attest")
        attested = home.attest()
        if attested.profile_id != materialised.profile_id:
            self.attempt_home_calls.append("cross_profile_mismatch")
            raise AttemptHomeError(
                "cross_profile_mismatch",
                f"attempt-home materialised as '{materialised.profile_id}' but attest produced "
                f"'{attested.profile_id}'",
            )
        if attested.profile_id != profile_id:
            self.attempt_home_calls.append("cross_profile_mismatch")
            raise AttemptHomeError(
                "cross_profile_mismatch",
                f"attempt-home bound '{attested.profile_id}' but launcher was asked to "
                f"launch '{profile_id}'",
            )
        return attested

    def launch_request(self, request: TransportRequest, **runtime_options: Any) -> LaunchResult:
        """Adapt the transport-neutral request onto the established native path."""
        if request.transport != "native":
            return self._blocked(
                request.worker_profile,
                [],
                None,
                "transport_mismatch",
                "native launcher received a non-native transport request",
            )
        if request.worker_profile != runtime_options.pop(
            "profile_id", request.worker_profile
        ):
            return self._blocked(
                request.worker_profile,
                [],
                None,
                "profile_mismatch",
                "transport worker binding does not match the requested profile",
            )
        return self.launch(
            profile_id=request.worker_profile,
            prompt=request.prompt,
            task_id=request.task_id,
            cwd=request.cwd,
            dry_run=request.dry_run,
            **runtime_options,
        )

    def _manifest_sha256(self, profile_id: str) -> str:
        digest = hashlib.sha256()
        root = self.profiles_root / profile_id
        for name in ("SOUL.md", "config.yaml", "honcho.json"):
            path = root / name
            if path.is_file():
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
        return digest.hexdigest()

    def _run(self, cmd: list[str], *, timeout: float, cwd: str | None, env: dict[str, str]):
        if self.process_runner is not None:
            return self.process_runner(
                cmd,
                timeout=timeout,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                start_new_session=True,
            )
        return self._run_in_process_group(cmd, timeout=timeout, cwd=cwd, env=env)

    def _run_in_process_group(self, cmd: list[str], *, timeout: float, cwd: str | None, env: dict[str, str]):
        """Start the child as its own session so timeout can signal the group."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd or None,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            confirmed = self._signal_process_group(proc.pid)
            try:
                proc.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                confirmed = False
            setattr(exc, "pid", proc.pid)
            setattr(exc, "cleanup_confirmed", confirmed)
            raise
        completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        setattr(completed, "pid", proc.pid)
        return completed

    def _signal_process_group(self, pid: Any) -> bool:
        """Signal the whole group. True only when the group is confirmed gone.

        pgid 0 or negative would signal the caller's group. Never do that.
        """
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.02)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    @staticmethod
    def _positive_seconds(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        return number

    def _effective_timeout(self, timeout_s: Any, remaining_lease_s: Any) -> float | None:
        """Cap the subprocess timeout at a positive remaining lease.

        An omitted lease (``_UNSET``) is not a duration. Do not fall back to
        the raw ``timeout_s``; a direct non-dry-run launch must refuse.
        """
        requested = self._positive_seconds(timeout_s)
        remaining = self._positive_seconds(remaining_lease_s)
        if requested is None or remaining is None:
            return None
        return min(requested, remaining)

    def _attest(self, profile_id: str) -> tuple[LaunchAttestation | None, str | None, list[str]]:
        cmd = [self.hermes_bin, "-p", profile_id, "profile", "show", profile_id]
        try:
            proc = self._run(cmd, timeout=60, cwd=None, env=self._env())
        except subprocess.TimeoutExpired as exc:
            if getattr(exc, "cleanup_confirmed", None) is None:
                pid = getattr(exc, "pid", None)
                if pid is None and self.process_runner is not None:
                    pid = getattr(self.process_runner, "active_pid", None)
                self.cancel_managed(handle={"pid": pid, "pgid": pid}, workspace=Path("."))
            return None, f"profile attestation command failed: {exc}", cmd
        except Exception as exc:  # noqa: BLE001 - launch boundary must report typed failure
            return None, f"profile attestation command failed: {exc}", cmd
        output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
        if getattr(proc, "returncode", 1) != 0:
            return None, f"profile attestation failed ({proc.returncode}): {output.strip()}", cmd
        loaded = None
        path = None
        for line in output.splitlines():
            if match := re.match(r"Profile:\s*(\S+)", line.strip()):
                loaded = match.group(1)
            if match := re.match(r"Path:\s*(.+)", line.strip()):
                path = match.group(1).strip()
        if loaded != profile_id:
            return None, f"requested profile '{profile_id}' but Hermes reported '{loaded}'", cmd
        return (
            LaunchAttestation(
                requested_profile=profile_id,
                loaded_profile=loaded,
                runtime="hermes",
                profile_path=path or str(self.profiles_root / profile_id),
                manifest_sha256=self._manifest_sha256(profile_id),
            ),
            None,
            cmd,
        )

    def _blocked(
        self,
        profile_id: str,
        cmd: list[str],
        attestation: LaunchAttestation | None,
        code: str,
        error: str,
    ) -> LaunchResult:
        return LaunchResult(
            False,
            profile_id,
            attestation.loaded_profile if attestation else None,
            cmd,
            attestation,
            error,
            blocker_code=code,
            accepted=False,
            verification_pending=False,
            model_driven_canary=False,
        )

    def launch(
        self,
        *,
        profile_id: str,
        prompt: str,
        task_id: str,
        cwd: str | None = None,
        dry_run: bool = True,
        provider: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        remaining_lease_s: Any = _UNSET,
        workspace: str | Path | None = None,
        fallback_policy: str = "deny",
        attempt_id: str | None = None,
        contract_revision: str | None = None,
        assignment_epoch: int | None = None,
        baseline: str | None = None,
    ) -> LaunchResult:
        if dry_run:
            attestation, error, attest_cmd = self._attest(profile_id)
            if error or attestation is None:
                return LaunchResult(False, profile_id, None, attest_cmd, None, error, blocker_code="profile_mismatch")
            return LaunchResult(
                True,
                profile_id,
                attestation.loaded_profile,
                attest_cmd,
                attestation,
                accepted=False,
                verification_pending=False,
                model_driven_canary=False,
                dry_run=True,
            )
        if not provider or not model:
            return self._blocked(profile_id, [], None, "missing_approved_pair", "approved provider/model pair is required")
        if timeout_s is None or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            return self._blocked(profile_id, [], None, "invalid_deadline", "deadline must be a positive number of seconds")
        if fallback_policy != "deny":
            return self._blocked(profile_id, [], None, "fallback_unenforced", "fallback policy must be deny")
        if workspace is None:
            return self._blocked(
                profile_id,
                [],
                None,
                "fallback_unenforced",
                "isolated empty fallback chain is not proven; refusing inference",
            )
        try:
            isolated_root = write_isolated_fallback_session(
                Path(workspace), profile_id, provider, model
            )
            config_text = (isolated_root / "profiles" / profile_id / "config.yaml").read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return self._blocked(
                profile_id,
                [],
                None,
                "fallback_unenforced",
                f"isolated fallback control could not be proven: {exc}",
            )
        if not fallback_denial_proven(config_text):
            return self._blocked(
                profile_id,
                [],
                None,
                "fallback_unenforced",
                "isolated config does not prove an empty fallback chain; refusing inference",
            )
        # F5: refuse a second launch of the same attempt if the credential
        # budget is already closed (billing/quota failure). A fresh attempt
        # opens the budget and is allowed to proceed.
        if _credential_budget_closed(provider=provider, model=model, task_id=task_id):
            return self._blocked(
                profile_id,
                [],
                None,
                "credential_attempt_exhausted",
                "same-provider credential budget exhausted; refusing inference",
            )
        effective_timeout = self._effective_timeout(timeout_s, remaining_lease_s)
        if effective_timeout is None:
            return self._blocked(
                profile_id,
                [],
                None,
                "invalid_deadline",
                "remaining deadline is missing, invalid, or expired",
            )
        # B2 (contract §3.6): actually materialise and attest the
        # isolated attempt home before the real spawn. The previous
        # comment-only integration is replaced by an explicit call
        # whose digest.profile_id is compared to the launcher's
        # ``profile_id`` so a mismatch raises a typed refusal.
        try:
            attempt_digest = self._materialise_and_attest(
                profile_id=profile_id,
                workspace=workspace,
            )
        except AttemptHomeError as exc:
            return LaunchResult(
                False,
                profile_id,
                None,
                [],
                None,
                f"attempt home failure: {exc}",
                blocker_code=exc.code,
                task_id=task_id,
                attempt_id=attempt_id,
                dry_run=False,
            )
        if attempt_digest.profile_id != profile_id:
            return LaunchResult(
                False,
                profile_id,
                None,
                [],
                None,
                f"attempt-home loaded_profile mismatch: bound '{attempt_digest.profile_id}' "
                f"but launcher was asked to launch '{profile_id}'",
                blocker_code="loaded_profile_mismatch",
                task_id=task_id,
                attempt_id=attempt_id,
                dry_run=False,
            )
        attestation, error, attest_cmd = self._attest(profile_id)
        if error or attestation is None:
            return LaunchResult(False, profile_id, None, attest_cmd, None, error, blocker_code="profile_mismatch")
        # F3: mandatory fail-closed authorization at the real inference boundary
        # before the child starts.
        _authorize_launch_request(
            approved_provider=provider,
            approved_model=model,
            requested_provider=provider,
            requested_model=model,
            fallback_chain=isolated_fallback_chain(config_text),
            attempts=0,
            blocked=False,
        )
        # F4: child-side readback attestation of the launched isolated home
        # before any inference request. Re-open $HERMES_HOME/profiles/<id> and
        # verify identity + effective fallback chain, same check the child
        # performs at startup; fail closed when identity or chain mismatches.
        child_attestation, child_error, _ = self._attest(profile_id)
        child_chain_text = (isolated_root / "profiles" / profile_id / "config.yaml").read_text(encoding="utf-8")
        if child_error or child_attestation is None:
            return LaunchResult(False, profile_id, None, attest_cmd, None, child_error, blocker_code="profile_mismatch")
        if child_attestation.loaded_profile != profile_id:
            return LaunchResult(False, profile_id, None, attest_cmd, None, f"child profile mismatch: {child_attestation.loaded_profile}", blocker_code="profile_mismatch")
        if isolated_fallback_chain(child_chain_text) != []:
            return LaunchResult(False, profile_id, None, attest_cmd, None, "child fallback chain is not empty", blocker_code="fallback_unenforced")
        # Flags verified offline in hermes_cli/_parser.py: -p, --provider, -m, -z.
        cmd = [self.hermes_bin, "-p", profile_id, "--provider", provider, "-m", model, "-z", prompt]
        env = self._env()
        env["HERMES_HOME"] = str(isolated_root)
        env["NOESIS_FALLBACK_POLICY"] = "deny"
        env["NOESIS_APPROVED_PROVIDER"] = provider
        env["NOESIS_APPROVED_MODEL"] = model
        env["NOESIS_TASK_ID"] = task_id
        env["NOESIS_REQUESTED_PROFILE"] = profile_id
        try:
            proc = self._run(cmd, timeout=effective_timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired as exc:
            # Close the budget even on timeout so a repeat launch of the same
            # attempt is refused (F5). Record the failed attempt first.
            _close_credential_budget(provider=provider, model=model, task_id=task_id)
            timed_out_pid = getattr(exc, "pid", None)
            if timed_out_pid is None and self.process_runner is not None:
                timed_out_pid = getattr(self.process_runner, "active_pid", None)
            if getattr(exc, "cleanup_confirmed", None) is None:
                cleanup = self.cancel_managed(
                    handle={"pid": timed_out_pid, "pgid": timed_out_pid},
                    workspace=Path(workspace) if workspace else Path(cwd or "."),
                )
            else:
                confirmed = bool(getattr(exc, "cleanup_confirmed"))
                cleanup = {
                    "cleanup_state": "terminated" if confirmed else "uncertain",
                    "workspace_reusable": confirmed,
                }
            process_id = timed_out_pid if isinstance(timed_out_pid, int) and not isinstance(timed_out_pid, bool) else None
            # Preserve whatever the child emitted before the deadline: captured
            # stdout keeps its own identity, and any partial candidate artifact
            # references it reported are retained (collection failure is explicit).
            partial_stdout = getattr(exc, "stdout", None) or ""
            if isinstance(partial_stdout, bytes):
                partial_stdout = partial_stdout.decode("utf-8", "replace")
            partial_stdout = redact(str(partial_stdout))
            stdout_digest = hashlib.sha256(partial_stdout.encode("utf-8")).hexdigest() if partial_stdout else None
            timeout_workspace = Path(workspace) if workspace else None
            timeout_artifacts, timeout_digest, timeout_collection_error = self._collect_artifacts(
                partial_stdout, timeout_workspace
            )
            return LaunchResult(
                False,
                profile_id,
                attestation.loaded_profile if attestation else None,
                cmd,
                attestation,
                f"process deadline exceeded: {exc}",
                provider=provider,
                model=model,
                timeout_s=effective_timeout,
                artifacts=tuple(timeout_artifacts),
                candidate_digest=timeout_digest,
                accepted=False,
                verification_pending=False,
                blocker_code="timeout",
                cleanup_state=cleanup["cleanup_state"],
                process_id=process_id,
                workspace_reusable=bool(cleanup["workspace_reusable"]),
                model_driven_canary=False,
                task_id=task_id,
                attempt_id=attempt_id,
                contract_revision=contract_revision,
                assignment_epoch=assignment_epoch,
                baseline=baseline,
                stdout_digest=stdout_digest,
                captured_stdout=partial_stdout,
                collection_error=timeout_collection_error,
            )
        _close_credential_budget(provider=provider, model=model, task_id=task_id)
        stdout = redact(getattr(proc, "stdout", "") or "")
        stderr = redact(getattr(proc, "stderr", "") or "")
        stdout_digest = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        workspace_path = Path(workspace) if workspace else None
        artifacts, verified_digest, artifact_error = self._collect_artifacts(stdout, workspace_path)
        if getattr(proc, "returncode", 1) != 0:
            # Nonzero exit: preserve whatever candidate artifacts and captured
            # stdout were available, and record any collection failure explicitly.
            # A nonzero exit is never accepted nor verification-pending.
            return LaunchResult(
                False,
                profile_id,
                attestation.loaded_profile,
                cmd,
                attestation,
                f"child exited {getattr(proc, 'returncode', 1)}: {(stderr + stdout).strip()}",
                provider=provider,
                model=model,
                timeout_s=effective_timeout,
                artifacts=tuple(artifacts),
                candidate_digest=verified_digest,
                disposition="nonzero_exit",
                accepted=False,
                verification_pending=False,
                blocker_code="child_failed",
                task_id=task_id,
                attempt_id=attempt_id,
                contract_revision=contract_revision,
                assignment_epoch=assignment_epoch,
                baseline=baseline,
                stdout_digest=stdout_digest,
                captured_stdout=stdout,
                collection_error=artifact_error,
                dry_run=False,
            )
        if artifact_error:
            _close_credential_budget(provider=provider, model=model, task_id=task_id)
            return self._blocked(profile_id, cmd, attestation, artifact_error, "artifact evidence rejected")
        return LaunchResult(
            True,
            profile_id,
            attestation.loaded_profile,
            cmd,
            attestation,
            provider=provider,
            model=model,
            timeout_s=effective_timeout,
            artifacts=tuple(artifacts),
            disposition="exited",
            verification_pending=verified_digest is not None,
            accepted=False,
            process_id=getattr(proc, "pid", None),
            candidate_digest=verified_digest,
            usage_tokens=None,
            model_driven_canary=False,
            task_id=task_id,
            attempt_id=attempt_id,
            contract_revision=contract_revision,
            assignment_epoch=assignment_epoch,
            baseline=baseline,
            stdout_digest=stdout_digest,
            captured_stdout=stdout,
            collection_error=artifact_error,
        )

    def _collect_artifacts(self, stdout: str, workspace: Path | None) -> tuple[list[str], str | None, str | None]:
        """Collect candidate artifact references and compute their verified digest.

        Returns ``(kept_names, verified_digest, error)``. ``verified_digest`` is
        the sha256 of the collected artifact bytes, or ``None`` when no candidate
        artifact was collected (never a fabricated digest). A claimed digest that
        disagrees with the collected bytes, an escaped reference, or a malformed
        payload is reported as an explicit error.
        """
        try:
            payload = json.loads(stdout) if stdout.strip().startswith("{") else None
        except json.JSONDecodeError:
            return [], None, "malformed_evidence"
        if not isinstance(payload, dict) or "artifacts" not in payload:
            return [], None, None
        if workspace is None:
            return [], None, "malformed_evidence"
        kept: list[str] = []
        for item in payload.get("artifacts") or []:
            resolved = workspace_contains(workspace, str(item))
            if resolved is None:
                return [], None, "artifact_escape"
            kept.append(str(item))
        if not kept:
            # An empty artifact list is "no candidate", not a candidate.
            return [], None, None
        actual = hashlib.sha256(b"".join(Path(workspace, name).read_bytes() for name in kept)).hexdigest()
        claimed = payload.get("digest")
        if claimed:
            if str(claimed).replace("sha256:", "") != actual:
                return kept, None, "malformed_evidence"
        return kept, actual, None

    def cancel_managed(self, *, handle: dict[str, Any], workspace: Path | str) -> dict[str, Any]:
        """Stop a managed process group. Uncertain termination quarantines the workspace.

        Partial artifacts are retained. ``cleanup_state`` is ``terminated`` only
        when the group signal is confirmed gone.
        """
        root = Path(workspace)
        group_handle = dict(handle)
        if group_handle.get("pgid") is None and group_handle.get("pid") is not None:
            group_handle["pgid"] = group_handle.get("pid")
        runner = self.process_runner
        if runner is not None and hasattr(runner, "terminate_group"):
            confirmed = bool(runner.terminate_group(group_handle))
        elif runner is not None and hasattr(runner, "terminate"):
            confirmed = bool(runner.terminate(group_handle))
        else:
            confirmed = self._signal_process_group(group_handle.get("pgid"))
        return {
            "cleanup_state": "terminated" if confirmed else "uncertain",
            "workspace_reusable": bool(confirmed),
            "partial_artifacts_retained": root.exists(),
            "handle": group_handle,
        }
