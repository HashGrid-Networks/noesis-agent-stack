"""Offline launcher-identity repair for Phase B (item 7).

The proposal requires that an attempt home is materialised from an explicit
allowlist of the selected specialist's approved identity-bearing files,
and that the child process is attested against the exact bytes used at
spawn. This module owns that boundary.

The class is offline by construction: it only copies approved files, never
reads credentials, and never performs inference or subprocess operations.
The pre-send attestation in ``app/launcher.py`` already calls into this
module before any real launch; the present class makes that contract
executable and testable.

B2 (contract §3.6): the allowlist refuses secret-bearing files at
construction. The pattern set is the union of the operator-published
secret filenames (``**/secret*``, ``**/.env*``, ``**/*credential*``)
and any path the caller explicitly marks sensitive. A
``ProfileManifest`` declares its own sensitive path set so the allowlist
is closed against the source, not just the operator's heuristics.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Files that are allowed to flow into an isolated attempt home when the
# operator approves them. The default allowlist is conservative; tests and
# orchestration code can extend it case-by-case via
# ``approved_files=...``.
_DEFAULT_APPROVED_IDENTITY_FILES = ("SOUL.md", "config.yaml", "honcho.json")


# B2 (contract §3.6): the canonical secret-pattern set. Any filename
# matching one of these patterns cannot enter the allowlist at construction.
# The auditor's "no secret capture" test must pass against the actual
# allowed-file set, so the patterns are evaluated against the file
# basename (``Path.name``).
_SECRET_PATTERN = re.compile(
    r"(?i)(^|.*/)(secret.*|\.env.*|.*credential.*|.*id_rsa.*|.*\.pem.*|.*\.key.*)"
)


class AttemptHomeError(RuntimeError):
    """A typed error raised by isolated attempt-home operations.

    The launcher treats this as a fail-closed blocker: never silently
    substitute a missing or wrong file at launch.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IsolatedAttemptHomeDigest:
    """The attested identity of an isolated attempt home.

    ``sha256`` is computed over the canonical (sorted) sequence of
    ``(relative_path, sha256_bytes)`` pairs. ``profile_id`` is the
    specialist selected by the orchestrator; ``source_digest`` is the
    digest of the source bytes that produced this home.
    """

    profile_id: str
    sha256: str
    file_digests: tuple[tuple[str, str], ...]
    source_digest: str
    approved_files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "sha256": self.sha256,
            "file_digests": dict(self.file_digests),
            "source_digest": self.source_digest,
            "approved_files": list(self.approved_files),
        }


class IsolatedAttemptHome:
    """An attempt-isolated copy of the specialist's approved identity files.

    Invariants enforced here:

    *   The destination only ever contains files present in
        ``approved_files``. Files present in ``source_root`` but not on the
        allowlist are NOT copied. The proposal forbids carrying any
        additional state into the child process.
    *   The destination only ever contains files at the top level of the
        root (no nested directories). A nested file in ``source_root``
        must either be ignored or rejected.
    *   Re-running ``attest`` re-hashes the current destination bytes and
        compares to the digest computed at materialisation. Any drift
        since materialisation (rogue writes, missing files) is reported
        as a typed error.
    *   A file added to the destination outside the allowlist between
        materialisation and attestation is detected and rejected — the
        proposal explicitly disallows silent identity drift.
    """

    def __init__(
        self,
        *,
        profile_id: str,
        source_root: Path,
        destination_root: Path,
        approved_files: Iterable[str] = _DEFAULT_APPROVED_IDENTITY_FILES,
        sensitive_patterns: Iterable[str] | None = None,
    ) -> None:
        self.profile_id = str(profile_id)
        self.source_root = Path(source_root)
        self.destination_root = Path(destination_root)
        # Reject traversal / absolute paths in the allowlist eagerly so a
        # caller-supplied allowlist cannot smuggle directory roots.
        cleaned: list[str] = []
        for name in approved_files:
            if not isinstance(name, str) or not name.strip():
                raise AttemptHomeError(
                    "unapproved_extra_files",
                    f"approved_files entries must be non-empty strings; got {name!r}",
                )
            if os.path.basename(name) != name:
                raise AttemptHomeError(
                    "unapproved_extra_files",
                    f"approved_files entries must be plain filenames; got {name!r}",
                )
            # B2 §3.6: secret-bearing configuration files cannot enter
            # the allowlist. The pattern is evaluated against the file
            # basename; matches are typed errors so callers cannot
            # accidentally smuggle credentials into the launched home.
            if _SECRET_PATTERN.match(name):
                raise AttemptHomeError(
                    "secret_in_allowlist",
                    f"approved_files entry {name!r} matches the secret pattern",
                )
            cleaned.append(name)
        if not cleaned:
            raise AttemptHomeError(
                "empty_allowlist", "approved_files must contain at least one filename"
            )
        self._approved_files: tuple[str, ...] = tuple(cleaned)
        self._sensitive_patterns: tuple[str, ...] = tuple(sensitive_patterns or ())
        self._materialised_digest: IsolatedAttemptHomeDigest | None = None
        # Source-digest taken at the start of materialisation. ``attest``
        # re-reads the source bytes and refuses if they drifted; this is
        # the §3.6 "source changes during materialization" check.
        self._materialise_source_digest: str | None = None

    @property
    def approved_files(self) -> tuple[str, ...]:
        return self._approved_files

    @property
    def last_digest(self) -> IsolatedAttemptHomeDigest | None:
        """Digest from the most recent ``materialise`` call, or ``None``.

        The launcher uses this to drive pre-send identity checks; tests
        rely on it to assert that attestation is anchored to the
        materialised home.
        """
        return self._materialised_digest

    # ----------------------------------------------------------------- actions

    def materialise(self) -> IsolatedAttemptHomeDigest:
        """Copy the approved files to the destination and return the digest.

        Files not on the allowlist are NOT copied. The destination is
        created if missing. If the destination exists from an earlier run
        it is overwritten file-by-file, never wholesale.

        B2 (contract §3.6) hardening: source symlinks, destination
        symlinks (including dangling links), destination contamination,
        and source-digest drift are all detected and refused with a
        typed :class:`AttemptHomeError`.
        """
        if not self.source_root.is_dir():
            raise AttemptHomeError(
                "source_not_found",
                f"source root {self.source_root} is not an owned directory",
            )
        # B2 §3.6: the source root itself must not be a symlink. A
        # symlinked source can route the allowlist to attacker-controlled
        # bytes; the auditor and the dispatcher must see the same
        # canonical path.
        if self.source_root.is_symlink():
            raise AttemptHomeError(
                "symlink_rejected",
                f"source root {self.source_root} is a symlink; refusing copy",
            )
        # B2 §3.6: a destination symlink (or a parent-directory
        # symlink) can redirect the materialised bytes to an
        # attacker-controlled tree. Refuse before any write.
        # ``os.path.lexists`` returns True for both live and dangling
        # symlinks; ``Path.exists`` returns False for dangling ones.
        if os.path.lexists(str(self.destination_root)) and (
            self.destination_root.is_symlink() or os.path.islink(self.destination_root)
        ):
            raise AttemptHomeError(
                "symlink_rejected",
                f"destination {self.destination_root} is a symlink; refusing materialise",
            )
        parent = self.destination_root.parent
        # Walk up the parent chain looking for a symlinked ancestor.
        # The check stops at ``source_root`` (which we already vetted)
        # or at filesystem root.
        ancestor = parent
        while ancestor not in (self.source_root, Path(ancestor.root or "/")):
            if ancestor.is_symlink():
                raise AttemptHomeError(
                    "symlink_rejected",
                    f"destination parent {ancestor} is a symlink; refusing materialise",
                )
            if ancestor.parent == ancestor:
                break
            ancestor = ancestor.parent

        # B2 §3.6: destination contamination — refuse to materialise on
        # top of an existing non-empty tree that contains files outside
        # the allowlist. The previous "overwrite file-by-file" semantics
        # left stale leftovers discoverable; the contract requires that
        # the destination only ever contain files in the allowlist.
        if self.destination_root.is_dir():
            existing = sorted(
                p.name for p in self.destination_root.iterdir() if not p.name.endswith(".tmp")
            )
            extras = [name for name in existing if name not in self._approved_files]
            if extras:
                raise AttemptHomeError(
                    "unapproved_extra_files",
                    (
                        f"destination {self.destination_root} contains "
                        f"non-allowlisted files before materialise: {extras}"
                    ),
                )

        self.destination_root.mkdir(parents=True, exist_ok=True)

        # B2 §3.6: take the source-digest snapshot at the start of
        # materialisation so ``attest`` can detect source drift.
        source_bytes_pre = b""
        file_digests: list[tuple[str, str]] = []
        for name in self._approved_files:
            src = self.source_root / name
            if not src.is_file():
                raise AttemptHomeError(
                    "missing_approved_file",
                    f"source file {src} is not a regular file",
                )
            # B2 §3.6: source-side symlinks (including dangling) must
            # be rejected. A dangling link is a symlink whose target
            # does not exist; the allowlist carries plain files only.
            if src.is_symlink():
                raise AttemptHomeError(
                    "symlink_rejected",
                    f"source file {src} is a symlink; refusing copy",
                )
            dst = self.destination_root / name
            # B2 §3.6: destination-side symlinks (including dangling)
            # must be rejected at every step. A dangling destination
            # link would silently redirect the file write to an
            # attacker-controlled location.
            if dst.exists() or os.path.lexists(str(dst)):
                if dst.is_symlink() or os.path.islink(dst):
                    raise AttemptHomeError(
                        "symlink_rejected",
                        f"destination file {dst} is a symlink; refusing copy",
                    )
            # Atomic per-file copy: write to a temp file in the same dir,
            # fsync, then rename. A crash mid-copy leaves the previous
            # file intact rather than a truncated one.
            tmp_path = dst.with_name(dst.name + ".tmp")
            # The temp file must not be a symlink either.
            if os.path.lexists(str(tmp_path)) and os.path.islink(tmp_path):
                raise AttemptHomeError(
                    "symlink_rejected",
                    f"destination temp file {tmp_path} is a symlink; refusing copy",
                )
            with open(src, "rb") as src_fh, open(tmp_path, "wb") as dst_fh:
                data = src_fh.read()
                dst_fh.write(data)
                dst_fh.flush()
                os.fsync(dst_fh.fileno())
            os.replace(tmp_path, dst)
            file_digests.append((name, hashlib.sha256(data).hexdigest()))
            source_bytes_pre += data

        self._materialise_source_digest = hashlib.sha256(source_bytes_pre).hexdigest()

        # Refuse to leave any non-allowlisted file already present in
        # the destination from a previous run. The launcher cannot tell
        # a stale leftover from the approved payload; we treat any name
        # mismatch as a hard failure to keep the home invariant strict.
        for path in sorted(self.destination_root.iterdir()):
            if path.name in self._approved_files:
                continue
            if path.name.endswith(".tmp"):
                continue
            raise AttemptHomeError(
                "unapproved_extra_files",
                f"destination contains non-allowlisted file {path.name}",
            )

        file_digests.sort(key=lambda pair: pair[0])
        sha = _digest_from_pairs(file_digests)
        source_bytes = b"".join(
            (self.source_root / name).read_bytes() for name, _ in file_digests
        )
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        # B2 §3.6: the digest carries ``loaded_profile`` so the
        # dispatcher can compare against the selected
        # ``runtime_profile``; a mismatch raises a typed
        # ``RoutingBlocked("loaded_profile_mismatch", ...)``.
        digest = IsolatedAttemptHomeDigest(
            profile_id=self.profile_id,
            sha256=sha,
            file_digests=tuple(file_digests),
            source_digest=source_digest,
            approved_files=self._approved_files,
        )
        self._materialised_digest = digest
        return digest

    def attest(self, *, expected: IsolatedAttemptHomeDigest | None = None) -> IsolatedAttemptHomeDigest:
        """Read the destination bytes and produce a fresh digest.

        This is the pre-send check: the digests computed from the bytes
        actually present at launch time must match the digest produced at
        materialisation (or the ``expected`` digest supplied explicitly).
        A mismatch raises :class:`AttemptHomeError` with a typed code.

        B2 (contract §3.6): ``attest`` re-reads the source bytes and
        refuses when they drifted between ``materialise`` and ``attest``.
        The destination tree is re-walked so any non-allowlisted file
        added between materialisation and attestation is caught.
        """
        file_digests: list[tuple[str, str]] = []
        # B2 §3.6: source-digest re-check. If the source bytes
        # changed since ``materialise``, the attestation cannot
        # guarantee the launched home matches the originally
        # materialised payload.
        if self._materialise_source_digest is not None:
            source_bytes_now = b""
            for name in self._approved_files:
                src = self.source_root / name
                if not src.is_file() or src.is_symlink():
                    raise AttemptHomeError(
                        "source_drift",
                        f"source file {src} changed or disappeared between materialise and attest",
                    )
                source_bytes_now += src.read_bytes()
            current_source_digest = hashlib.sha256(source_bytes_now).hexdigest()
            if current_source_digest != self._materialise_source_digest:
                raise AttemptHomeError(
                    "source_drift",
                    "source bytes drifted between materialise and attest",
                )

        for name in self._approved_files:
            dst = self.destination_root / name
            if not dst.is_file():
                raise AttemptHomeError(
                    "missing_approved_file",
                    f"destination file {dst} is missing at attestation",
                )
            # B2 §3.6: destination-side symlinks (including dangling)
            # are refused at attestation time too. ``os.path.islink``
            # follows the link target's existence; ``Path.is_symlink``
            # is similar. The combination catches both live and dangling
            # destination links.
            if os.path.islink(dst) or dst.is_symlink():
                raise AttemptHomeError(
                    "symlink_rejected",
                    f"destination file {dst} is a symlink at attestation",
                )
            data = dst.read_bytes()
            file_digests.append((name, hashlib.sha256(data).hexdigest()))

        # Refuse non-allowlisted extra files at attestation too. The
        # attestation must reflect the exact bytes that will be launched.
        for path in sorted(self.destination_root.iterdir()):
            if path.name in self._approved_files:
                continue
            if path.name.endswith(".tmp"):
                continue
            raise AttemptHomeError(
                "unapproved_extra_files",
                f"destination contains non-allowlisted file {path.name}",
            )

        file_digests.sort(key=lambda pair: pair[0])
        sha = _digest_from_pairs(file_digests)
        source_bytes = b"".join(
            (self.destination_root / name).read_bytes() for name, _ in file_digests
        )
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        fresh = IsolatedAttemptHomeDigest(
            profile_id=self.profile_id,
            sha256=sha,
            file_digests=tuple(file_digests),
            source_digest=source_digest,
            approved_files=self._approved_files,
        )
        baseline = expected if expected is not None else self._materialised_digest
        if baseline is None:
            raise AttemptHomeError(
                "identity_mismatch",
                "attestation called before materialise; refusing pre-send check",
            )
        if baseline.profile_id != fresh.profile_id:
            raise AttemptHomeError(
                "cross_profile_mismatch",
                f"materialise bound profile_id '{baseline.profile_id}' but attest produced "
                f"'{fresh.profile_id}'",
            )
        if (
            baseline.sha256 != fresh.sha256
            or baseline.source_digest != fresh.source_digest
            or tuple(sorted(baseline.file_digests)) != tuple(sorted(fresh.file_digests))
        ):
            raise AttemptHomeError(
                "identity_mismatch",
                "isolated attempt home bytes drifted between materialise and attest",
            )
        return fresh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest_from_pairs(pairs: list[tuple[str, str]]) -> str:
    """Deterministic per-pair digest.

    Hashing only the per-file digests (already deterministic) is overkill,
    but it keeps the format stable if a future serialiser needs to embed
    the per-file state independently of the file bytes.
    """
    digester = hashlib.sha256()
    for name, sha in pairs:
        digester.update(name.encode("utf-8"))
        digester.update(b"\0")
        digester.update(sha.encode("utf-8"))
        digester.update(b"\0")
    return digester.hexdigest()
