#!/usr/bin/env python3
"""Discover supported optional Real-ESRGAN checkpoints in the project weights/."""
from __future__ import annotations

import os
from pathlib import Path


# Prefer the general-purpose x4 model requested by the project README. The
# other supported models are fallback choices when it is not present.
MODEL_PREFERENCE = (
    "RealESRGAN_x4plus",
    "RealESRGAN_x4plus_anime_6B",
    "RealESRGAN_x2plus",
)


def weights_directory() -> str:
    """Return the repository-local weights directory, independent of cwd."""
    return str(Path(__file__).resolve().parent / "weights")


def available_models(weights_dir: str | os.PathLike[str] | None = None) -> list[dict]:
    """List only checkpoints this application knows how to instantiate."""
    from xcur_upscale import REALESRGAN_CONFIGS

    directory = Path(weights_dir) if weights_dir is not None else Path(weights_directory())
    if not directory.is_dir():
        return []

    found: dict[str, dict] = {}
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []

    for item in children:
        if not item.is_file() or item.suffix.lower() != ".pth":
            continue
        config = REALESRGAN_CONFIGS.get(item.stem)
        if config is None:
            continue
        try:
            full_path = str(item.resolve(strict=True))
        except OSError:
            continue
        if full_path in found:
            continue
        found[full_path] = {
            "path": full_path,
            "name": item.name,
            "known_config": dict(config),
        }

    preference = {stem: index for index, stem in enumerate(MODEL_PREFERENCE)}
    return sorted(
        found.values(),
        key=lambda model: (
            preference.get(Path(model["name"]).stem, len(preference)),
            model["name"].casefold(),
        ),
    )


def preferred_model_path(weights_dir: str | os.PathLike[str] | None = None) -> str | None:
    """Return the highest-priority supported local checkpoint, if present."""
    models = available_models(weights_dir)
    return models[0]["path"] if models else None


def resolve_backend_choice(backend: str, weights_dir: str | os.PathLike[str] | None = None) -> tuple[str, str | None]:
    """Resolve a user-selected backend; never switch methods implicitly."""
    if backend == "lanczos":
        return "lanczos", None
    if backend != "realesrgan":
        raise ValueError(f"unsupported upscale method: {backend!r}; choose 'lanczos' or 'realesrgan'")
    model_path = preferred_model_path(weights_dir)
    if not model_path:
        raise ValueError(
            "Real-ESRGAN was selected, but no supported checkpoint was found in ./weights. "
            "Download a supported model or select Lanczos (CPU)."
        )
    return "realesrgan", model_path

