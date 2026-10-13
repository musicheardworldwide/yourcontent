# Third-party notices

YourContent's own code is under the PolyForm Small Business License 1.0.0 (`LICENSE`). It contains no third-party code (one test fixture holds third-party
text; see *Test fixtures* below; the optional craft service builds four MIT/Apache apps from their own repositories,
see *Craft service*) and **ships no model weights**: it sends
graphs to a ComfyUI you run, which loads model files you downloaded yourself. Each model keeps its own licence, and
those licences apply to how you use the models and what you make with them. YourContent records the licence of every
model it used in each file's sidecar, and an export inherits the most restrictive one.

Checked against the model cards on Hugging Face: Qwen-Image-2512 on 2026-10-12, the rest on 2026-10-07.

| model | files YourContent names by default | role | licence | commercial use |
|---|---|---|---|---|
| **Qwen-Image-2512** (default) ([Qwen/Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512), ComfyUI repack [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI)) | `qwen_image_2512_fp8_e4m3fn.safetensors` | picture model | Apache-2.0 (the model card's `license: apache-2.0`; the repack declares `apache-2.0` too) | **Yes**; YourContent labels its pictures commercial OK (`qwen-image-2512`) |
| Qwen2.5-VL 7B, fp8 file from that repack (upstream [Qwen/Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct)) | `qwen_2.5_vl_7b_fp8_scaled.safetensors` | Qwen-Image-2512's text encoder | Apache-2.0 | yes |
| Qwen-Image VAE, from that repack | `qwen_image_vae.safetensors` | Qwen-Image-2512's VAE | Apache-2.0 | yes |
| Qwen-Image 2.1, selectable ([Qwen/Qwen-Image-2.1](https://huggingface.co/Qwen/Qwen-Image-2.1), ComfyUI repack [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)) | `qwen_image_2.1_bf16.safetensors`, `qwen_image_2.1_vae_bf16.safetensors` | picture model and VAE | Qwen RESEARCH LICENSE AGREEMENT (release date 2026-09-20) | **No.** Non-commercial purposes only; a separate commercial licence from Qwen is needed |
| Any Qwen-Image 2.1 quantisation, fine-tune or LoRA you add in `data/lab/config/models.json` | yours | picture model | follows Qwen-Image 2.1 (YourContent labels it `qwen-image-nc`) | **No** |
| Qwen3-VL 8B, int8 file from the ComfyUI repack ([Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1)) | `qwen3vl_8b_int8_convrot.safetensors` | picture text encoder | Qwen research licence: the repack that distributes this file declares `qwen-research` (YourContent labels it `qwen-image-nc`). Upstream [Qwen/Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct) is Apache-2.0, which applies only to a conversion you make from it yourself | **No** (as distributed in the repack) |
| Wan 2.2 I2V A14B ([Wan-AI/Wan2.2-I2V-A14B](https://huggingface.co/Wan-AI/Wan2.2-I2V-A14B)) | `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors`, `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` | clip model (two experts) | Apache-2.0 | yes |
| Wan 2.1 VAE | `wan_2.1_vae.safetensors` | clip VAE | Apache-2.0 | yes |
| lightx2v Wan 2.2 4-step LoRAs ([lightx2v/Wan2.2-Lightning](https://huggingface.co/lightx2v/Wan2.2-Lightning)) | `wan2.2_i2v_lightx2v_4steps_lora_v1_{high,low}_noise.safetensors` | clip speed-up | Apache-2.0 | yes |
| umt5-xxl ([google/umt5-xxl](https://huggingface.co/google/umt5-xxl)) | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | clip text encoder | Apache-2.0 | yes |
| ACE-Step 1.5 turbo ([ACE-Step/Ace-Step1.5](https://huggingface.co/ACE-Step/Ace-Step1.5)) | `acestep_v1.5_turbo.safetensors`, `qwen_0.6b_ace15.safetensors`, `qwen_1.7b_ace15.safetensors`, `ace_1.5_vae.safetensors` | music | Apache-2.0 / MIT (the model card lists MIT) | yes; the card says generated music may be used commercially |

What this means in practice:
- A picture made with Qwen-Image-2512 (the default) has no model restriction and is labelled commercial OK. So is
  a clip made from it, as long as nothing else in it is restricted. Apache-2.0 asks anyone who redistributes the
  model files to keep its licence and notices; YourContent does not redistribute them.
- A picture made with any Qwen-Image 2.1 variant is non-commercial. So is a clip or extension made from it, and any
  cut that contains one of them. YourContent labels them "NC" and says so in the export row; it never blocks.
- A cut made only of your own uploads and ACE-Step music has no model restriction, but uploads are marked "rights
  not checked": you are responsible for having the rights.
- If you redistribute Qwen-Image 2.1 files yourself, its licence requires a copy of the licence and the notice in
  `NOTICE`. YourContent does not redistribute them.

Runtime tools (not bundled in the repository; the Docker image installs them from Debian):
- Python 3.12 (PSF License).
- FFmpeg 7.1 (LGPL-2.1+, built by Debian with GPL components such as libx264; GPL-2.0+ for the build as a whole).
- ComfyUI (GPL-3.0) and ComfyUI-VideoHelperSuite (GPL-3.0) are separate programs you run; YourContent talks to
  ComfyUI over HTTP only.
- The post render service (`render/`, its own image) is built on Microsoft's Playwright Python image: Playwright
  (Apache-2.0) and the Chromium it ships (BSD-3-Clause and the licences Chromium lists in its own credits).
- Its 19 font families are fetched while that image is built, never stored in the repository: 18 under the SIL
  Open Font License 1.1 and Permanent Marker under Apache-2.0. Each family's licence text is installed beside the
  fonts. List, sources and checksums: `render/FONTS.md` and `render/fonts.json`. Neither licence restricts the
  pictures made with them.
- The app's own page uses three of those families, Inter, Space Grotesk and JetBrains Mono (all SIL Open Font
  License 1.1), fetched the same way into `static/fonts/` while the app image is built (with their licence texts in
  `static/fonts/licences/`) and served from the app itself, never from a font CDN. Without them the page falls back
  to the system font.

Craft service (photo and video recipes; optional, its own container built from `craft/Dockerfile`):
- PhotoCraft, LightCraft, FilmCraft and EffectCraft ([github.com/storytold](https://github.com/storytold)), by the
  ArtCraft Team and contributors, are licensed MIT OR Apache-2.0. YourContent does not include their source; the craft
  image builds each app's CLI from its upstream repository at a pinned commit (photocraft 6f39aba, lightcraft 52f64ce,
  filmcraft a4536bc, effectcraft e38a92e) and copies each app's `LICENSE-MIT`, `LICENSE-APACHE`, `NOTICE` (copyright
  ArtCraft Team and contributors), attribution file, bundled font and icon licences, and a list of every compiled-in
  crate with its licence into `/usr/share/licenses/<app>/`. YourContent runs them as separate programs over MCP.
- LightCraft's `crates/segment` (a port of Hugging Face Transformers' SAM 3 code) is Apache-2.0 only, not MIT.
- The SAM 3 weights are Meta's, under the SAM License
  (<https://github.com/facebookresearch/sam3/blob/main/LICENSE>), a custom licence that is not open source. They are
  never shipped: AI masks are off unless you set `MEDIA_LAB_CRAFT_AI_MASKS=1` and supply the model folder yourself,
  which means accepting Meta's terms.
- "ArtCraft" and the PhotoCraft, LightCraft, FilmCraft and EffectCraft names and logos are trademarks of the ArtCraft
  Team. They are not covered by the apps' open-source licence, YourContent does not use them as its own branding, and
  the image does not include the apps' logo files.
- FilmCraft's audio decoding uses `symphonia` (MPL-2.0). Its source is available at
  [github.com/pdeljanov/Symphonia](https://github.com/pdeljanov/Symphonia) and on crates.io, at the versions listed in
  `/usr/share/licenses/filmcraft/DEPENDENCIES.txt`; YourContent does not modify it.
- Codecs such as H.264, HEVC and AAC may be covered by patents in some countries; licensing them for your use is up to you.

Test fixtures:
- `tests/fixtures/object_info_0.37.0.json` is the `/object_info` output of ComfyUI 0.37.0 (GPL-3.0), kept for schema
  tests. Its node names, descriptions and tooltips are ComfyUI's text, not YourContent's, and are not covered by
  YourContent's licence. The Docker image includes `tests/` (so the suite can run in the container), and with it
  this file.
