"""Entity aliases as append-only records (J06 / C13b minimal mechanism).

An alias is **not** a side table.  It is a ``record`` whose ``kind`` is
``entity_alias``: ``records`` is the current-state table, ``record_versions``
is the append-only history, and every proposal / confirmation / revocation is
one more version.  That choice is deliberate:

* the deletion barrier already covers ``record`` targets
  (``store/deletion.py`` maps a record target to every version payload), so an
  alias body can never survive a deletion that removed its record;
* ``record_versions_no_update`` already forbids in-place edits, so history
  cannot be rewritten;
* ``records.kind`` is free text, so this needs **no migration**.

Two rules keep the alias from silently becoming an identity claim:

1. only a ``confirmed`` version is an identity; ``proposed`` resolves to
   ``None``;
2. one alias maps to at most one confirmed canonical entity per namespace.

Aliases never enter ``scope_matches`` / ``scope_contained``.  The matching
rule stays exact; aliases are handled on both sides of it — canonicalised when
a scope is written, expanded when a query is built.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..foundation import identifiers
from ..foundation.errors import InvalidInput, StorageError

ALIAS_KIND = "entity_alias"
ALIAS_SCHEMA_VERSION = "1"

#: The closed status set.  ``revoked`` is terminal for resolution purposes but
#: is kept as a version, never deleted.
ALIAS_STATUSES = ("proposed", "confirmed", "revoked")

_ALIAS_RECORD_NAMESPACE = uuid.UUID("6f1d5a02-8f6c-5b3e-9a44-2d7c1e0b7a10")

_MAX_ALIAS_LENGTH = 128
_MAX_ID_LENGTH = 128
_MAX_TEXT_LENGTH = 512


class AliasConflict(StorageError):
    """The alias already resolves to a different canonical entity."""

    code = "ALIAS_CONFLICT"


class AliasNotFound(StorageError):
    """No alias record exists for the requested alias."""

    code = "NOT_FOUND"


@dataclass(frozen=True)
class AliasVersion:
    """One immutable alias version, as stored."""

    record_id: str
    version: int
    alias: str
    canonical_id: str
    status: str
    basis: str
    decided_by: str
    decided_at: str


def alias_record_id(namespace: str, alias: str) -> str:
    """Deterministic record ID for one alias within one namespace."""

    ns = identifiers.require_namespace(namespace)
    return str(uuid.uuid5(_ALIAS_RECORD_NAMESPACE, f"{ns}\x1f{_require_text(alias, 'alias', _MAX_ALIAS_LENGTH)}"))


def _require_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidInput(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise InvalidInput(f"{field} must not be empty")
    if len(text) > limit:
        raise InvalidInput(f"{field} must be at most {limit} characters")
    return text


def _require_status(value: Any) -> str:
    if value not in ALIAS_STATUSES:
        raise InvalidInput(
            f"alias status must be one of {', '.join(ALIAS_STATUSES)}"
        )
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AliasService:
    """Propose / confirm / revoke / resolve entity aliases for one store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------- writes

    def propose(
        self,
        namespace: str,
        *,
        canonical_id: str,
        alias: str,
        basis: str,
        decided_by: str,
        decided_at: str | None = None,
    ) -> AliasVersion:
        ns = identifiers.require_namespace(namespace)
        canonical = _require_text(canonical_id, "canonical_id", _MAX_ID_LENGTH)
        name = _require_text(alias, "alias", _MAX_ALIAS_LENGTH)
        reason = _require_text(basis, "basis", _MAX_TEXT_LENGTH)
        actor = _require_text(decided_by, "decided_by", _MAX_ID_LENGTH)

        self._store.require_standalone("alias.propose")
        with self._store.transaction():
            current = self._current_locked(ns, name)
            if current is not None:
                if current.status == "confirmed":
                    if current.canonical_id == canonical:
                        return current
                    raise AliasConflict(
                        f"alias {name!r} already resolves to {current.canonical_id!r}"
                    )
                if (
                    current.status == "proposed"
                    and current.canonical_id == canonical
                ):
                    return current
                if current.status == "revoked":
                    raise AliasConflict(
                        f"alias {name!r} was revoked; re-propose needs a new basis"
                    )
            return self._append_locked(
                ns, name, canonical, "proposed", reason, actor, decided_at
            )

    def confirm(
        self,
        namespace: str,
        *,
        alias: str,
        basis: str,
        decided_by: str,
        decided_at: str | None = None,
        canonical_id: str | None = None,
    ) -> AliasVersion:
        ns = identifiers.require_namespace(namespace)
        name = _require_text(alias, "alias", _MAX_ALIAS_LENGTH)
        reason = _require_text(basis, "basis", _MAX_TEXT_LENGTH)
        actor = _require_text(decided_by, "decided_by", _MAX_ID_LENGTH)

        self._store.require_standalone("alias.confirm")
        with self._store.transaction():
            current = self._current_locked(ns, name)
            if current is None:
                raise AliasNotFound(f"no alias record for {name!r}")
            if current.status == "confirmed":
                if canonical_id is None or current.canonical_id == canonical_id:
                    return current
                raise AliasConflict(
                    f"alias {name!r} already resolves to {current.canonical_id!r}"
                )
            if current.status == "revoked":
                raise InvalidInput(f"alias {name!r} is revoked and cannot be confirmed")
            target = current.canonical_id
            if canonical_id is not None:
                requested = _require_text(canonical_id, "canonical_id", _MAX_ID_LENGTH)
                if requested != target:
                    raise AliasConflict(
                        f"alias {name!r} was proposed for {target!r}, not {requested!r}"
                    )
            return self._append_locked(
                ns, name, target, "confirmed", reason, actor, decided_at
            )

    def revoke(
        self,
        namespace: str,
        *,
        alias: str,
        basis: str,
        decided_by: str,
        decided_at: str | None = None,
    ) -> AliasVersion:
        ns = identifiers.require_namespace(namespace)
        name = _require_text(alias, "alias", _MAX_ALIAS_LENGTH)
        reason = _require_text(basis, "basis", _MAX_TEXT_LENGTH)
        actor = _require_text(decided_by, "decided_by", _MAX_ID_LENGTH)

        self._store.require_standalone("alias.revoke")
        with self._store.transaction():
            current = self._current_locked(ns, name)
            if current is None:
                raise AliasNotFound(f"no alias record for {name!r}")
            if current.status == "revoked":
                return current
            return self._append_locked(
                ns, name, current.canonical_id, "revoked", reason, actor, decided_at
            )

    # -------------------------------------------------------------- reads

    def resolve(self, namespace: str, *, alias: str) -> str | None:
        """The canonical id for a **confirmed** alias, else ``None``."""

        ns = identifiers.require_namespace(namespace)
        name = _require_text(alias, "alias", _MAX_ALIAS_LENGTH)
        current = self._current_locked(ns, name)
        if current is None or current.status != "confirmed":
            return None
        return current.canonical_id

    def expand(self, namespace: str, *, aliases: Iterable[str]) -> frozenset[str]:
        """Every entity id a query must name to reach these aliases.

        The literal alias is always included: historical record scopes stored
        the alias form and were never rewritten, so the expanded query has to
        contain both spellings.  Only a confirmed alias contributes its
        canonical id.
        """

        ns = identifiers.require_namespace(namespace)
        out: set[str] = set()
        for raw in aliases:
            name = _require_text(raw, "alias", _MAX_ALIAS_LENGTH)
            out.add(name)
            current = self._current_locked(ns, name)
            if current is not None and current.status == "confirmed":
                out.add(current.canonical_id)
        return frozenset(out)

    def history(self, namespace: str, *, canonical_id: str) -> tuple[AliasVersion, ...]:
        """Every version of every alias that ever pointed at *canonical_id*."""

        ns = identifiers.require_namespace(namespace)
        canonical = _require_text(canonical_id, "canonical_id", _MAX_ID_LENGTH)
        collected: list[AliasVersion] = []
        for version in self._store.current_versions_of_kind(ns, ALIAS_KIND):
            pointer = self._store.record_pointer(ns, version.record_id)
            if pointer is None:  # pragma: no cover - version without pointer
                continue
            for number in range(1, pointer.current_version + 1):
                record = self._store.get_version(ns, version.record_id, number)
                parsed = _alias_version(record)
                if parsed.canonical_id == canonical:
                    collected.append(parsed)
        collected.sort(key=lambda item: (item.alias, item.version))
        return tuple(collected)

    # ------------------------------------------------- scope integration

    def canonicalize_scope(
        self, namespace: str, scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Rewrite ``scope.entity_ids`` aliases to canonical ids (write side)."""

        out = dict(scope)
        entities = out.get("entity_ids")
        if not entities:
            return out
        rewritten: list[str] = []
        seen: set[str] = set()
        for raw in entities:
            resolved = self.resolve(namespace, alias=str(raw)) or str(raw)
            if resolved in seen:
                continue
            seen.add(resolved)
            rewritten.append(resolved)
        out["entity_ids"] = rewritten
        return out

    def expand_query_scope(
        self, namespace: str, scope: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Widen ``scope.entity_ids`` so historical alias scopes still match.

        The matching rule requires ``record scope ⊆ query scope``; expanding
        the *query* keeps that direction and never makes a record visible.
        """

        out = dict(scope)
        entities = out.get("entity_ids")
        if not entities:
            return out
        out["entity_ids"] = sorted(self.expand(namespace, aliases=list(entities)))
        return out

    # ---------------------------------------------------------- internals

    def _current_locked(self, namespace: str, alias: str) -> AliasVersion | None:
        record_id = alias_record_id(namespace, alias)
        pointer = self._store.record_pointer(namespace, record_id)
        if pointer is None:
            return None
        version = self._store.get_version(namespace, record_id, pointer.current_version)
        parsed = _alias_version(version)
        if parsed.alias != alias:  # pragma: no cover - deterministic id collision guard
            raise AliasConflict(
                f"record {record_id} holds alias {parsed.alias!r}, not {alias!r}"
            )
        return parsed

    def _append_locked(
        self,
        namespace: str,
        alias: str,
        canonical_id: str,
        status: str,
        basis: str,
        decided_by: str,
        decided_at: str | None,
    ) -> AliasVersion:
        record_id = alias_record_id(namespace, alias)
        pointer = self._store.record_pointer(namespace, record_id)
        if pointer is None:
            version = 1
        else:
            if pointer.kind != ALIAS_KIND:
                raise AliasConflict(
                    f"record {record_id} is kind {pointer.kind!r}, not an alias"
                )
            version = pointer.current_version + 1
        body = {
            "alias": alias,
            "canonical_id": canonical_id,
            "status": _require_status(status),
            "basis": basis,
            "decided_by": decided_by,
            "decided_at": decided_at or _utc_now(),
        }
        appended = self._store.append_record_version(
            namespace=namespace,
            record_id=record_id,
            record_version=version,
            kind=ALIAS_KIND,
            body=body,
            status="active",
            schema_version=ALIAS_SCHEMA_VERSION,
            scope=None,
        )
        self._store.record_authorization(
            namespace=namespace,
            operation=f"alias.{status}",
            target_kind="record",
            target_id=record_id,
            target_version=version,
            authorization_ref=f"alias:{decided_by}",
        )
        return _alias_version(appended)


def _alias_version(record: Any) -> AliasVersion:
    body = record.body
    return AliasVersion(
        record_id=record.record_id,
        version=record.record_version,
        alias=body["alias"],
        canonical_id=body["canonical_id"],
        status=body["status"],
        basis=body.get("basis", ""),
        decided_by=body.get("decided_by", ""),
        decided_at=body.get("decided_at", ""),
    )


def canonicalize_entity_ids(
    store: Any, namespace: str, scope: Mapping[str, Any]
) -> dict[str, Any]:
    """Convenience wrapper used by the Belief write path."""

    return AliasService(store).canonicalize_scope(namespace, scope)


def expand_query_entity_ids(
    store: Any, namespace: str, scope: Mapping[str, Any]
) -> dict[str, Any]:
    """Convenience wrapper used by the query paths."""

    return AliasService(store).expand_query_scope(namespace, scope)


__all__ = [
    "ALIAS_KIND",
    "ALIAS_SCHEMA_VERSION",
    "ALIAS_STATUSES",
    "AliasConflict",
    "AliasNotFound",
    "AliasService",
    "AliasVersion",
    "alias_record_id",
    "canonicalize_entity_ids",
    "expand_query_entity_ids",
]
