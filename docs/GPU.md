# GPU, VRAM and downloads

Making pictures, clips and music needs a GPU through ComfyUI. Everything else in YourContent (posts, photo and
video recipes, the calendar, Google, agents) runs without one.

## Short version

- **24 GB or more (NVIDIA):** every feature with the stock files. 32 GB is the only card size measured so far.
- **12–16 GB, AMD, Apple Silicon:** smaller quantised files can work but are **not tested** in YourContent yet;
  see *Smaller cards* below.
- **No GPU:** use posts, recipes, calendar and agents; generation features show as unavailable with the reason.

## Downloads

`python3 -m medialab get-models <set> --models-dir /path/to/ComfyUI/models` fetches a set with sha256 checks.

| set | files | total |
|---|---|---|
| pictures, bf16 (default) | model 14.23 + encoder 9.35 + VAE 0.68 | **24.26 GB** |
| pictures, int8 instead | 7.26 + 9.35 + 0.68 | 17.28 GB |
| clips | 2 × 14.29 experts + 2 × 1.23 LoRA + umt5 6.74 + VAE 0.25 | **38.03 GB** |
| music | 4.79 + 1.19 + 3.71 + 0.34 | **10.03 GB** |
| all, bf16 default | | **72.32 GB** |

## Measured (one card: 32 GB, Blackwell)

These are peaks on a 32 GB card, where ComfyUI keeps whatever fits resident: **not minimums**.

| set | what was measured |
|---|---|
| pictures | 1024², 30 steps: 14 s int8, 24 s bf16; 76 s with 2 reference pictures |
| clips | peak 31,201 MiB at 848×480, 81 frames, 60.7 s; a 3 s 480p clip took 37–54 s; Extend 3 s took 29.8 s |
| music | 10–11 GB resident; 12.7 GB peak for a 30 s track (9.1 s), 20.7 GB for 120 s (14.9 s) |
| export | ffmpeg only: a 10.7 s 9:16 cut took 8.9 s |

## Smaller cards (not measured)

Typical starting points, untested in YourContent. Every Qwen-Image quantisation keeps the Qwen Research Licence, so
its output is non-commercial.

- **16 GB:** Qwen-Image Q8_0 GGUF with the int8 text encoder on CPU; Wan 2.2 Q4_K_M or Q5_K_M with umt5 Q8_0.
- **12 GB:** Qwen-Image Q4_K_M; Wan 2.2 Q3_K_M.
- **Mac and AMD:** GGUF for pictures and clips, the stock music model.

GGUF files load through the ComfyUI-GGUF nodes. YourContent's graphs don't emit those loaders yet (planned), so for
now these need your own `data/lab/config/models.json` entries and are at your own risk.

Hardware notes: NVFP4 needs a Blackwell card; fp8 compute needs sm_89 or newer (older cards upcast it, saving only
storage); ROCm always upcasts fp8.
