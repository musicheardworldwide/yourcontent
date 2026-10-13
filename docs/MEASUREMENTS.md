# Measurements

Card: RTX PRO 4500 Blackwell, 32 GB. ComfyUI 0.37.0. Measured 2026-10-07 by sampling `nvidia-smi` every 0.25 s.

## ACE-Step 1.5 turbo (music)

Graph: UNETLoader `acestep_v1.5_turbo` → ModelSamplingAuraFlow (shift 3) → KSampler (8 steps, cfg 1, euler/simple);
DualCLIPLoader `qwen_0.6b_ace15` + `qwen_1.7b_ace15` (type `ace`) → TextEncodeAceStepAudio1.5 (negative = ConditioningZeroOut);
EmptyAceStep1.5LatentAudio; VAEDecodeAudio `ace_1.5_vae`; SaveAudioMP3 V0.

| Run | Time | VRAM before → peak |
|---|---|---|
| 30 s track, cold (ComfyUI cache freed) | 9.1 s | 1.9 GB → 12.7 GB |
| 30 s track, warm, LLM audio codes on | 5.2 s | 11.5 GB → 11.7 GB |
| 30 s track, warm, LLM audio codes off | 3.6 s | 11.5 GB → 13.9 GB |
| 120 s track, warm, codes on | 14.9 s | 11.5 GB → 20.7 GB |

The vendor's "under 4 GB VRAM" does not hold in this configuration: ACE-Step resident is about 10–11 GB, and a 2-minute track peaks near 21 GB.
Downloads: 4.57 GB diffusion model + 3.54 GB (1.7b) + 1.19 GB (0.6b) text encoders + 0.32 GB VAE ≈ 9.6 GB, all sha256-verified against Hugging Face LFS oids (`~/.forge/fetch-ace15.sh`).

## Pictures and clips (from the qwen-studio build)

Pictures at 1024², 30 steps: NVFP4 12 s, int8 14 s, bf16 + LoRA 24 s; with 2 references 76 s.
Clips, Wan 2.2 i2v 14B 4-step: 3 s at 480p 37–54 s. Extend 3 s + 3 s: 36 s.

## End-to-end through YourContent (2026-10-07, `tests/e2e_comfy.py`, owner's int8 picture model)

Wall time per step, models partly warm (ComfyUI had other models cached): picture 512², 4 steps 6.3 s; clip 3 s
480p 42.6 s; Extend 3 s 29.8 s (97 frames = 49 + 49 − 1); ACE-Step 30 s track 10.3 s (48 kHz FLAC, 1,440,000
samples); ACE-Step 5 s via SaveAudioAdvanced 2.0 s; interrupt by prompt id → stopped 1.0 s; final 9:16 export
of still + clip + extension + music (256 frames, 10.67 s) 8.9 s, −13.8 LUFS, true peak −2.1 dBTP, checks ok.
