# ADR 0001: Descriptor authority and canonicalization

- Status: accepted
- Date: 2026-07-18

## Context

Runtime discovery can prove that an implementation is present, but it cannot
grant permissions or execution authority. Descriptor hashes must also be
identical in Core, command-line tooling, and provider SDKs without changing
semantic whitespace in Wolfram, LaTeX, or other tool strings.

## Decision

- A Git-reviewed descriptor bundle is the sole Phase 3 authority source.
- Core stores the exact accepted source commit, immutable parsed descriptor,
  RFC 8785 canonical bytes, SHA-256 hash, import outcome, and lifecycle audit.
- JSON is decoded from UTF-8 bytes with duplicate-key and non-finite-number
  rejection before model construction.
- Descriptor schemas use the named
  `json-schema-2020-12-superlily-v1` profile. The profile has explicit size,
  depth, item, property, reference, and expansion limits; unknown keywords,
  ambiguous unions, remote/dynamic references, cycles, executable formats, and
  unbounded containers are rejected.
- RFC 8785 serialization is provided by the pinned `rfc8785` package. Draft
  2020-12 conformance checking and instance validation use the pinned
  `jsonschema` package only after the stricter Superlily profile passes.
- Descriptor authority models do not inherit ingestion's global whitespace
  stripping. Only explicitly documented identifier fields are normalized.
- Cosmetic changes are authority changes until a later versioned exclusion
  contract is approved.

## Consequences

Equivalent object-key order produces the same hash, while array order and
string whitespace remain meaningful. Provider inventory can match an exact
descriptor version/hash but cannot create or edit one. Dependency upgrades
require refreshed golden vectors and the complete dual-database test suite.

## Required evidence

- Shared accepted/rejected raw JSON vectors.
- Exact canonical bytes and SHA-256 vectors.
- Duplicate-key, non-finite, remote-ref, cycle, unknown-keyword, limit, and
  semantic-whitespace regression tests.
