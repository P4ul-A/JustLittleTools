# Drone whale segmentation experiments

These scripts test two complementary approaches on DJI video:

- `test_yoloe_whales.py` automatically searches every frame using text prompts
  such as `whale` and `orca`. It is convenient but zero-shot predictions must
  not be assumed accurate.
- `test_sam21_whales.py` starts from a box or point on the first frame and uses
  SAM 2.1 video memory to propagate a precise mask. It is better suited to
  assisted annotation, but it does not discover whales that enter later.

Both scripts process the entire video by default and automatically choose CUDA,
Apple MPS, or CPU in that order. Official model weights download on first use.
The first prompted YOLOE run also obtains its text encoder, which is much larger
than the nano YOLOE checkpoint itself.

## Setup

From the repository root:

```sh
.venv/bin/python -m pip install -U ultralytics
```

YOLOE-26 text prompting and SAM 2.1 video propagation require a current
Ultralytics installation. See the official
[YOLOE documentation](https://docs.ultralytics.com/models/yoloe/) and
[SAM 2 documentation](https://docs.ultralytics.com/models/sam-2/).

## Test YOLOE-26

Run the built-in `whale`, `orca`, and `dolphin` prompts:

```sh
.venv/bin/python drone_measuring/test_yoloe_whales.py /path/to/DJI_0001.MP4
```

Try species-specific prompts and preview the result:

```sh
.venv/bin/python drone_measuring/test_yoloe_whales.py /path/to/DJI_0001.MP4 \
  --prompt "humpback whale" \
  --prompt "minke whale" \
  --prompt orca \
  --imgsz 1280 \
  --show
```

Useful speed controls are `--stride 2`, `--imgsz 640`, and `--seconds 30`.
Use `--device mps` to require Apple GPU execution instead of automatic
selection.

## Test SAM 2.1

With no prompt arguments, the script displays the first frame and asks for a
box. After selection it prints a ready-to-copy
`--box X1 Y1 X2 Y2` argument:

```sh
.venv/bin/python drone_measuring/test_sam21_whales.py /path/to/DJI_0001.MP4 \
  --draw-negative-boxes \
  --show
```

After the positive whale box is accepted, `--draw-negative-boxes` opens the
first frame again. Draw one or more exclusion boxes over splash, foam, or wake,
press Enter or Space after each, then press Esc to finish. The tool prints every
drawn exclusion as a reusable `--negative-box X1 Y1 X2 Y2` argument in original
video coordinates.

For repeatable runs, supply first-frame pixel coordinates:

```sh
.venv/bin/python drone_measuring/test_sam21_whales.py /path/to/DJI_0001.MP4 \
  --box 1420 610 2050 1210 \
  --negative-box 900 560 1400 1250 \
  --negative-box 300 400 900 1400
```

Repeat `--box` to initialize multiple whales. A positive point, optionally
refined with background points, is also supported:

```sh
.venv/bin/python drone_measuring/test_sam21_whales.py /path/to/DJI_0001.MP4 \
  --point 1740 910 \
  --negative-point 1500 900 \
  --negative-point 2050 900
```

SAM 2.1 exposes negative points rather than native negative boxes. Internally,
each `--negative-box` is converted into a 3-by-3 grid of negative prompt
points. Keep exclusion boxes off the whale itself; any body pixels inside them
are deliberately discouraged.

SAM tracking is most reliable with `--stride 1`. If an animal enters after the
first frame, create a clip beginning shortly before its appearance and run SAM
again with a new prompt.

## SAM-assisted YOLO training workflow

Yes, using SAM 2.1 to bootstrap YOLO masks is reasonable. Treat the masks as
annotation suggestions: review and correct them before training.

### 1. Export sparse candidate labels

Create a separate export directory for every source video:

```sh
.venv/bin/python drone_measuring/test_sam21_whales.py /path/to/DJI_0001.MP4 \
  --box 1420 610 2050 1210 \
  --export-dir /path/to/sam_exports/DJI_0001 \
  --export-every 30 \
  --save-previews
```

The export contains:

```text
DJI_0001/
├── images/          original training frames
├── labels/          normalized YOLO segmentation polygons
├── previews/        optional visual checks
├── manifest.csv     source frame and review status
└── metadata.json    model, prompt, video, and export settings
```

At 30 fps, `--export-every 30` saves approximately one frame per second. Avoid
exporting every video frame: adjacent frames add little diversity and can
overweight one encounter. Add `--include-empty` only when you intend to review
candidate background frames carefully.

### 2. Review every candidate

Use the lightweight reviewer:

```sh
.venv/bin/python drone_measuring/review_sam_export.py \
  /path/to/sam_exports/DJI_0001
```

Press `A` to approve, `R` to reject, `U` to reset, or `Q` to save and quit.
If a polygon needs editing, correct it in a segmentation annotation tool, then
press `C` in the reviewer to mark it `CORRECTED`. The dataset builder accepts
only `APPROVED` and `CORRECTED` rows.

Check especially for:

- masks drifting onto glare, wakes, or nearby whales;
- partial masks when the animal is submerged;
- water included between separated body parts;
- missed whales incorrectly exported as empty background;
- inconsistent treatment of pectoral fins, flukes, and shadows.

Choose one annotation policy and apply it consistently. SAM errors copied into
the labels will be learned by YOLO.

### 3. Build train and validation data by source video

Review exports from at least two independent videos, preferably from different
flights or encounters. Then build the dataset:

```sh
.venv/bin/python drone_measuring/build_yolo_dataset.py \
  /path/to/sam_exports/DJI_0001 \
  /path/to/sam_exports/DJI_0002 \
  /path/to/sam_exports/DJI_0003 \
  --output /path/to/whale_yolo_dataset
```

The builder assigns whole videos to either training or validation and writes
`whales.yaml`. This avoids the misleading validation scores produced when
near-identical adjacent frames appear in both splits. Videos from the same
encounter can still leak individual animals and conditions, so keep entire
encounters on one side when possible.

### 4. Train YOLO26 segmentation

Start with the nano model and a conservative batch size on Apple Silicon:

```sh
.venv/bin/yolo segment train \
  model=yolo26n-seg.pt \
  data=/path/to/whale_yolo_dataset/whales.yaml \
  epochs=100 \
  imgsz=960 \
  batch=4 \
  device=mps \
  workers=0 \
  project=drone_measuring/runs \
  name=whale_yolo26n_seg
```

If memory is tight, reduce `batch` and then `imgsz`. Use an independent,
manually reviewed flight as the final test set; do not measure final model
quality against unreviewed SAM pseudo-labels.

### 5. Iterate with active learning

Run the trained `best.pt` on new flights, retain false positives, false
negatives, difficult sea states, different altitudes, and different degrees of
submergence, then correct those examples and retrain. This targeted cycle is
usually more valuable than adding thousands of nearly identical easy frames.
