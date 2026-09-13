"""A lookup from a GPU's PID or model string to its known VRAM.

**Fills a gap no management plane this platform collects from closes on
its own.** Intersight's `graphics.Card` and UCS Manager's `graphicsCard`
both report a GPU's identity (model/vendor/serial/PID) but neither
reports memory size or power draw anywhere — confirmed against both
SDKs' full field sets and, for Intersight, Cisco's own official metrics
API too. See docs/cisco-collectors.md, "GPUs (coprocessor cards vs.
graphics cards)". A Redfish-sourced GPU frequently reports no memory
summary either.

Two kinds of identifier reach this catalog, because two kinds of
management plane feed it. Cisco reports a **PID** — its own part-number
scheme (`UCSC-GPU-A100`), stable per SKU regardless of firmware version.
Dell's iDRAC and HPE's iLO report no Cisco PID at all; they report a
**model string** (`NVIDIA A100-PCIE-40GB`, `NVIDIA H100 80GB HBM3`).
Either matches, after normalization — see `GpuCatalog.enrich`.

**This module ships a default table** (`gpu_models.DEFAULT_GPU_MODELS`),
which reverses the original decision to ship none. That decision assumed
a Cisco-only fleet, where the identifier really was operator knowledge
about their own part numbers; a vendor's own model string is not, and an
estate should recognize an A100 without being told what one is. The
operator half is retained where it still earns its place:
`INVENTORY_GPU_MODELS` **overrides** the built-in table rather than
replacing it, so a deployment can correct a row or add a card this
codebase has never heard of, and its answer always wins. See
docs/adr/0021-built-in-gpu-catalog-with-model-matching.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.domain.value_objects.gpu_models import DEFAULT_GPU_MODELS

_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")

_VENDOR_PREFIXES = frozenset({"HPE", "HP", "NVIDIA", "AMD", "INTEL", "TESLA", "QUADRO"})

# Marketing nouns trailing a rebranded SKU ("HPE NVIDIA A100 40GB PCIe
# Accelerator"); dropped from the end only — ADR-0022, "GPU matching".
_TRAILING_NOISE = frozenset(
    {
        "ACCELERATOR",
        "ACCELERATORS",
        "COMPUTATIONAL",
        "GRAPHICS",
        "MODULE",
        "ADAPTER",
        "ADPTR",
        "CARD",
        "KIT",
        "GPU",
    }
)

# A trailing TDP ("NVIDIA T4 16GB 70W", live Intersight 2026-09-07),
# stripped unconditionally — ADR-0017's second field pass says why.
_TRAILING_WATTAGE = re.compile(r"\d+W")


def _normalize(identifier: str) -> str:
    """
    Reduce a PID or model string to the key both sides of a match share.

    Compared for equality only, never as a substring — `A10` can never
    match `A100` (ADR-0021, decision 2).

    Args:
        identifier (str): A Cisco PID or a vendor-reported model string.

    Returns:
        str: The normalized key, or `""` for a string that is nothing but
            vendor words and punctuation — which matches nothing.
    """
    return "".join(_words(identifier))


def _words(identifier: str) -> list[str]:
    """
    Split an identifier into its meaningful words.

    Separate from `_normalize` because `_for_identifier` needs the words:
    only the list can tell `T4` + `16GB` from `T` + `416GB`.

    Args:
        identifier (str): A Cisco PID or a vendor-reported model string.

    Returns:
        list[str]: The remaining words, in order.
    """
    words = [word for word in _SEPARATORS.split(identifier.upper()) if word]
    while words and words[0] in _VENDOR_PREFIXES:
        words.pop(0)
    while words and (words[-1] in _TRAILING_NOISE or _TRAILING_WATTAGE.fullmatch(words[-1])):
        words.pop()
    return words


# Dropped positionally, and only as a lookup fallback — never from a
# table key, since `"H100 PCIe"` is one (ADR-0017, second field pass).
_FORM_FACTOR_WORDS = frozenset({"PCIE", "SXM", "SXM2", "SXM4", "SXM5", "OAM"})

# The `48GB` of `L40S 48GB PCIe`; matched against a bare-model row only
# when the capacity agrees with the row's own VRAM (ADR-0022).
_CAPACITY_WORD = re.compile(r"(\d+)GB")


class GpuCatalogConfigurationError(ValueError):
    """
    `INVENTORY_GPU_MODELS` could not be read.

    Raised at startup, never during a request, so a typo fails loudly.
    """


@dataclass(frozen=True, slots=True)
class GpuModelDefinition:
    """
    One known GPU SKU.

    Attributes:
        pid (str): The identifier this definition is named for — a Cisco
            PID (`"UCSC-GPU-A100"`) for a Cisco-sourced row, or the
            canonical model string for a row no Cisco PID covers.
        name (str): The friendly name to report in place of the raw
            identifier, e.g. `"NVIDIA A100 40GB"`.
        memory_bytes (int): The card's known VRAM, in bytes.
        keys (tuple[str, ...]): Every normalized identifier that matches
            this definition, `pid`'s own included. Precomputed at
            construction — `GpuCatalog.enrich` compares against these, so
            normalizing per lookup would repeat the same work on every
            GPU of every server in the fleet.
    """

    pid: str
    name: str
    memory_bytes: int
    keys: tuple[str, ...] = ()


def _definition(
    pid: str, name: str, memory_bytes: int, identifiers: tuple[str, ...]
) -> GpuModelDefinition:
    """
    Build a definition with its normalized match keys filled in.

    Args:
        pid (str): The identifier the definition is named for.
        name (str): The friendly name to report.
        memory_bytes (int): The card's VRAM, in bytes.
        identifiers (tuple[str, ...]): Every spelling that should match,
            unnormalized.

    Returns:
        GpuModelDefinition: The definition, with duplicate and empty keys
            dropped.
    """
    keys: list[str] = []
    for identifier in identifiers:
        key = _normalize(identifier)
        if key and key not in keys:
            keys.append(key)
    return GpuModelDefinition(pid=pid, name=name, memory_bytes=memory_bytes, keys=tuple(keys))


def _built_in_definitions() -> tuple[GpuModelDefinition, ...]:
    """
    Build the shipped default table.

    Returns:
        tuple[GpuModelDefinition, ...]: One definition per row of
            `gpu_models.DEFAULT_GPU_MODELS`, in table order.
    """
    return tuple(
        _definition(identifiers[0], name, vram_gb * 1024**3, identifiers)
        for name, vram_gb, identifiers in DEFAULT_GPU_MODELS
    )


@dataclass(frozen=True, slots=True)
class GpuCatalog:
    """
    The GPUs this deployment can enrich.

    The built-in table with `INVENTORY_GPU_MODELS` merged over it; immutable, like `SiteCatalog`.
    """

    definitions: tuple[GpuModelDefinition, ...]

    @classmethod
    def from_spec(cls, spec: str) -> GpuCatalog:
        """
        Parse `INVENTORY_GPU_MODELS` (`PID:Friendly Name:VRAM_GB`, comma-separated).

        Configured entries override the built-in table per key; they never
        replace it (ADR-0021, decision 1).

        Args:
            spec (str): The raw configured value. Empty means the
                built-in table unmodified, not an error.

        Returns:
            GpuCatalog: Configured entries first, then the built-in rows
                they did not override.

        Raises:
            GpuCatalogConfigurationError: On a malformed or duplicate
                entry.
        """
        configured = cls._parse(spec)
        overridden = {key for definition in configured for key in definition.keys}
        built_in: list[GpuModelDefinition] = []
        for definition in _built_in_definitions():
            kept = tuple(key for key in definition.keys if key not in overridden)
            if kept:
                built_in.append(
                    GpuModelDefinition(
                        pid=definition.pid,
                        name=definition.name,
                        memory_bytes=definition.memory_bytes,
                        keys=kept,
                    )
                )
        return cls(definitions=(*configured, *built_in))

    @staticmethod
    def _parse(spec: str) -> tuple[GpuModelDefinition, ...]:
        """
        Parse the configured entries alone, without the built-in table.

        Args:
            spec (str): The raw `INVENTORY_GPU_MODELS` value.

        Returns:
            tuple[GpuModelDefinition, ...]: The parsed entries, in
                configured order.

        Raises:
            GpuCatalogConfigurationError: On a malformed or duplicate
                entry.
        """
        text = spec.strip()
        if not text:
            return ()

        definitions: list[GpuModelDefinition] = []
        seen: set[str] = set()
        for entry in text.split(","):
            if not entry.strip():
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: {entry.strip()!r} is not in the required "
                    "'PID:Friendly Name:VRAM_GB' shape — expected exactly two ':' "
                    "separators, e.g. 'P1001-200:NVIDIA A100 40GB:40'."
                )
            pid, name, vram_gb_raw = (part.strip() for part in parts)
            if not pid:
                raise GpuCatalogConfigurationError(
                    "INVENTORY_GPU_MODELS: an entry names no PID — the part before the first ':'."
                )
            if not name:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} names no friendly name — unlike "
                    "INVENTORY_SITES, there is no way to derive one from the PID alone."
                )
            try:
                vram_gb = int(vram_gb_raw)
            except ValueError as exc:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r}'s VRAM {vram_gb_raw!r} is not a "
                    "whole number of GB."
                ) from exc
            if vram_gb <= 0:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r}'s VRAM must be positive, got {vram_gb}."
                )
            key = _normalize(pid)
            if not key:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} is nothing but vendor words and "
                    "punctuation, so it could never match a GPU."
                )
            if key in seen:
                raise GpuCatalogConfigurationError(
                    f"INVENTORY_GPU_MODELS: PID {pid!r} is listed twice."
                )
            seen.add(key)
            definitions.append(_definition(pid, name, vram_gb * 1024**3, (pid,)))
        return tuple(definitions)

    def _for_identifier(self, identifier: str) -> GpuModelDefinition | None:
        """
        Look up one PID or model string.

        Tried in order: exact; minus form-factor words; then `<model><N>GB`
        against a bare-model row, only when N matches its VRAM (ADR-0022).

        Args:
            identifier (str): The PID or model as a provider reported it.

        Returns:
            GpuModelDefinition | None: The matching definition, or `None`.
                Configured entries come first, so one always wins over
                the built-in row it overrides.
        """
        words = _words(identifier)
        if not words:
            return None
        stripped = [word for word in words if word not in _FORM_FACTOR_WORDS]
        candidates = ["".join(words)]
        if stripped != words:
            candidates.append("".join(stripped))
        for candidate in candidates:
            for definition in self.definitions:
                if candidate in definition.keys:
                    return definition

        # The only inexact match, self-validating on capacity: HPE's 64GB
        # A16 (four 16GB GPUs here) finds nothing rather than a wrong number.
        capacity = _CAPACITY_WORD.fullmatch(stripped[-1]) if stripped else None
        if capacity is None:
            return None
        base = "".join(stripped[:-1])
        if not base:
            return None
        wanted = int(capacity.group(1)) * 1024**3
        for definition in self.definitions:
            if base in definition.keys and definition.memory_bytes == wanted:
                return definition
        return None

    def enrich(self, gpu: Mapping[str, Any]) -> dict[str, Any]:
        """
        Fill in a GPU's memory from this catalog, when the API left it unknown.

        A `memory_bytes` a collector read is never overridden; `model` may
        be a Cisco PID or a vendor model string.

        Args:
            gpu (Mapping[str, Any]): One entry from `ProviderServer.gpus`
                — keys mirror `app.domain.models.hardware.Gpu`.

        Returns:
            dict[str, Any]: `gpu` unchanged, or a copy with `model`
                replaced by the friendly name and `memory_bytes` filled
                in, when `memory_bytes` was `None` and `model` matched.
        """
        if gpu.get("memory_bytes") is not None:
            return dict(gpu)
        model = gpu.get("model")
        if not isinstance(model, str):
            return dict(gpu)
        definition = self._for_identifier(model)
        if definition is None:
            return dict(gpu)
        enriched = dict(gpu)
        enriched["model"] = definition.name
        enriched["memory_bytes"] = definition.memory_bytes
        return enriched


@lru_cache(maxsize=8)
def gpu_catalog(spec: str) -> GpuCatalog:
    """
    A cached catalog for one configured spec.

    Keyed on the spec, not `Settings`, so a test can pass a literal.

    Args:
        spec (str): The `INVENTORY_GPU_MODELS` value.

    Returns:
        GpuCatalog: The parsed catalog.

    Raises:
        GpuCatalogConfigurationError: On a malformed entry.
    """
    return GpuCatalog.from_spec(spec)
