# Memory integration contract v1

Status: **frozen for J02 D1 (contract investigation)**. This directory records
what is verified against the pinned upstream, what is not, and what the
integration must never assume. It is not an implementation.

## Upstream pin

| Item | Value |
|---|---|
| Distribution | `hermes-scope-recall` |
| Version | `3.14.1` |
| Commit | `2b640260134565ac7fef3a76644e352a50866533` |
| Licence | MIT |
| Python | `>=3.11,<3.15` |

The pin is the only upstream considered by these contracts. A later version or
a patched build must be recorded with its exact version and commit and
re-assessed before any change here.

## Files

| File | Contract |
|---|---|
| `01-instance-identity.md` | instance binding and PCS namespace mapping |
| `02-source-mapping.md` | PCS experience to upstream source mapping |
| `03-role-and-origin.md` | origin, role and principal authority |
| `04-root-source-dedup.md` | root-source identity and de-duplication |
| `05-delivery-receipt.md` | delivery identity, receipts and retry rules |
| `06-deletion.md` | deletion order, three completion layers, ack handling, receipt proposal, purge semantics |
| `07-budget.md` | estimate units, caps and timeout semantics |
| `08-permissions.md` | maintenance capability boundary |
| `09-error-mapping.md` | upstream error code to PCS classification |
| `enums.json` | machine-checkable field and enum inventory (observed values) |

## Verification status (D1)

The behavioural facts cited in these files come from the J02 G0 evidence set
(`g0-deletion-recovery/` in the Lab and the J02 probe output). Two acceptance
items are **BLOCKED**, not passed:

- **AC-16 blocked**: the pinned commit has no public receipt interface.
  `MemoryManagement` exposes `preview`/`apply`/`forget_session` but no
  `receipt`; the only receipt reader is the transaction-internal
  `tx.deletions.receipt(operation_id)`.
- **AC-07 blocked** for the same reason: an acknowledgement loss cannot be
  resolved through a public interface. Replaying `apply` returns
  `VERSION_CONFLICT` and is never evidence of success.

Therefore J02 delivers **D1 only**. D2 (deletion recovery) requires AC-16 and
the AC-07 recovery branch to pass, which needs the upstream receipt interface
proposed in `06-deletion.md` §4. No adapter skeleton exists; creating one
before D2 is out of scope.

## Rules for consumers

1. These files describe a boundary; they do not grant a capability. J04 owns
   the projection and outbox, J07 owns Context delivery.
2. Upstream state is memory evidence. It is never PCS `Belief.status`; an
   upstream `active` claim does not become a supported PCS Belief.
3. When a fact here conflicts with a measured run at the pin, the measured run
   wins and this directory must be corrected in the same change.
4. No absolute host path, credential, or private instance detail belongs in
   this directory.

## Structure and text-consistency self-check

```text
python -m unittest tests.contract.test_memory_integration_contract -v
```

This is a **contract structure and text-consistency self-check**. Its 23
assertions cover required files, sections, keywords, phrases, enum classes and
cross-file consistency, and it reads only these files without importing the
upstream package. Passing it does not prove any runtime behaviour; behavioural
claims are backed by the probe outputs cited in each contract and in
`docs/development/J02-CONTRACTS.md`.
