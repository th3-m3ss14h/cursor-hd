# cursor-hd

A local web UI for inspecting installed Xcursor themes and upgrading selected themes to the GNOME 51 scalable-cursor layout in one batch.

![Theme browser after a completed 10-theme batch: upgraded themes now report 256px native maximum and Scalable ready, with the batch log below](docs/screenshots/theme-browser.png)

*The theme browser: filter and multi-select installed themes, inspect the upgrade plan and cursor inventory, and watch live batch progress.*

> [!WARNING]
> **Back up your cursor themes before processing them.** Writable user themes are replaced in place when an upgrade succeeds, including themes under `~/.icons` and `$XDG_DATA_HOME/icons`. A successful upgrade does not retain an automatic copy of the original theme. The app attempts rollback when it detects a processing failure, but that is not a substitute for a backup. Back up `~/.icons` and your XDG icon directory (usually `~/.local/share/icons`) before using **Upgrade**. System themes in protected directories are left unchanged and receive a same-ID user-local override.

## What the app does

- Scans the usual cursor-theme locations in a background task: `~/.icons`, `$XDG_DATA_HOME/icons`, `/usr/share/icons`, and `/usr/local/share/icons`. The page shows live directory, theme, and cursor-level progress during both legacy inventory and GNOME 51 metadata validation instead of appearing frozen until the entire scan returns.
- Lists each effective theme ID once. A GNOME 51 companion that points back to a legacy theme is folded into that theme's row. When a full user-local override shadows the system copy, only the effective user-local theme is listed.
- Shows the installed path, theme ID/display name, cursor count, aliases, animations, native size range, and GNOME 51 validation status. The details pane includes the cursor inventory and missing/invalid scalable names.
- Lets you select several themes and upgrade them using one action. The selection is preflighted for path collisions before any work begins.
- Processes every real cursor in each selected theme through extract → upscale → Xcursor rebuild, then creates/refreshes `cursors_scalable/` with GNOME 51 metadata and aliases. Upscaling uses the method you choose for the batch: Lanczos (CPU, the default) or Real-ESRGAN (GPU, opt-in, requires a supported checkpoint in the repository's `weights/` directory). It preserves valid original vector artwork where available and keeps legacy cursor aliases.
- Replaces writable user themes in place. Themes installed under protected locations, such as `/usr/share/icons`, are upgraded as same-ID overrides under `$XDG_DATA_HOME/icons/<theme-id>`. The protected source is left unchanged. No `HD`, `COPY`, or `GNOME 51` duplicate theme names are created.
- Refuses to overwrite an unrelated theme that already occupies the required user-local theme ID. Resolve the conflict before retrying.

The generated scalable layer uses the largest cursor-size group produced by the Xcursor rebuild. When no source vector artwork exists, each SVG holds the upscaled raster frame. That makes it compatible with the scalable metadata loader, but it is not vectorization: it cannot invent image detail that isn't present in the source.

## Optional AI upscaling

Create or use the repository-root `weights/` directory and place a supported Real-ESRGAN checkpoint there. The recommended general-purpose checkpoint is [`RealESRGAN_x4plus.pth` from the official upstream v0.1.0 release](https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth). Save it as `weights/RealESRGAN_x4plus.pth`. The checkpoint is not bundled in this repository; `.pth` model files are ignored by Git because they are large binary assets.

The published SHA-256 reference for this upstream checkpoint is:

```text
4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1
```

The upstream release page does not display this checksum; the value is listed in [model metadata that identifies the upstream asset](https://huggingface.co/skillsafe-ai/realesrgan-x4plus/blame/main/README.md). Verify a downloaded file with `sha256sum weights/RealESRGAN_x4plus.pth` and compare the output to the value above.

When you start a batch, choose one upscaling method in the confirmation dialog.

![Upgrade confirmation dialog with the Lanczos and Real-ESRGAN upscaling options](docs/screenshots/upgrade-dialog.png)
 **Lanczos (CPU)** is the default and always works without an AI runtime. **Real-ESRGAN (GPU)** is an explicit opt-in. The dialog checks this local `weights/` directory to show whether a supported checkpoint is available; it never changes your selected method based on model presence. When GPU mode is selected, it prefers `RealESRGAN_x4plus.pth`, then recognizes `RealESRGAN_x4plus_anime_6B.pth` and `RealESRGAN_x2plus.pth`.

Before any theme is changed in GPU mode, the app loads the selected checkpoint, checks the CUDA-enabled PyTorch/Real-ESRGAN runtime, and executes a small test inference. If the checkpoint, dependencies, or GPU runtime fail that preflight, the batch is rejected before theme processing begins. It never silently switches a requested Real-ESRGAN run to Lanczos. The checkpoint is not enough by itself: compatible CUDA-enabled PyTorch, torchvision, BasicSR, Real-ESRGAN, NumPy and OpenCV must also be available in the same Python environment.

## Install and run

Requirements: Python 3.10+, Flask, Pillow, and `xcursorgen`.

On Arch Linux:

```sh
sudo pacman -S python-flask python-pillow xorg-xcursorgen
python3 app.py
```

Or use the project requirements file in a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python3 app.py
```

Open <http://127.0.0.1:5057>.

The web server binds to `127.0.0.1` only. The API also checks loopback Host/Origin values and requires JSON content types for mutations to reject cross-site form posts. Do not expose it to an untrusted network. Run it as your normal user. System themes are handled via user-local overrides, so root is not required.

## Tests

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile *.py tests/*.py
node --check static/app.js
```

The MIT-licensed project includes 43 unit and regression tests covering model discovery and priority, scalable metadata validation, vector preservation, theme-ID and companion deduplication, collision checks, user-theme in-place upgrade, and system-theme override creation. Regression tests also cover symlinked `~/.icons`, legacy index encoding and section formatting, hidden swap backups, malformed Xcursor TOC counts, output suffix handling, loopback API request validation, temporary-file cleanup, cursor names containing spaces, and scan progress callbacks. Tests use a stub `xcursorgen` executable, so they exercise the application pipeline without modifying the host's installed cursor themes.

**Testing scope:** the project targets Arch Linux with GNOME 51. The automated tests do not validate the real `xcursorgen` binary or prove that generated cursors render correctly in a live GNOME session. The Real-ESRGAN path is covered by mocked pipeline tests, not a full GPU/model integration test. Other distributions and desktop environments have not been verified.

## Main modules

- `scanner.py`: theme discovery and inventory, native-size/frame parsing, and GNOME 51 status.
- `upgrade.py`: safe output planning and the unified upgrade operation.
- `orchestrator.py`: Xcursor extraction, upscaling, rebuild, alias preservation, theme metadata, and staged directory replacement.
- `gnome51.py`: scalable cursor metadata/SVG generation, CSS-name aliases, preservation of native vectors, and validation.
- `xcur_extract.py`, `xcur_upscale.py`, `xcur_rebuild.py`: Xcursor parser, image processing, and `xcursorgen` integration.
- `app.py`: local Flask API, explicit upscaling-method selector, Real-ESRGAN preflight, single batch-upgrade workflow, output path locks, and progress reporting.
- `model_utils.py`: project-local model discovery and explicit upscaler-choice validation.
- `local_security.py`: loopback Host/Origin and JSON-mutation checks for the local API.
- `templates/index.html`, `static/app.js`, `static/style.css`: single-workflow selection UI, inspection panel, confirmation, and live progress.
