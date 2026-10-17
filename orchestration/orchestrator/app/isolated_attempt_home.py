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
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Files that are allowed to flow into an isolated attempt home when the
# operator approves them. The default allowlist is conservative; tests and
# orchestration code can extend it case-by-case via
# ``approved_files=...``.
_DEFAULT_APPROVED_IDENTITY_FILES = ("SOUL.md", "config.yaml", "honcho.json")


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
            cleaned.append(name)
        if not cleaned:
            raise AttemptHomeError(
                "empty_allowlist", "approved_files must contain at least one filename"
            )
        self._approved_files: tuple[str, ...] = tuple(cleaned)
        self._materialised_digest: IsolatedAttemptHomeDigest | None = None

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
        """
        if not self.source_root.is_dir():
            raise AttemptHomeError(
                "source_not_found",
                f"source root {self.source_root} is not an owned directory",
            )
        self.destination_root.mkdir(parents=True, exist_ok=True)

        file_digests: list[tuple[str, str]] = []
        for name in self._approved_files:
            src = self.source_root / name
            if not src.is_file():
                raise AttemptHomeError(
                    "missing_approved_file",
                    f"source file {src} is not a regular file",
                )
            dst = self.destination_root / name
            # Refuse symlinks in the source: identity-bearing files must be
            # plain regular files so a symlink cannot smuggle a different
            # payload into the launched profile.
            if src.is_symlink() or dst.is_symlink() if dst.exists() else src.is_symlink():
                raise AttemptHomeError(
                    "symlink_rejected",
                    f"source file {src} is a symlink; refusing copy",
                )
            # Atomic per-file copy: write to a temp file in the same dir,
            # fsync, then rename. A crash mid-copy leaves the previous
            # file intact rather than a truncated one.
            tmp_path = dst.with_name(dst.name + ".tmp")
            with open(src, "rb") as src_fh, open(tmp_path, "wb") as dst_fh:
                data = src_fh.read()
                dst_fh.write(data)
                dst_fh.flush()
                os.fsync(dst_fh.fileno())
            os.replace(tmp_path, dst)
            file_digests.append((name, hashlib.sha256(data).hexdigest()))

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
        """
        file_digests: list[tuple[str, str]] = []
        for name in self._approved_files:
            dst = self.destination_root / name
            if not dst.is_file():
                raise AttemptHomeError(
                    "missing_approved_file",
                    f"destination file {dst} is missing at attestation",
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
