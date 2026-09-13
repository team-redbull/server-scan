"""
`app.domain.value_objects.gpu_catalog` — the PID/model-string lookup that
fills in GPU VRAM where the vendor API reports none (ADR-0021).
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.value_objects.gpu_catalog import (
    GpuCatalog,
    GpuCatalogConfigurationError,
    _built_in_definitions,
    _normalize,
    gpu_catalog,
)

pytestmark = pytest.mark.unit

_SPEC = "P1001-200:NVIDIA A100 40GB:40,P1010-200:NVIDIA H100 80GB:80"

_BUILT_IN_ROWS = len(_built_in_definitions())


class TestNormalize:
    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("NVIDIA A100-PCIE-40GB", "A100PCIE40GB"),
            ("A100 PCIe 40GB", "A100PCIE40GB"),
            ("  nvidia   a100_pcie_40gb  ", "A100PCIE40GB"),
            ("Tesla T4", "T4"),
            ("NVIDIA Tesla T4", "T4"),
            ("AMD Instinct MI300X", "INSTINCTMI300X"),
            ("UCSC-GPU-A100", "UCSCGPUA100"),
            ("P1001-200", "P1001200"),
        ],
    )
    def test_spellings_that_name_one_card_collapse_to_one_key(
        self, identifier: str, expected: str
    ) -> None:
        assert _normalize(identifier) == expected

    def test_a_string_of_nothing_but_vendor_words_normalizes_to_nothing(self) -> None:
        """`_for_identifier` treats `""` as no match rather than as a key,
        so a GPU whose model a BMC reported as bare `"NVIDIA"` cannot
        collide with an equally empty catalog entry.
        """
        assert _normalize("NVIDIA") == ""
        assert _normalize(" - ") == ""


class TestBuiltInTable:
    def test_no_two_rows_claim_the_same_normalized_key(self) -> None:
        """A key on two rows would make `enrich()`'s answer depend on
        table order — silently right for one card and wrong for another.
        """
        keys = [key for definition in _built_in_definitions() for key in definition.keys]
        assert [key for key, count in Counter(keys).items() if count > 1] == []

    def test_every_row_has_a_positive_vram_and_at_least_one_key(self) -> None:
        for definition in _built_in_definitions():
            assert definition.memory_bytes > 0, definition.name
            assert definition.keys, definition.name


class TestFromSpec:
    def test_an_empty_spec_is_the_built_in_table_not_an_empty_catalog(self) -> None:
        """ADR-0021's reversal: an unconfigured deployment recognizes the
        cards this repo ships, which is the point of shipping them.
        """
        catalog = GpuCatalog.from_spec("")
        assert len(catalog.definitions) == _BUILT_IN_ROWS

    def test_parses_pid_name_and_vram(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        a100 = next(d for d in catalog.definitions if d.pid == "P1001-200")
        assert a100.name == "NVIDIA A100 40GB"
        assert a100.memory_bytes == 40 * 1024**3

    def test_configured_entries_are_added_to_the_built_in_table(self) -> None:
        """Neither PID in `_SPEC` collides with a built-in row, so both
        are additions, not overrides.
        """
        catalog = GpuCatalog.from_spec(_SPEC)
        assert len(catalog.definitions) == _BUILT_IN_ROWS + 2

    def test_whitespace_around_entries_and_fields_is_trimmed(self) -> None:
        catalog = GpuCatalog.from_spec(" P1001-200 : NVIDIA A100 40GB : 40 ")
        assert catalog.definitions[0].pid == "P1001-200"
        assert catalog.definitions[0].name == "NVIDIA A100 40GB"

    def test_blank_entries_between_commas_are_skipped(self) -> None:
        catalog = GpuCatalog.from_spec("P1001-200:NVIDIA A100 40GB:40,,")
        assert len(catalog.definitions) == _BUILT_IN_ROWS + 1

    @pytest.mark.parametrize(
        "entry",
        [
            "P1001-200",
            "P1001-200:NVIDIA A100 40GB",
            "P1001-200:NVIDIA A100 40GB:40:extra",
        ],
    )
    def test_wrong_number_of_colon_parts_is_rejected(self, entry: str) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="PID:Friendly Name:VRAM_GB"):
            GpuCatalog.from_spec(entry)

    def test_empty_pid_is_rejected(self) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="names no PID"):
            GpuCatalog.from_spec(":NVIDIA A100 40GB:40")

    def test_a_pid_of_nothing_but_vendor_words_is_rejected(self) -> None:
        """It normalizes to the empty key, which matches nothing — so it
        would be a silently dead entry rather than a loud one.
        """
        with pytest.raises(GpuCatalogConfigurationError, match="could never match"):
            GpuCatalog.from_spec("NVIDIA:NVIDIA A100 40GB:40")

    def test_empty_name_is_rejected(self) -> None:
        """Unlike a site code, there is no way to derive a friendly GPU
        name from a bare PID, so this cannot fall back the way
        `INVENTORY_SITES`' display-name half does.
        """
        with pytest.raises(GpuCatalogConfigurationError, match="names no friendly name"):
            GpuCatalog.from_spec("P1001-200::40")

    @pytest.mark.parametrize("vram", ["forty", "40.5", "0", "-40", ""])
    def test_non_positive_whole_vram_is_rejected(self, vram: str) -> None:
        with pytest.raises(GpuCatalogConfigurationError):
            GpuCatalog.from_spec(f"P1001-200:NVIDIA A100 40GB:{vram}")

    def test_duplicate_pid_is_rejected(self) -> None:
        with pytest.raises(GpuCatalogConfigurationError, match="listed twice"):
            GpuCatalog.from_spec("P1001-200:NVIDIA A100 40GB:40,P1001-200:NVIDIA A100 40GB:40")

    def test_duplicate_pid_check_is_case_and_separator_insensitive(self) -> None:
        """`enrich()` matches on the normalized key, so the same PID typed
        differently is an ambiguous duplicate and must be rejected.
        """
        with pytest.raises(GpuCatalogConfigurationError, match="listed twice"):
            GpuCatalog.from_spec("p1001-200:A:40,P1001 200:B:40")


class TestGpuCatalogCache:
    def test_gpu_catalog_is_cached_per_spec(self) -> None:
        assert gpu_catalog(_SPEC) is gpu_catalog(_SPEC)


class TestEnrich:
    def test_fills_in_memory_and_friendly_name_on_a_configured_pid(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "P1001-200", "vendor": "NVIDIA", "memory_bytes": None}

        enriched = catalog.enrich(gpu)

        assert enriched["model"] == "NVIDIA A100 40GB"
        assert enriched["memory_bytes"] == 40 * 1024**3

    def test_match_is_case_insensitive(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "p1001-200", "memory_bytes": None}

        assert catalog.enrich(gpu)["memory_bytes"] == 40 * 1024**3

    def test_a_cisco_pid_still_matches_from_the_built_in_table(self) -> None:
        """The pre-ADR-0021 behaviour, now with no configuration at all:
        `UCSC-GPU-A100` is Cisco's own PID for the 250W 40GB A100
        (C240 M6 spec sheet).
        """
        catalog = GpuCatalog.from_spec("")

        enriched = catalog.enrich({"model": "UCSC-GPU-A100", "memory_bytes": None})

        assert enriched["model"] == "NVIDIA A100 40GB"
        assert enriched["memory_bytes"] == 40 * 1024**3

    @pytest.mark.parametrize(
        ("reported", "expected_name", "expected_gb"),
        [
            # nvidia-smi / iDRAC spelling
            ("NVIDIA A100-PCIE-40GB", "NVIDIA A100 40GB", 40),
            ("NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB", 80),
            # NVIDIA's own datasheet spelling, spaces instead of dashes
            ("A100 80GB PCIe", "NVIDIA A100 80GB", 80),
            # Redfish spelling with the memory technology appended
            ("NVIDIA H100 80GB HBM3", "NVIDIA H100 80GB", 80),
            # vendor prefix present or absent, any case
            ("Tesla T4", "NVIDIA T4 16GB", 16),
            ("t4", "NVIDIA T4 16GB", 16),
            ("NVIDIA L40S", "NVIDIA L40S 48GB", 48),
            ("AMD Instinct MI300X", "AMD Instinct MI300X 192GB", 192),
            ("MI210", "AMD Instinct MI210 64GB", 64),
            # The plainest capacity-qualified spelling, which is what a
            # BMC most often reports and what the first version of this
            # table missed on exactly the two families that need it.
            ("H100 80GB", "NVIDIA H100 80GB", 80),
            ("NVIDIA A100 40GB", "NVIDIA A100 40GB", 40),
            ("A100 80GB", "NVIDIA A100 80GB", 80),
            ("H100 NVL 94GB", "NVIDIA H100 NVL 94GB", 94),
            # Intersight's own `graphics.Card.Model`: the bus word sits
            # *before* the capacity, not after it like every spelling
            # above — confirmed live 2026-09-07 against a real tenant.
            ("NVIDIA T4 PCIe 16GB", "NVIDIA T4 16GB", 16),
        ],
    )
    def test_model_string_spellings_a_vendor_actually_reports(
        self, reported: str, expected_name: str, expected_gb: int
    ) -> None:
        """Dell's iDRAC and HPE's iLO report no Cisco PID at all — the
        model string is the only identifier they give, and they spell one
        card several ways.
        """
        catalog = GpuCatalog.from_spec("")

        enriched = catalog.enrich({"model": reported, "memory_bytes": None})

        assert enriched["model"] == expected_name
        assert enriched["memory_bytes"] == expected_gb * 1024**3

    def test_a10_and_a100_never_cross_match(self) -> None:
        """The reason matching is equality on a normalized key and never
        a substring or prefix test: these differ by one character and by
        3x the VRAM.
        """
        catalog = GpuCatalog.from_spec("")

        assert catalog.enrich({"model": "NVIDIA A10", "memory_bytes": None})["memory_bytes"] == (
            24 * 1024**3
        )
        assert catalog.enrich({"model": "NVIDIA L40", "memory_bytes": None})["memory_bytes"] == (
            48 * 1024**3
        )

    def test_a_form_factor_word_before_the_capacity_still_matches(self) -> None:
        """Intersight puts the bus word between model and capacity (`"NVIDIA T4
        PCIe 16GB"`, live 2026-09-07), which the trailing-only strip missed —
        see ADR-0017's second field pass.
        """
        catalog = GpuCatalog.from_spec("")

        enriched = catalog.enrich({"model": "NVIDIA T4 PCIe 16GB", "memory_bytes": None})

        assert enriched["model"] == "NVIDIA T4 16GB"
        assert enriched["memory_bytes"] == 16 * 1024**3

    def test_a_trailing_wattage_figure_is_stripped_like_marketing_noise(self) -> None:
        """Intersight's own "Model" column, live 2026-09-07: `"NVIDIA T4 PCIe
        16GB 70W"` — a TDP suffix on top of the mid-string form-factor word (ADR-0017).
        """
        catalog = GpuCatalog.from_spec("")

        enriched = catalog.enrich({"model": "NVIDIA T4 PCIe 16GB 70W", "memory_bytes": None})

        assert enriched["model"] == "NVIDIA T4 16GB"
        assert enriched["memory_bytes"] == 16 * 1024**3

    def test_a_bare_model_name_that_shipped_in_two_capacities_matches_nothing(self) -> None:
        """`A100` alone names a 40GB card and an 80GB card. Guessing
        either would be silently wrong for half a fleet, so the table
        only carries capacity-qualified spellings for those families.
        """
        catalog = GpuCatalog.from_spec("")

        for ambiguous in ("NVIDIA A100", "Tesla V100", "NVIDIA H100", "Tesla P100"):
            assert catalog.enrich({"model": ambiguous, "memory_bytes": None}) == {
                "model": ambiguous,
                "memory_bytes": None,
            }

    def test_a_configured_entry_beats_the_built_in_row_it_collides_with(self) -> None:
        catalog = GpuCatalog.from_spec("UCSC-GPU-A100:Reworked A100:48")

        enriched = catalog.enrich({"model": "UCSC-GPU-A100", "memory_bytes": None})

        assert enriched["model"] == "Reworked A100"
        assert enriched["memory_bytes"] == 48 * 1024**3

    def test_an_override_takes_only_the_keys_it_names(self) -> None:
        """Overriding one spelling of the A100 40GB must not delete the
        row's other spellings — an override corrects a key, it does not
        withdraw the card.
        """
        catalog = GpuCatalog.from_spec("UCSC-GPU-A100:Reworked A100:48")

        enriched = catalog.enrich({"model": "NVIDIA A100-PCIE-40GB", "memory_bytes": None})

        assert enriched["memory_bytes"] == 40 * 1024**3

    def test_unknown_identifier_is_returned_unchanged(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "P9999-999", "memory_bytes": None}

        enriched = catalog.enrich(gpu)

        assert enriched == gpu
        assert enriched is not gpu  # still a copy, not the same object

    def test_a_real_reported_memory_value_is_never_overridden(self) -> None:
        """A provider's `None` means unread, not zero; a memory value an API
        actually reports must win over this catalog, never the other way around.
        """
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "NVIDIA A100-PCIE-40GB", "memory_bytes": 12345}

        enriched = catalog.enrich(gpu)

        assert enriched["memory_bytes"] == 12345
        assert enriched["model"] == "NVIDIA A100-PCIE-40GB"

    def test_missing_or_non_string_model_is_handled_without_raising(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        assert catalog.enrich({"memory_bytes": None}) == {"memory_bytes": None}
        assert catalog.enrich({"model": None, "memory_bytes": None}) == {
            "model": None,
            "memory_bytes": None,
        }

    def test_a_model_of_nothing_but_a_vendor_name_matches_nothing(self) -> None:
        catalog = GpuCatalog.from_spec(_SPEC)
        gpu = {"model": "NVIDIA", "memory_bytes": None}

        assert catalog.enrich(gpu) == gpu


class TestRebrandedSkuStrings:
    """OneView reports HPE's own product name and no memory field, so the
    catalog is the only source of VRAM for every HPE card (docs/hpe-collectors.md, "GPUs").
    """

    @pytest.mark.parametrize(
        ("reported", "expected_name", "expected_gb"),
        [
            # The leading vendor word is HPE's, not the chip vendor's,
            # and the trailing noun is marketing.
            ("HPE NVIDIA A100 40GB PCIe Accelerator", "NVIDIA A100 40GB", 40),
            ("HPE NVIDIA A100 80GB SXM4 Accelerator", "NVIDIA A100 80GB", 80),
            ("HPE NVIDIA H100 80GB PCIe Accelerator", "NVIDIA H100 80GB", 80),
            ("HPE NVIDIA H100 NVL 94GB PCIe Accelerator", "NVIDIA H100 NVL 94GB", 94),
            # A capacity appended to a model the table keys on alone.
            ("HPE NVIDIA L40S 48GB PCIe Accelerator", "NVIDIA L40S 48GB", 48),
            ("HPE NVIDIA A30 24GB PCIe Accelerator", "NVIDIA A30 24GB", 24),
            ("HPE NVIDIA A2 16GB PCIe Accelerator Kit", "NVIDIA A2 16GB", 16),
            # `T4` + `16GB` must not be read as `T` + `416GB`, which is
            # why the match works on words rather than the joined key.
            ("HPE NVIDIA Tesla T4 16GB Computational Accelerator", "NVIDIA T4 16GB", 16),
        ],
    )
    def test_hpe_product_names_match_the_card_they_name(
        self, reported: str, expected_name: str, expected_gb: int
    ) -> None:
        catalog = GpuCatalog.from_spec("")

        enriched = catalog.enrich({"model": reported, "memory_bytes": None})

        assert enriched["model"] == expected_name
        assert enriched["memory_bytes"] == expected_gb * 1024**3

    def test_a_capacity_that_disagrees_with_the_table_matches_nothing(self) -> None:
        """The `<model> <N>GB` fallback is accepted only when N GB is that row's
        own VRAM: HPE's 64GB A16 is modelled here as four 16GB GPUs, so it
        matches nothing (docs/hpe-collectors.md, "GPUs").
        """
        catalog = GpuCatalog.from_spec("")

        for wrong in ("HPE NVIDIA A16 64GB PCIe Accelerator", "NVIDIA A10 25GB"):
            assert catalog.enrich({"model": wrong, "memory_bytes": None})["memory_bytes"] is None

    def test_a_network_adapter_is_not_mistaken_for_a_gpu(self) -> None:
        """OneView's `Devices` subresource carries NICs and storage
        controllers in the same shape as GPUs; only `DeviceType` tells
        them apart, and a stray one must never enrich to a card.
        """
        catalog = GpuCatalog.from_spec("")
        adapter = {"model": "HPE Eth 10Gb 2p 521T Adptr", "memory_bytes": None}

        assert catalog.enrich(adapter) == adapter

    def test_a_string_of_nothing_but_vendor_and_noise_words_matches_nothing(self) -> None:
        """A string of only vendor and noise words normalizes to `""`, which must
        never find a row: `_for_identifier` returns early on an empty word list
        and `_definition` drops empty keys. Both are load-bearing.
        """
        catalog = GpuCatalog.from_spec("")

        assert not [d for d in catalog.definitions if "" in d.keys]
        for noise in ("NVIDIA GPU", "HPE Graphics Card", "AMD", "GPU", "Accelerator", ""):
            gpu = {"model": noise, "memory_bytes": None}
            assert catalog.enrich(gpu) == gpu
