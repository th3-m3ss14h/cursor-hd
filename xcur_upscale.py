#!/usr/bin/env python3
"""
Upscale extracted cursor frame PNGs, producing a full ladder of nominal
sizes (not just one giant size) so the resulting .cursor file responds
correctly to the desktop's cursor-size setting. Xcursor/libXcursor picks
whichever embedded nominal size is closest to what's requested - a theme
with only one embedded size (e.g. 128px) always renders at 128px no
matter what the slider says, which is the "too big and doesn't scale"
symptom.

Strategy per source frame:
  1. Run the expensive upscale (Real-ESRGAN or Lanczos) ONCE, to the
     largest size in the ladder ("master").
  2. Derive every smaller ladder size from that master via a cheap
     Lanczos downsample - still benefits from the AI upscale's cleanup
     even at sizes below the max, and never touches torch/basicsr more
     than once per frame regardless of ladder length.

Backends:
  lanczos     - PIL.Image.LANCZOS, always available, decent baseline.
  realesrgan  - Real-ESRGAN (x4plus / x2plus / anime_6B), needs
                `realesrgan` + `basicsr` + a CUDA torch install. Meant to
                run on the actual GPU box, not the dev sandbox.

Usage:
    python3 xcur_upscale.py <manifest.json> <frame_dir> <out_dir> <scale> [backend] [model_path]
"""
import json
import os
import sys
import threading

from PIL import Image

# Only sizes >= the cursor's native size are ever generated - we're
# adding headroom for larger cursor-size settings / HiDPI, not
# downscaling below what the original theme already covered.
STANDARD_LADDER = [16, 20, 24, 28, 32, 40, 48, 56, 64, 72, 80, 96,
                    112, 128, 144, 160, 192, 224, 256]



def upscale_lanczos_to(img, target_w, target_h):
    return img.resize((target_w, target_h), Image.LANCZOS)


_realesrgan_upsampler = None
_realesrgan_loaded_path = None
_realesrgan_init_lock = threading.RLock()
_realesrgan_inference_lock = threading.Lock()

# (num_block, native netscale) per known Real-ESRGAN checkpoint. Keyed by
# filename stem so callers can just point at whatever .pth they already
# have (e.g. from an existing ComfyUI/upscale_models dir) without needing
# to know the architecture details.
REALESRGAN_CONFIGS = {
    "RealESRGAN_x4plus":            {"num_block": 23, "netscale": 4},
    "RealESRGAN_x4plus_anime_6B":   {"num_block": 6,  "netscale": 4},
    "RealESRGAN_x2plus":            {"num_block": 23, "netscale": 2},
}


def _resolve_realesrgan_config(model_path):
    stem = os.path.splitext(os.path.basename(model_path))[0]
    if stem not in REALESRGAN_CONFIGS:
        raise ValueError(
            f"unknown Real-ESRGAN checkpoint '{stem}' - add its "
            f"(num_block, netscale) to REALESRGAN_CONFIGS in xcur_upscale.py. "
            f"Known: {list(REALESRGAN_CONFIGS)}"
        )
    return REALESRGAN_CONFIGS[stem]


def _ensure_realesrgan_upsampler(model_path):
    """Import the GPU runtime and initialize the requested checkpoint once."""
    global _realesrgan_upsampler, _realesrgan_loaded_path

    if not model_path:
        raise ValueError("Real-ESRGAN requires a model checkpoint path")
    if not os.path.isfile(model_path):
        raise ValueError(f"Real-ESRGAN model not found: {model_path}")
    if _realesrgan_upsampler is not None and _realesrgan_loaded_path == model_path:
        return _realesrgan_upsampler

    with _realesrgan_init_lock:
        if _realesrgan_upsampler is not None and _realesrgan_loaded_path == model_path:
            return _realesrgan_upsampler
        try:
            import torch
            import torchvision.transforms.functional as tv_functional

            # BasicSR 1.4.x imports the old torchvision.transforms.functional_tensor
            # module. torchvision removed that public module, so alias the modern
            # functional module before importing BasicSR/Real-ESRGAN. This keeps
            # the compatibility fix local to this application.
            if "torchvision.transforms.functional_tensor" not in sys.modules:
                sys.modules["torchvision.transforms.functional_tensor"] = tv_functional

            from basicsr.archs.rrdbnet_arch import RRDBNet
            from realesrgan import RealESRGANer
        except Exception as exc:
            raise RuntimeError(
                "Real-ESRGAN is selected, but its Python runtime could not be loaded. "
                "Install compatible CUDA-enabled PyTorch, torchvision, BasicSR, "
                f"Real-ESRGAN, NumPy and OpenCV packages. Details: {exc}"
            ) from exc

        if not torch.cuda.is_available():
            raise RuntimeError(
                f"Real-ESRGAN was selected, but CUDA is unavailable to this Python "
                f"environment (torch {getattr(torch, '__version__', 'unknown')}). "
                "Select Lanczos (CPU) or repair the CUDA/PyTorch environment."
            )

        cfg = _resolve_realesrgan_config(model_path)
        try:
            model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                            num_block=cfg["num_block"], num_grow_ch=32,
                            scale=cfg["netscale"])
            upsampler = RealESRGANer(
                scale=cfg["netscale"],
                model_path=model_path,
                model=model,
                tile=0,
                tile_pad=10,
                pre_pad=0,
                half=True,
                device=torch.device("cuda"),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Real-ESRGAN could not load checkpoint {os.path.basename(model_path)}: {exc}"
            ) from exc

        _realesrgan_upsampler = upsampler
        _realesrgan_loaded_path = model_path
        return _realesrgan_upsampler


def preflight_realesrgan(model_path):
    """Fully load and briefly run the selected model before a batch can mutate themes."""
    if not model_path or not os.path.isfile(model_path):
        raise RuntimeError(
            "Real-ESRGAN was selected, but no supported readable checkpoint was found "
            "in the project's ./weights directory. No themes were changed."
        )
    try:
        upsampler = _ensure_realesrgan_upsampler(model_path)
        import numpy as np
        import torch

        # Exercise an actual small GPU inference, not just torch.cuda.is_available().
        # This catches bad checkpoints and common runtime/driver failures up front.
        probe = np.zeros((32, 32, 3), dtype=np.uint8)
        with _realesrgan_inference_lock:
            output, _ = upsampler.enhance(probe, outscale=_resolve_realesrgan_config(model_path)["netscale"])
            torch.cuda.synchronize()
        if output is None or getattr(output, "size", 0) == 0:
            raise RuntimeError("the checkpoint returned an empty test image")
    except Exception as exc:
        raise RuntimeError(
            "Real-ESRGAN preflight failed before any themes were changed. "
            f"Check the checkpoint, Python dependencies, GPU and CUDA driver. Details: {exc}"
        ) from exc
    return {"backend": "realesrgan", "model_path": model_path, "cuda_test": "passed"}


def upscale_realesrgan_to(img, target_w, target_h, model_path=None):
    """AI-upscale the RGB channels on CUDA and resize alpha with Lanczos."""
    upsampler = _ensure_realesrgan_upsampler(model_path)
    import numpy as np

    # Cursors are RGBA; RRDBNet is RGB-only, so preserve alpha separately to
    # avoid halos around transparent edges.
    rgb = img.convert("RGB")
    alpha = img.getchannel("A")

    outscale = target_w / img.width
    arr = np.array(rgb)[:, :, ::-1]  # RGB -> BGR for Real-ESRGAN's cv2 convention
    with _realesrgan_inference_lock:
        out_arr, _ = upsampler.enhance(arr, outscale=outscale)
    out_rgb = Image.fromarray(out_arr[:, :, ::-1], "RGB")

    if out_rgb.size != (target_w, target_h):
        out_rgb = out_rgb.resize((target_w, target_h), Image.LANCZOS)

    out_alpha = alpha.resize((target_w, target_h), Image.LANCZOS)
    out_rgb.putalpha(out_alpha)
    return out_rgb


def _build_target_plan(manifest, scale):
    """Return one source-size/frame group for every output nominal size.

    A cursor can contain several native nominal sizes. Expanding each native
    group independently creates duplicate nominal sizes, which xcursorgen
    interprets as animation frames. Instead, each output nominal size gets
    exactly one source group: the largest native size not exceeding the
    target, or the smallest native size when the target is below it.
    """
    native_sizes = sorted(int(s) for s in manifest["sizes"])
    if not native_sizes:
        raise ValueError("cursor manifest contains no native sizes")
    if any(s <= 0 for s in native_sizes):
        raise ValueError(f"invalid native sizes: {native_sizes}")

    max_target = native_sizes[-1] * scale
    targets = sorted(set(
        s for s in STANDARD_LADDER if native_sizes[0] <= s <= max_target
    ) | set(native_sizes) | {max_target})

    plan = []
    for target in targets:
        eligible = [s for s in native_sizes if s <= target]
        source_size = eligible[-1] if eligible else native_sizes[0]
        plan.append((target, source_size))
    return plan


def upscale_manifest(manifest_path, frame_dir, out_dir, scale, backend="lanczos",
                      model_path=None):
    if backend not in ("lanczos", "realesrgan"):
        raise ValueError(f"unsupported backend: {backend}")
    if not isinstance(scale, int) or scale < 1 or scale > 16:
        raise ValueError(f"scale must be an integer from 1 to 16, got {scale!r}")
    if backend == "realesrgan" and not model_path:
        raise ValueError("backend=realesrgan requires model_path")
    if backend == "realesrgan" and not os.path.isfile(model_path):
        raise ValueError(f"Real-ESRGAN model not found: {model_path}")

    with open(manifest_path) as f:
        manifest = json.load(f)

    os.makedirs(out_dir, exist_ok=True)
    out_manifest = {"source": manifest["source"], "scale": scale,
                    "backend": backend, "sizes": {}}

    plan = _build_target_plan(manifest, scale)
    native_cache = {}

    for target_size, source_size in plan:
        source_frames = manifest["sizes"][str(source_size)]
        factor = target_size / source_size
        output_frames = []

        for frame_idx, fr in enumerate(source_frames):
            img = Image.open(os.path.join(frame_dir, fr["file"])).convert("RGBA")
            target_w = max(1, round(img.width * factor))
            target_h = max(1, round(img.height * factor))

            # Cache the expensive AI/Lanczos master for each source frame at
            # the largest output size that this source group will feed.
            source_targets = [t for t, s in plan if s == source_size]
            master_target = max(source_targets)
            master_factor = master_target / source_size
            cache_key = (source_size, frame_idx, master_target, backend, model_path)
            master = native_cache.get(cache_key)
            if master is None:
                master_w = max(1, round(img.width * master_factor))
                master_h = max(1, round(img.height * master_factor))
                if backend == "realesrgan":
                    master = upscale_realesrgan_to(img, master_w, master_h, model_path)
                else:
                    master = upscale_lanczos_to(img, master_w, master_h)
                native_cache[cache_key] = master

            if (target_w, target_h) == master.size:
                frame_img = master
            else:
                frame_img = master.resize((target_w, target_h), Image.LANCZOS)

            out_name = fr["file"].removesuffix(".png") + (
                f".sz{target_size}.f{frame_idx:03d}.png"
            )
            frame_img.save(os.path.join(out_dir, out_name))
            output_frames.append({
                "file": out_name,
                "width": target_w,
                "height": target_h,
                "xhot": round(fr["xhot"] * factor),
                "yhot": round(fr["yhot"] * factor),
                "delay": fr["delay"],
            })

        out_manifest["sizes"][str(target_size)] = output_frames

    out_manifest_path = os.path.join(
        out_dir,
        os.path.basename(manifest_path).removesuffix(".json") + f".x{scale}.json",
    )
    with open(out_manifest_path, "w") as f:
        json.dump(out_manifest, f, indent=2)
    return out_manifest_path


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(f"usage: {sys.argv[0]} <manifest.json> <frame_dir> <out_dir> "
              f"<scale> [backend=lanczos|realesrgan] [model_path]", file=sys.stderr)
        sys.exit(1)
    manifest_path, frame_dir, out_dir, scale = sys.argv[1:5]
    backend = sys.argv[5] if len(sys.argv) > 5 else "lanczos"
    model_path = sys.argv[6] if len(sys.argv) > 6 else None
    result = upscale_manifest(manifest_path, frame_dir, out_dir, int(scale), backend,
                               model_path=model_path)
    print(result)
