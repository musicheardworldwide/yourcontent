"""PURE ComfyUI graph builders and their requirements (§4, §9). Owner: WP3. Imports nothing impure.

The picture and clip graphs are today's (pre-v1) graphs, node for node; only the file prefixes and the model
table moved out. The music graph is ComfyUI's own ACE-Step 1.5 template (§9). `validate()` checks a graph
against /object_info so a renamed node or input fails loudly in tests and in /api/health.
"""
from __future__ import annotations

from fractions import Fraction
from math import floor

# Picture sizes, clip pixel areas and clip frame counts, as in the pre-v1 app.
SIZES = {"square": (1328, 1328), "portrait": (1104, 1472), "landscape": (1472, 1104),
         "wide": (1664, 928), "tall": (928, 1664)}
VIDEO_AREA = {"480p": 832 * 480, "720p": 1280 * 720}
VIDEO_FRAMES = {"3": 49, "5": 81}
VIDEO_FPS = 16
VIDEO_STEPS = 4
MAX_REFS = 6

WAN_NEGATIVE = "static, frozen, blurry, distorted, deformed, low quality, watermark, text, subtitles"
INSTRUMENTAL = "[Instrumental]"

KEYSCALES = tuple(f"{n} {m}" for m in ("major", "minor")
                  for n in ("C", "C#", "Db", "D", "D#", "Eb", "E", "F", "F#", "Gb", "G", "G#", "Ab", "A", "A#",
                            "Bb", "B"))
TIMESIGS = ("2", "3", "4", "6")
LANGUAGES = ("ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "he", "hi", "hr",
             "ht", "hu", "id", "is", "it", "ja", "ko", "la", "lt", "ms", "ne", "nl", "no", "pa", "pl", "pt", "ro",
             "ru", "sa", "sk", "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi", "yue", "zh",
             "unknown")
SAVERS = ("SaveAudio", "SaveAudioAdvanced")

# (class_type, input) -> ComfyUI model folder, for requirements().
MODEL_INPUTS = {
    ("UNETLoader", "unet_name"): "diffusion_models",
    ("CLIPLoader", "clip_name"): "text_encoders",
    ("DualCLIPLoader", "clip_name1"): "text_encoders",
    ("DualCLIPLoader", "clip_name2"): "text_encoders",
    ("VAELoader", "vae_name"): "vae",
    ("LoraLoaderModelOnly", "lora_name"): "loras",
}


def fit(w: int, h: int, area: int) -> tuple[int, int]:
    """`area` pixels at the aspect ratio of w x h, in multiples of 16."""
    ratio = max(0.4, min(w / h, 2.5))
    tw = (area * ratio) ** 0.5
    return int(round(tw / 16) * 16), int(round(tw / ratio / 16) * 16)


# Picture graph families (a variant's "graph" in models.default.json). "qwen-image-2.1" is the default for
# variants that name none, so an owner's own 2.1 variants keep working.
IMAGE_GRAPHS = ("qwen-image-2512", "qwen-image-2.1")
# Qwen-Image-2512's own negative prompt (ComfyUI's template uses this list in Chinese), used when none is given.
QWEN_2512_NEGATIVE = ("low resolution, low quality, deformed limbs, deformed fingers, oversaturated, waxy look, "
                      "faces without detail, overly smooth, AI look, chaotic composition, blurry or distorted text")


def image_family(variant: dict) -> str:
    g = variant.get("graph") or "qwen-image-2.1"
    return g if g in IMAGE_GRAPHS else "qwen-image-2.1"


def max_refs(variant: dict) -> int:
    """Reference pictures a variant takes: Qwen-Image 2.1 up to MAX_REFS, Qwen-Image-2512 none (text-to-image)."""
    return 0 if image_family(variant) == "qwen-image-2512" else MAX_REFS


def image_graph(variant: dict, prompt: str, negative: str, w: int, h: int, steps: int, seed: int,
                refs: list[str], prefix: str) -> dict:
    """A picture from text. `variant` = {unet, lora, clip, vae, graph, cfg}; `refs` are LoadImage values (only
    Qwen-Image 2.1 takes them; Qwen-Image-2512 ignores them, callers refuse them first)."""
    if image_family(variant) == "qwen-image-2512":
        return image_graph_2512(variant, prompt, negative, w, h, steps, seed, prefix)
    enc = {"clip": ["2", 0], "prompt": prompt, "negative_prompt": negative, "resolution": max(w, h)}
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": variant["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": variant["clip"], "type": "qwen_image",
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": variant["vae"]}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": enc},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                                                   "latent_image": ["5", 0], "seed": seed, "steps": steps, "cfg": 1.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": prefix}},
    }
    if variant.get("lora"):
        g["9"] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": ["1", 0], "lora_name": variant["lora"], "strength_model": 1.0}}
        g["6"]["inputs"]["model"] = ["9", 0]
    # References are spliced in as VAE latents, so the encoder needs the VAE once any are attached.
    for i, image in enumerate(list(refs)[:MAX_REFS], 1):
        g[f"r{i}"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        enc[f"images.image_{i}"] = [f"r{i}", 0]
    if refs:
        enc["vae"] = ["3", 0]
    return g


def image_graph_2512(variant: dict, prompt: str, negative: str, w: int, h: int, steps: int, seed: int,
                     prefix: str) -> dict:
    """Qwen-Image-2512 text-to-image, node for node ComfyUI's own template (image_qwen_Image_2512): Qwen2.5-VL
    text encoder through CLIPTextEncode, ModelSamplingAuraFlow shift 3.1, EmptySD3LatentImage, KSampler euler/simple
    at cfg 4 (50 steps recommended)."""
    cfg = float(variant.get("cfg") or 4.0)
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": variant["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": variant["clip"], "type": "qwen_image",
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": variant["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative or QWEN_2512_NEGATIVE}},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["6", 0], "positive": ["4", 0], "negative": ["5", 0],
                                                   "latent_image": ["7", 0], "seed": seed, "steps": steps, "cfg": cfg,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }
    if variant.get("lora"):
        g["11"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": ["1", 0], "lora_name": variant["lora"], "strength_model": 1.0}}
        g["6"]["inputs"]["model"] = ["11", 0]
    return g


def video_graph(video: dict, start: str, prompt: str, w: int, h: int, frames: int, seed: int, prefix: str) -> dict:
    """Wan 2.2 14B image-to-video, two experts (high noise steps 0-2, low noise 2-end), lightx2v 4-step."""
    g: dict = {}
    for side, nid in (("high", 10), ("low", 20)):
        g[str(nid)] = {"class_type": "UNETLoader",
                       "inputs": {"unet_name": video["unet"].format(side), "weight_dtype": "default"}}
        g[str(nid + 1)] = {"class_type": "LoraLoaderModelOnly",
                           "inputs": {"model": [str(nid), 0], "lora_name": video["lora"].format(side),
                                      "strength_model": 1.0}}
        g[str(nid + 2)] = {"class_type": "ModelSamplingSD3", "inputs": {"model": [str(nid + 1), 0], "shift": 5.0}}
    common = {"noise_seed": seed, "steps": VIDEO_STEPS, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
              "positive": ["35", 0], "negative": ["35", 1]}
    g.update({
        "30": {"class_type": "CLIPLoader", "inputs": {"clip_name": video["clip"], "type": "wan", "device": "default"}},
        "31": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": prompt}},
        "32": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": WAN_NEGATIVE}},
        "33": {"class_type": "VAELoader", "inputs": {"vae_name": video["vae"]}},
        "34": {"class_type": "LoadImage", "inputs": {"image": start}},
        "35": {"class_type": "WanImageToVideo", "inputs": {"positive": ["31", 0], "negative": ["32", 0],
                                                          "vae": ["33", 0], "width": w, "height": h,
                                                          "length": frames, "batch_size": 1,
                                                          "start_image": ["34", 0]}},
        "40": {"class_type": "KSamplerAdvanced", "inputs": {**common, "model": ["12", 0], "add_noise": "enable",
                                                           "latent_image": ["35", 2], "start_at_step": 0,
                                                           "end_at_step": 2, "return_with_leftover_noise": "enable"}},
        "41": {"class_type": "KSamplerAdvanced", "inputs": {**common, "model": ["22", 0], "add_noise": "disable",
                                                           "latent_image": ["40", 0], "start_at_step": 2,
                                                           "end_at_step": 10000,
                                                           "return_with_leftover_noise": "disable"}},
        "42": {"class_type": "VAEDecode", "inputs": {"samples": ["41", 0], "vae": ["33", 0]}},
        "43": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["42", 0], "frame_rate": VIDEO_FPS,
                                                           "loop_count": 0, "filename_prefix": prefix,
                                                           "format": "video/h264-mp4", "pingpong": False,
                                                           "save_output": True}},
    })
    return g


def quantise_seconds(seconds: float) -> float:
    """ACE's latent runs at 25 frames per second: snap to 0.04 s, half up, never below 1 s."""
    q = floor(Fraction(str(float(seconds))) * 25 + Fraction(1, 2))
    return max(25, q) / 25


def music_graph(music: dict, tags: str, lyrics: str, bpm: int, seconds: float, seed: int,
                keyscale: str = "C major", timesig: str = "4", language: str = "en",
                saver: str = "SaveAudio", prefix: str = "yourcontent/music") -> dict:
    """ACE-Step 1.5 turbo text-to-music (ComfyUI's audio_ace_step_1_5_split template, §9)."""
    if saver not in SAVERS:
        raise ValueError(f"unknown audio saver {saver}")
    secs = quantise_seconds(seconds)
    save_inputs: dict = {"audio": ["9", 0], "filename_prefix": prefix}
    if saver == "SaveAudioAdvanced":
        save_inputs["format"] = "flac"   # plain string; accepted by 0.37.0 (e2e, 2026-10-07)
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": music["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": music["clip1"], "clip_name2": music["clip2"],
                                                         "type": "ace", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": music["vae"]}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "TextEncodeAceStepAudio1.5", "inputs": {
            "clip": ["2", 0], "tags": tags, "lyrics": lyrics.strip() or INSTRUMENTAL, "seed": seed, "bpm": int(bpm),
            "duration": secs, "timesignature": str(timesig), "language": language, "keyscale": keyscale,
            "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": 0.85, "top_p": 0.9, "top_k": 0,
            "min_p": 0.0}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": secs, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                                                   "latent_image": ["7", 0], "seed": seed, "steps": 8, "cfg": 1.0,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": saver, "inputs": save_inputs},
    }


def requirements(graph: dict) -> dict:
    """{"nodes": set[str], "models": {folder: set[str]}}"""
    nodes: set[str] = set()
    models: dict[str, set[str]] = {}
    for node in graph.values():
        ct = node.get("class_type")
        nodes.add(ct)
        for name, value in (node.get("inputs") or {}).items():
            folder = MODEL_INPUTS.get((ct, name))
            if folder and isinstance(value, str):
                models.setdefault(folder, set()).add(value)
    return {"nodes": nodes, "models": models}


# ---------------------------------------------------------------------------------------------- validation

def _spec_options(spec) -> list | None:
    """Combo options of an input spec, in either object_info style; None when it is not a combo."""
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    if isinstance(spec[0], list):
        return list(spec[0])
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return list(spec[1].get("options") or [])
    return None


def _spec_type(spec) -> str:
    if isinstance(spec, (list, tuple)) and spec:
        return "COMBO" if isinstance(spec[0], list) else str(spec[0])
    return str(spec)


def _spec_opts(spec) -> dict:
    if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1]
    return {}


# Inputs whose combo is a live folder listing that our generated names are never in.
UNCHECKED_COMBOS = {("LoadImage", "image")}


def validate(graph: dict, object_infos: dict) -> list[str]:
    """Check every node against /object_info: class exists, required inputs present, no unknown inputs,
    combo values in their options, numbers in range, links point at a real output of the right type."""
    errs: list[str] = []
    for nid, node in graph.items():
        ct = node.get("class_type")
        info = object_infos.get(ct)
        if not info:
            errs.append(f"node {nid}: {ct} is not installed in ComfyUI")
            continue
        req = (info.get("input") or {}).get("required") or {}
        opt = (info.get("input") or {}).get("optional") or {}
        specs = {**req, **opt}
        inputs = node.get("inputs") or {}
        # autogrow inputs ("images" -> "images.image_1" ...)
        grow: dict[str, tuple[str, dict]] = {}
        for name, spec in specs.items():
            if _spec_type(spec) == "COMFY_AUTOGROW_V3":
                tmpl = _spec_opts(spec).get("template") or {}
                names = tmpl.get("names") or []
                sub = ((tmpl.get("input") or {}).get("required") or {})
                sub_spec = next(iter(sub.values()), ["*"])
                for n in names:
                    grow[f"{name}.{n}"] = (name, sub_spec)
        # VHS_VideoCombine: per-format extra widgets are legal inputs
        extra: dict[str, object] = {}
        if ct == "VHS_VideoCombine":
            fmt_spec = specs.get("format")
            fmts = _spec_opts(fmt_spec).get("formats") or {}
            for w in fmts.get(inputs.get("format"), []) or []:
                if isinstance(w, list) and w:
                    extra[w[0]] = w[1] if len(w) > 1 else "*"
        # dynamic combos: sub inputs of the chosen key ("format.quality")
        for name, spec in specs.items():
            if _spec_type(spec) == "COMFY_DYNAMICCOMBO_V3":
                chosen = inputs.get(name)
                for o in _spec_opts(spec).get("options") or []:
                    if o.get("key") == chosen:
                        for sub_name, sub_spec in ((o.get("inputs") or {}).get("required") or {}).items():
                            extra[f"{name}.{sub_name}"] = sub_spec
        for name, spec in req.items():
            if _spec_type(spec) == "COMFY_AUTOGROW_V3":
                continue
            if name not in inputs:
                errs.append(f"node {nid} {ct}: required input {name} is missing")
        for name, value in inputs.items():
            if name in specs:
                spec = specs[name]
            elif name in grow:
                spec = grow[name][1]
            elif name in extra:
                spec = extra[name]
            else:
                errs.append(f"node {nid} {ct}: unknown input {name}")
                continue
            errs.extend(_check_value(graph, object_infos, nid, ct, name, spec, value))
    return errs


def _check_value(graph, object_infos, nid, ct, name, spec, value) -> list[str]:
    where = f"node {nid} {ct}.{name}"
    typ = _spec_type(spec)
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
        src = graph.get(value[0])
        if src is None:
            return [f"{where}: links to missing node {value[0]}"]
        outs = (object_infos.get(src.get("class_type")) or {}).get("output") or []
        if value[1] >= len(outs):
            return [f"{where}: node {value[0]} has no output {value[1]}"]
        out_t = outs[value[1]]
        if typ not in ("*", "COMBO") and out_t != "*" and out_t != typ and typ.split(",")[0] != out_t:
            return [f"{where}: expects {typ}, got {out_t} from node {value[0]}"]
        return []
    opts = _spec_opts(spec)
    if typ == "COMBO":
        if (ct, name) in UNCHECKED_COMBOS:
            return [] if isinstance(value, str) and value else [f"{where}: empty value"]
        options = _spec_options(spec) or []
        return [] if value in options else [f"{where}: {value!r} is not one of the choices"]
    if typ == "COMFY_DYNAMICCOMBO_V3":
        keys = [o.get("key") for o in opts.get("options") or []]
        return [] if value in keys else [f"{where}: {value!r} is not one of {keys}"]
    if typ == "INT":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{where}: expects a whole number"]
    elif typ == "FLOAT":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{where}: expects a number"]
    elif typ == "BOOLEAN":
        return [] if isinstance(value, bool) else [f"{where}: expects true or false"]
    elif typ == "STRING":
        return [] if isinstance(value, str) else [f"{where}: expects text"]
    else:
        return [f"{where}: expects a link to a {typ}"]
    lo, hi = opts.get("min"), opts.get("max")
    if lo is not None and value < lo:
        return [f"{where}: {value} is below {lo}"]
    if hi is not None and value > hi:
        return [f"{where}: {value} is above {hi}"]
    return []
