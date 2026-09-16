# 21. A built-in GPU catalog, matched by model string as well as PID

Date: 2026-09-04

## Status

Accepted. Reverses the "deliberately not a hardcoded table" half of the
`INVENTORY_GPU_MODELS` design introduced 2026-09-02
(`docs/cisco-collectors.md`, "`INVENTORY_GPU_MODELS` — filling the
`memory_bytes` gap this API leaves"). The configuration mechanism itself
is unchanged and still supported.

## Context

### What the original decision was

`app.domain.value_objects.gpu_catalog` shipped as an empty,
operator-configured lookup from a **Cisco PID** to a GPU's friendly name
and VRAM. Its module docstring argued the case explicitly:

> **Deliberately not a hardcoded table in this repo.** A PID-to-SKU
> mapping is operator knowledge (Cisco's own spec sheets), changes as new
> GPU models ship, and — like `INVENTORY_SITES` — this codebase should
> not assert it as a fact frozen at whatever moment this file was last
> edited.

That reasoning followed ADR-0018's precedent: a closed set whose contents
are an estate's own business belongs in configuration, not in code. It
was sound for the fleet the platform collected from at the time, which
was entirely Cisco.

### Why it no longer holds

Two things changed.

**The fleet stopped being Cisco-only.** `REDFISH_STANDALONE` ships
(ADR-0016) and Dell is collected through iDRAC/Redfish for hardware
(ADR-0020), with HPE next. **Neither reports a Cisco PID at all.** A
Redfish GPU's only identifier is a model string — `NVIDIA
A100-PCIE-40GB`, `NVIDIA H100 80GB HBM3`, `AMD Instinct MI300X`. A
PID-only catalog cannot enrich a single non-Cisco GPU no matter what an
operator configures into it, so on the majority of the fleet the feature
was inert by construction.

**"Operator knowledge" was the wrong description of half the data.**
Which PIDs an estate stocks is genuinely its own business. How much VRAM
an NVIDIA A100 has is not — it is a published, stable fact on NVIDIA's
own datasheet, identical for every estate on earth. Making every
deployment retype the entire NVIDIA and AMD datacenter line-up before it
can report GPU memory is not deference to operator knowledge; it is
withholding a fact the codebase can simply look up. The user's direction
was explicit: the platform should recognise all types of GPU out of the
box.

The argument that survives is the *correction* case — a card this repo
has never heard of, a PID an estate uses differently, a figure that turns
out to be wrong for their SKU. That is why the configuration mechanism is
kept and given precedence, rather than removed.

## Decision

### 1. Ship a default table, overridable by configuration

`gpu_models.DEFAULT_GPU_MODELS` is the built-in table.
`GpuCatalog.from_spec` merges `INVENTORY_GPU_MODELS` **over** it:
configured entries come first and win; every built-in row the operator
did not name survives untouched. An empty `INVENTORY_GPU_MODELS` is now
the built-in table alone, not an empty catalog.

An override takes only the keys it names. Configuring `UCSC-GPU-A100`
replaces that identifier's answer; the same card's other spellings
(`A100-PCIE-40GB`, `A100-SXM4-40GB`) keep the built-in row. An override
corrects an identifier, it does not withdraw a card.

The data lives in its own module, importable with no I/O. The matching
rules change rarely; the table changes every time a card ships. Keeping a
routine "add a row" edit away from the code that decides what a match
*means* is the whole reason for the split.

### 2. Match on model string as well as PID, via one normalized key

Both identifier kinds go through the same `_normalize`:

1. uppercase;
2. split on every non-alphanumeric character;
3. drop leading vendor/brand words (`NVIDIA`, `AMD`, `INTEL`, `TESLA`,
   `QUADRO`), repeatedly;
4. rejoin with no separators.

`NVIDIA A100-PCIE-40GB`, `A100 PCIe 40GB` and `nvidia a100_pcie_40gb` all
become `A100PCIE40GB`. `UCSC-GPU-A100` becomes `UCSCGPUA100`. A PID and a
model string cannot be confused for one another because no vendor's model
string normalizes to a Cisco part number.

**The comparison is equality on that key. Never a substring, prefix or
edit-distance test.** `A10` and `A100` differ by one character and by 3x
the VRAM; `L40` and `L40S` likewise. Any looser rule reports a confidently
wrong number, which is worse than reporting none — this catalog exists
precisely because `None` is the honest answer when nothing is known.
Spellings that survive normalization as genuinely different strings get an
explicit alias on the row, and there are 93 aliases across 30 rows. That
is the entire matching engine.

### 3. A bare model name is a key only where the model shipped in one capacity

`A30`, `A40`, `T4`, `L40S`, `H200`, `MI300X` each name exactly one
capacity, so the bare name is a safe key. `A100`, `V100`, `H100` and
`P100` each shipped in two. Their rows carry only capacity-qualified
spellings, so a GPU reporting a bare `NVIDIA A100` matches **nothing** and
keeps `memory_bytes: None`. Guessing either capacity would be silently
wrong for half of an estate's cards, and silently wrong is the one outcome
this catalog must not produce.

### 4. Existing contracts are unchanged

- A GPU whose `memory_bytes` a collector already read is returned
  untouched. The catalog fills gaps; it never overrides a vendor's own
  measurement (`ProviderServer`'s "a `None` means unread, not zero").
- A non-match returns the entry unchanged, PID or model string intact.
- A malformed `INVENTORY_GPU_MODELS` still fails loudly at startup.
- `IngestService`'s default catalog is now the built-in table rather than
  an empty one, so a collector that is handed no catalog enriches from
  the defaults.

## Sourcing standard for adding a row

**Only add a card whose VRAM you can cite from a primary vendor source.**
An NVIDIA or AMD datasheet or product page, or a Cisco UCS spec sheet for
a PID. Not a wiki, not a reseller listing, not recall. A card nobody can
source is a card the operator configures — that is what
`INVENTORY_GPU_MODELS` is still for, and an absent row costs an estate one
line of configuration while a wrong row costs it a silently incorrect
capacity report it has no reason to distrust.

Deliberately absent for exactly that reason, as of this ADR: NVIDIA B100
(no per-GPU figure found on nvidia.com), GB200 (NVIDIA publishes only the
384 GB per-Superchip total, and halving it is arithmetic, not a stated
figure), Tesla P40 / M10 / M60 (Cisco's spec-sheet rows for the PIDs carry
no memory size and the NVIDIA datasheets did not fetch), and Intel Data
Center GPU Flex.

### Sources used for the shipped table

| Family | Source |
|---|---|
| Tesla V100 16/32GB | `nvidia.com/content/technologies/volta/pdf/tesla-volta-v100-datasheet.pdf`, corroborated by Cisco C240 M5 PIDs `UCSC-GPU-V100` (16GB) / `UCSC-GPU-V100-32` (32GB) |
| T4 | `nvidia.com/.../tesla-t4/t4-tensor-core-datasheet-951643.pdf`; Cisco `UCSC-GPU-T4-16`, `UCSX-GPU-T4-16` ("NVIDIA T4 PCIE 75W 16GB") |
| P100 12/16GB | Cisco C240 M5 spec sheet, PIDs `UCSC-GPU-P100-12G` / `UCSC-GPU-P100-16G` |
| Quadro RTX 6000 / 8000 | Cisco C240 M5 spec sheet, `UCSC-GPU-RTX6000` (24GB) / `UCSC-GPU-RTX8000` (48GB) |
| A2 | `nvidia.com/en-us/data-center/products/a2/` |
| A10 | `nvidia.com/.../a10/pdf/datasheet-new/nvidia-a10-datasheet.pdf`; Cisco `UCSC-GPU-A10` ("TESLA A10, PASSIVE, 150W, 24GB") |
| A16 | `images.nvidia.com/content/Solutions/data-center/vgpu-a16-datasheet.pdf`; Cisco `UCSC-GPU-A16` ("NVIDIA A16 PCIE 250W 4X16GB") |
| A30 | `nvidia.com/.../a30-gpu/pdf/a30-datasheet.pdf`; Cisco `UCSC-GPU-A30-D` ("TESLA A30, PASSIVE, 180W, 24GB") |
| A40 | `images.nvidia.com/content/Solutions/data-center/a40/nvidia-a40-datasheet.pdf`; Cisco `UCSC-GPU-A40-D` ("TESLA A40 RTX, PASSIVE, 300W, 48GB") |
| A100 40GB | Cisco C240 M6 spec sheet, `UCSC-GPU-A100` ("TESLA A100, PASSIVE, 250W, 40GB") |
| A100 80GB | `nvidia.com/.../a100/pdf/nvidia-a100-datasheet-nvidia-us-2188504-web.pdf` (80GB HBM2e, PCIe and SXM); Cisco `UCSC-GPU-A100-80`, `UCSX-GPU-A100-80` |
| L4 / L40 / L40S | Cisco C240 M7 and X440p spec sheets — "NVIDIA L4: 70W, 24GB", "NVIDIA L40: 300W, 48GB", "NVIDIA L40S: 350W, 48GB" |
| H100 80GB | NVIDIA H100 PCIe product brief PB-11133-001_v02, "Memory size: 80 GB"; Cisco `UCSC-GPU-H100-80`, `UCSX-GPU-H100-80` |
| H100 NVL 94GB | Cisco C240 M7 / X440p spec sheets, "NVIDIA H100 NVL, 400W, 94GB" |
| H200 141GB | `nvidia.com/en-us/data-center/h200/` — "GPU Memory: 141GB" for both SXM and NVL |
| B200 180GB | `images.nvidia.com/aem-dam/Solutions/documents/HGX-B200-PCF-Summary.pdf` — "eight NVIDIA Blackwell B200 GPUs, each with 180 GB of HBM3E memory" |
| RTX PRO 6000 Blackwell Server Edition 96GB | `nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/` |
| AMD Instinct MI100/MI210/MI250/MI250X/MI300A/MI300X/MI325X/MI355X | `rocm.docs.amd.com/en/latest/reference/gpu-arch-specs.html` (AMD's own GPU architecture specifications table); MI210 corroborated by Cisco `UCSX-GPU-MI210` ("AMD Instinct MI210: 300W, 64GB") |

## Consequences

- An unconfigured deployment now reports GPU VRAM for 30 datacenter cards
  across NVIDIA and AMD, on every collector, without configuration.
- `INVENTORY_GPU_MODELS` changes meaning from "the only source" to "the
  override", and its semantics changed from replace to merge. No operator
  action is required: an existing spec keeps working and keeps winning
  wherever it collides.
- The table will go stale. It is a maintenance obligation, roughly on the
  cadence of "Keeping CI current" in `CLAUDE.md` — but a stale table now
  degrades to what shipped before this ADR (the operator configures the
  new card), rather than to a wrong answer.
- **The A16 is the one row where the entity is genuinely ambiguous.** It
  is a single card carrying four 16GB GPUs. Cisco's PID names the 64GB
  card; a Redfish enumeration names each 16GB GPU. This platform's `Gpu`
  models a GPU, so the row is 16GB. An estate that reads the card instead
  overrides it. Called out here because it is the only row where the
  source and the model disagree about what is being measured.
- VRAM is configured and stored in binary units: a row of `40` becomes
  `40 * 1024**3` bytes, matching `Gpu.memory_bytes` elsewhere in the
  platform. Vendors write "40GB" for what is 40 GiB, so this reads
  correctly for every row in the table.

## Update, 2026-09-05: the catalog is a fallback, not the only source

This ADR and `CLAUDE.md` were both read as saying that no management
plane reports GPU VRAM at all. That is not what the code does, and stated
that broadly it would tell a future session not to bother reading one.

`redfish.mapping.gpus_from_processors` **does** read it, and has since
ADR-0016: `MemorySummary.TotalMemorySizeMiB` on a `ProcessorType ==
"GPU"` member, which is standard Redfish 1.0, with a fallback to summing
`ProcessorMemory[].CapacityMiB` for pre-2020.4 firmware that predates
`MemorySummary`. That covers `REDFISH_STANDALONE` and — because Dell's
hardware pass reuses the same mapping (ADR-0020) — `OPENMANAGE` as well.

The three that report nothing are `UCS_CENTRAL`/`UCS_MANAGER` (no field
in the object model), `INTERSIGHT` and `ONEVIEW` (both hardcode
`memory_bytes: None`; neither API has a VRAM attribute on a GPU). Those
are what this ADR was written for, and the reasoning above is unaffected.

The precedence was always right and is worth stating plainly, because it
is what makes the two facts compatible: `GpuCatalog.enrich` returns the
GPU untouched when `memory_bytes` is already set, so **a value a
collector really read always wins and the catalog only fills a gap** —
the same "a provider's `None` means unread, not zero" contract the rest
of the platform follows.

### Still unverified

Whether Dell or HPE actually populate `TotalMemorySizeMiB` for arbitrary
add-in GPUs. The path is standard; the data is best-effort, and
`gpus_from_processors`' own docstring has said so since it was written
("no evidence was found that Dell or HPE populate this"). No live
hardware has settled it either way, so the practical coverage today is
unknown rather than absent.

It is cheap to settle — one authenticated Redfish GET against a Dell with
a GPU fitted — and is now part 3 of `docs/field-test-checklist.md`.
Record the answer here. If iDRAC does populate it, the catalog quietly
stops mattering for Dell and stays load-bearing for Cisco and HPE. If it
does not, that is worth knowing too: it means the standard path is
decorative on real hardware and the catalog is carrying every vendor.

## Update, 2026-09-16: A800/H800/A10G rows, for `redfish/mapping.py`'s PCI ID table

Added while building `INVENTORY_REDFISH_PCIE_GPU_MODELS`
(`docs/adr/0016`'s 2026-09-16 update) — a PCIeDevice-sourced GPU can now
resolve to "A800 80GB" etc., and this table is what turns that string
into VRAM. `A10G` is a new alias on the existing `NVIDIA A10 24GB` row
(AWS's own datasheet confirms the identical 24GB/GDDR6 — an OEM-branded
SKU, not a separate capacity). `NVIDIA A800 40GB`/`80GB` and
`NVIDIA H800 80GB` are new rows: China-market, export-compliance
variants of A100/H100, same silicon, NVLink-limited. The 40GB A800 is
confirmed via NVIDIA's own published datasheet; neither 80GB variant has
an NVIDIA public page at all (confirmed: NVIDIA does not publish one for
either China SKU), but both independently agree across multiple
server-vendor/reseller spec sheets — Lenovo's own ThinkSystem product
guide for H800 specifically, the same OEM-source standard this ADR
already accepts for a Cisco UCS spec sheet.

Deliberately not added: pre-Pascal Tesla device IDs (Fermi/Kepler/Maxwell
— K20/K40/K80/M60/the C-series) turned up while researching the PCI ID
table. Out of scope for the DGX/HGX-class fleet this feature targets,
and adding VRAM rows for decade-old hardware this platform has no
evidence any real estate still runs would be exactly the "guessed
because it seemed plausible" this ADR's sourcing standard exists to
prevent.
