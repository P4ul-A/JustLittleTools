# Prepare a FinPrintv2 identification dataset

`prepare_finprint_dataset.py` converts clustered fin crops into the
`cropped_images` layout used by FinPrintv2 identification training. It validates
manifest-backed JPEGs, removes duplicates, optionally balances individuals, and
creates encounter-aware train, validation, and test CSV files.

The source tree is never modified. Output is assembled in a temporary sibling
directory and renamed into place only after the complete dataset and reports
have been written.

## Defaults

The easily editable defaults are near the top of
`prepare_finprint_dataset.py`:

```python
DEFAULT_SOURCE = Path("/Volumes/SSD_ID/cluster_all_2sec")
DEFAULT_MINIMUM_TOTAL_IMAGES: int | None = 10
DEFAULT_MAXIMUM_MANUAL_IMAGES: int | None = 30
DEFAULT_MODE = "copy"
DEFAULT_EXCLUDE_LETTER_SUFFIX_IDS = True
```

Set either image cutoff to `None` to disable it by default. Command-line
options can override these values for an individual run.

## Expected input

The clustered source must contain one directory per identity:

```text
cluster_all_2sec/
├── NKW-001/
│   └── cropped/
│       ├── crop_manifest.jsonl
│       ├── manual__NKW-001_..._fin.jpg
│       └── additional__NKW-001_..._fin.jpg
├── NKW-002/
│   └── cropped/
│       └── ...
└── KI01/
    └── cropped/
        └── ...
```

Every copied crop must be a real JPEG listed in its
`cropped/crop_manifest.jsonl`. The manifest's `source_kind` identifies manual
and additional crops.

The manifest's `encounter_id` is the camera-burst identifier. Every retained
crop from the same burst is assigned to the same split. For unmatched manual
crops without an `encounter_id`, the source parent folder is used as a
conservative fallback group.

## Output

The output path must not already exist and cannot overlap the source tree:

```text
new-output/
├── cropped_images/
│   ├── train.csv
│   ├── val.csv
│   ├── test.csv
│   ├── NKW-001/
│   │   └── ...jpg
│   └── ...
├── preparation_report.json
└── preparation_report.csv
```

The reports include configuration, before/after counts per ID, split counts,
automatically resolved naming aliases, duplicate and cross-ID conflicts,
excluded offspring IDs, insufficient IDs, capped IDs, and every crop omitted
by balancing.

## Understanding the summary

The dry run prints `configuration`, `summary`, `solved_aliases`, and
`unresolved_aliases` as structured JSON. The same information is written to
`preparation_report.json` when a dataset is created. Retained, split, cap, and
insufficient-sample counts use clean crops after validation and conflict
handling. The duplicate and cross-ID conflict fields instead describe copies
removed during that cleaning process.

| Summary field | Meaning |
| --- | --- |
| `included_id_count` | Canonical identities retained in the finished dataset. Zero-padding aliases merged into one canonical ID count once. |
| `excluded_id_count` | Unique identities excluded for any ID-level reason, including offspring suffixes, unresolved aliases, missing manifests, insufficient clean images, or no usable crops. |
| `included_image_count` | Total crops retained after all filtering and balancing. This equals the train, validation, and test image counts added together. |
| `train_image_count` | Retained crops assigned to `train.csv`. |
| `val_image_count` | Retained crops assigned to `val.csv`. |
| `test_image_count` | Retained crops assigned to `test.csv`. |
| `excluded_letter_suffix_id_count` | `NKW-<digits><letter>` offspring IDs excluded by the suffix policy. This is zero when `--include-letter-suffix-ids` is used. |
| `capped_id_count` | IDs whose clean manual count exceeded the maximum. Each is reduced to the configured number of manual crops and has all additional crops omitted. |
| `manual_images_omitted_by_cap` | Excess manual crops removed from capped IDs. |
| `additional_images_omitted_by_cap` | Additional crops removed because their IDs triggered the manual-only cap. |
| `unresolved_alias_group_count` | Naming-variant groups that could not be assigned one canonical ID and were therefore excluded. This counts alias groups, not individual folders. |
| `resolved_alias_group_count` | Naming-variant groups successfully merged, whether automatically or through an alias map. |
| `automatically_resolved_alias_group_count` | Resolved alias groups handled by the built-in zero-padding rule, such as merging `NKW-74` into `NKW-074`. This is a subset of `resolved_alias_group_count`. |
| `same_id_duplicate_copy_count` | Extra byte-identical copies removed within one canonical ID. One preferred copy is retained, with manual preferred over additional. |
| `cross_id_conflict_hash_count` | Unique image hashes found under more than one canonical identity. No copy of a conflicting hash is retained. |
| `cross_id_conflict_copy_count` | Total crop files removed because their hashes occurred under different identities. One conflict hash can account for several copies. |
| `insufficient_sample_id_count` | Canonical IDs below the enabled minimum total-image cutoff after validation, deduplication, and cross-ID conflict removal. This is zero when the minimum is disabled. |
| `training_only_id_count` | Included IDs assigned entirely to training, with no retained crops in validation or test. |

The following relationships are useful when checking a run:

```text
included_image_count
  = train_image_count + val_image_count + test_image_count

automatically_resolved_alias_group_count
  <= resolved_alias_group_count
```

The summary deliberately separates hashes, physical copies, source ID folders,
and canonical identities. For example, `NKW-074` and `NKW-74` are two source
folders but become one canonical identity after automatic resolution.

For the underlying records, inspect these sections of
`preparation_report.json`:

- `excluded_ids` gives each excluded ID and its reason.
- `solved_aliases` lists every successfully merged alias group, its canonical
  ID, and whether resolution was automatic or explicit.
- `unresolved_aliases` lists alias groups excluded because no single canonical
  identity could be established.
- `naming_aliases` contains both solved and unresolved alias groups for
  compatibility and combined inspection.
- `balancing_details` gives before, after, and omitted counts for every
  candidate ID.
- `balancing_omitted_images` identifies every crop removed by the minimum or
  maximum balancing rules.
- `same_id_duplicates` and `cross_id_conflicts` provide the affected hashes and
  source paths.
- `split_details` gives image and camera-burst counts per split for every
  included ID.
- `rejected_crops` and `manifest_errors` describe invalid inputs that were not
  eligible for inclusion.
- `included_images` lists every retained crop, its source kind, canonical ID,
  output path, encounter, hash, and split.

## Recommended workflow

Run a read-only preview first:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py \
  /path/to/new-output \
  --dry-run
```

Then create the dataset:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py \
  /path/to/new-output
```

The default source is `/Volumes/SSD_ID/cluster_all_2sec`, and the default
materialization mode is `copy`.

## Balancing rules

Minimum filtering uses the clean crop count after manifest validation,
same-ID deduplication, and cross-ID conflict removal:

```text
manual crops + additional crops
```

An ID below the configured minimum is excluded completely.

Maximum filtering uses only the clean manual count. When that count is greater
than the configured maximum, the tool:

1. keeps exactly the configured number of manual crops;
2. omits all additional crops for that ID; and
3. chooses the manual crops deterministically across as many camera bursts as
   possible before choosing second crops from any burst.

An ID with exactly the maximum number of manual crops is not capped, so its
additional crops are retained.

When both cutoffs are enabled, the maximum must be at least the minimum. Zero
and negative cutoff values are rejected.

### Both cutoffs

The defaults use a minimum total of 10 and maximum manual count of 30:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --minimum-total-images 10 \
  --maximum-manual-images 30
```

### Minimum only

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --minimum-total-images 10 \
  --no-maximum-manual-images
```

### Maximum only

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --no-minimum-total-images \
  --maximum-manual-images 30
```

### Neither cutoff

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --no-minimum-total-images \
  --no-maximum-manual-images
```

`--minimum-images` remains an alias for `--minimum-total-images`.

## Offspring IDs

By default, all NKW identities ending in one letter, such as `NKW-1001a`,
`NKW-616b`, or `NKW-1042c`, are excluded as unreliable offspring identities:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --exclude-letter-suffix-ids
```

To include them as separate identities:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --include-letter-suffix-ids
```

An included suffixed identity is never merged automatically with its
unsuffixed numeric identity.

## Copy and link modes

The default `copy` mode creates independent regular files:

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --mode copy
```

Use `--mode hardlink` when source and output are on the same filesystem, or
`--mode symlink` to create absolute symlinks to the source crops.

## Identity aliases

Different zero padding such as `NKW-074` and `NKW-74` is resolved
automatically. Crops from both folders are combined under the standard padded
identity `NKW-074`. NKW numbers use at least three digits, while KI numbers use
at least two.

The JSON and CSV reports record the source IDs, chosen canonical ID, and
`automatic_zero_padding` resolution. The command also prints a `Solved aliases`
section for every dry run and completed dataset, for example:

```text
Solved aliases:
  NKW-074, NKW-74 -> NKW-074 (automatic zero padding)
```

An explicit JSON mapping can override the canonical spelling when necessary:

```json
{
  "NKW-074": "NKW-74"
}
```

```sh
python3 prepare_finprint_dataset/prepare_finprint_dataset.py OUTPUT \
  --alias-map /path/to/finprint_aliases.json
```

Suffixes are part of the alias key when suffix IDs are included, so an alias
mapping cannot merge an offspring ID into an unsuffixed ID.

## Other options

- `--source PATH`: use a different clustering output.
- `--val-fraction FLOAT`: validation encounter fraction; default `0.15`.
- `--test-fraction FLOAT`: test encounter fraction; default `0.15`.
- `--split-seed TEXT`: deterministic encounter ordering and cap-selection
  seed.
- `--dry-run`: scan, hash, and print configuration and summary without writing
  output.
- `--no-progress`: suppress hashing and materialization progress.
- `-h`, `--help`: show the complete CLI reference.

Validation and test fractions must each be in `[0, 1)` and must sum to less
than one. IDs with fewer than three retained encounter groups remain
training-only.

## FinPrintv2 configuration

Point `data_directory` to the generated output directory, use
`dataset: cropped_images`, and set `replace_csvs: false` so FinPrintv2 uses the
encounter-aware CSV files produced by this tool.
