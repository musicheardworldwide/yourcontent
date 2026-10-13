"""PURE: which features work, with reasons (§6 /api/health, §9 Health). Owner: WP3.

`compute()` builds each feature's real graph from the model table, then checks it against /object_info:
every node class must exist, every model file must be in its loader's list, and the graph must validate.
Reasons are short and name the missing file or node, so the owner knows what to fetch.
"""
from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING

from . import getmodels, graphs

if TYPE_CHECKING:  # type only; features stays pure
    from .models import Models

# ffmpeg filters the export graphs use (§8). WP3 owns this list.
REQUIRED_FILTERS: tuple[str, ...] = (
    "trim", "setpts", "scale", "crop", "setsar", "fps", "tpad", "settb", "format", "split", "gblur",
    "overlay", "loop", "xfade", "concat", "color", "aformat", "atrim", "asetpts", "adelay", "apad",
    "afade", "loudnorm", "aresample", "ebur128",
)

# The object_info classes the graphs (and the fixture) need. §14.
FIXTURE_CLASSES: tuple[str, ...] = (
    "UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "TextEncodeQwenImage21", "EmptyLatentImage",
    "EmptySD3LatentImage",
    "KSampler", "KSamplerAdvanced", "VAEDecode", "SaveImage", "LoadImage", "LoraLoaderModelOnly",
    "ModelSamplingSD3", "ModelSamplingAuraFlow", "CLIPTextEncode", "WanImageToVideo", "VHS_VideoCombine",
    "TextEncodeAceStepAudio1.5", "EmptyAceStep1.5LatentAudio", "ConditioningZeroOut", "VAEDecodeAudio",
    "SaveAudio", "SaveAudioAdvanced", "SaveAudioMP3",
)

NODE_HINTS = {
    "VHS_VideoCombine": "VHS_VideoCombine node (install ComfyUI-VideoHelperSuite)",
    "TextEncodeAceStepAudio1.5": "TextEncodeAceStepAudio1.5 node (update ComfyUI; 0.37.0 has it)",
    "EmptyAceStep1.5LatentAudio": "EmptyAceStep1.5LatentAudio node (update ComfyUI; 0.37.0 has it)",
    "TextEncodeQwenImage21": "TextEncodeQwenImage21 node (update ComfyUI; 0.37.0 has it)",
    "WanImageToVideo": "WanImageToVideo node (update ComfyUI)",
}
OFF = "ComfyUI is not reachable"


def pick_saver(object_infos: dict) -> str | None:
    """SaveAudio (lossless FLAC, deprecated but present) first, else SaveAudioAdvanced."""
    for s in graphs.SAVERS:
        if object_infos.get(s):
            return s
    return None


def _loader_files(object_infos: dict, ct: str, name: str) -> list | None:
    info = object_infos.get(ct) or {}
    inp = info.get("input") or {}
    spec = (inp.get("required") or {}).get(name) or (inp.get("optional") or {}).get(name)
    return graphs._spec_options(spec) if spec else None


def missing_for(graph: dict, object_infos: dict, set_name: str | None = None) -> list[str]:
    """Plain reasons a graph cannot run here: missing nodes, missing model files, then any other mismatch.
    A missing file the download manifest knows gets its size and the get-models command (`set_name` picks the
    set: pictures, clips or music); any other file keeps the plain `name (models/folder)` form."""
    out: list[str] = []
    absent = sorted({n["class_type"] for n in graph.values() if not object_infos.get(n["class_type"])})
    for ct in absent:
        out.append(NODE_HINTS.get(ct, f"{ct} node"))
    file_inputs = set()
    for nid, node in graph.items():
        ct = node["class_type"]
        for name, value in (node.get("inputs") or {}).items():
            folder = graphs.MODEL_INPUTS.get((ct, name))
            if not folder or not isinstance(value, str):
                continue
            file_inputs.add((nid, name))
            files = _loader_files(object_infos, ct, name)
            if files is not None and value not in files:
                more = getmodels.hint(value, set_name)
                msg = f"{value} (models/{folder}, {more}" if more else f"{value} (models/{folder})"
                if msg not in out:
                    out.append(msg)
    if not absent:
        for err in graphs.validate(graph, object_infos):
            m = re.match(r"node (\S+) \S+?\.(\S+):", err)
            if m and (m.group(1), m.group(2)) in file_inputs:
                continue   # already reported as a missing file
            out.append(err)
    return out


def compute(object_infos: dict, models: "Models", ff: dict) -> dict:
    """Feature health for /api/health (shape in §6). `object_infos` is {class: info} ({} when ComfyUI is off);
    `ff` is media.ffmpeg_caps()."""
    oi = object_infos or {}
    up = bool(oi)

    def gate(miss: list[str]) -> dict:
        if not up:
            miss = [OFF]
        return {"ok": not miss, "missing": miss}

    variants = {}
    for vid, v in models.image_variants().items():
        g = graphs.image_graph(v, "test", "", 1024, 1024, 4, 1, ["example.png"], "yourcontent/img")
        variants[vid] = {"label": v["label"], **gate(missing_for(g, oi, "pictures"))}
    picture = {"ok": any(x["ok"] for x in variants.values()), "variants": variants,
               "missing": [] if any(x["ok"] for x in variants.values()) else
               (["no picture model is set up"] if not variants else
                sorted({m for x in variants.values() for m in x["missing"]}))}

    video = models.video
    if video.get("unet") and video.get("lora") and video.get("clip") and video.get("vae"):
        clip = gate(missing_for(graphs.video_graph(video, "example.png", "test", 832, 480, 49, 1,
                                                   "yourcontent/clip"), oi, "clips"))
    else:
        clip = {"ok": False, "missing": ["no clip model is set up"]}

    ff = ff or {}
    missing_filters = list(ff.get("missing_filters") or [])
    missing_encoders = list(ff.get("missing_encoders") or [])
    have_ff = bool(ff.get("version"))
    export = {"ok": have_ff and not missing_filters and not missing_encoders,
              "missing_filters": missing_filters if have_ff else ["ffmpeg not found"],
              "missing_encoders": missing_encoders, "ffmpeg": ff.get("version") or ""}

    extend_missing = list(clip["missing"])
    if not have_ff:
        extend_missing.append("ffmpeg not found")
    extend = {"ok": clip["ok"] and have_ff, "missing": extend_missing}

    mus = models.music
    saver = pick_saver(oi) if up else None
    if not all(mus.get(k) for k in ("unet", "clip1", "clip2", "vae")):
        music = {"ok": False, "saver": saver, "missing": ["no music model is set up"]}
    else:
        g = graphs.music_graph(mus, "test", "", 120, 10.0, 1, saver=saver or "SaveAudio")
        miss = missing_for(g, oi, "music")
        if up and saver is None:
            miss = [m for m in miss if not m.startswith("SaveAudio")] + ["SaveAudio or SaveAudioAdvanced node"]
        music = {"saver": saver, **gate(miss)}

    return {"picture": picture, "clip": clip, "extend": extend, "music": music, "export": export}


def download_bytes(missing: list) -> int:
    """Bytes still to download for the missing-file reasons the manifest knows (each file counted once)."""
    names = {str(m).split(" (models/", 1)[0] for m in missing or [] if " (models/" in str(m)}
    return sum(getmodels.bytes_of(n) for n in names)


# ---------------------------------------------------------------------------------------------- fixture

def fixture_subset(object_infos: dict, keep_files: set[str] | None = None) -> dict:
    """The object_info subset tests use (§14): FIXTURE_CLASSES only, model-file lists reduced to `keep_files`
    (so no local filenames end up in the repo), and LoadImage's folder listing replaced by one dummy name."""
    keep = set(keep_files or ())
    out = {}
    for ct in FIXTURE_CLASSES:
        if ct not in object_infos:
            continue
        info = copy.deepcopy(object_infos[ct])
        for sec in ("required", "optional"):
            for name, spec in ((info.get("input") or {}).get(sec) or {}).items():
                if (ct, name) in graphs.MODEL_INPUTS or (ct, name) in graphs.UNCHECKED_COMBOS:
                    opts = graphs._spec_options(spec)
                    if opts is None:
                        continue
                    if (ct, name) in graphs.UNCHECKED_COMBOS:
                        new = ["example.png"]
                    else:
                        new = [f for f in opts if f in keep or f == "pixel_space"]
                    if isinstance(spec[0], list):
                        spec[0] = new
                    else:
                        spec[1]["options"] = new
        out[ct] = info
    return out
