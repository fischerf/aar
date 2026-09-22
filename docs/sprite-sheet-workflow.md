# Recipe: transparent sprite sheets, and using them in a game

End-to-end workflow for generating a transparent (RGBA) sprite sheet with the
[qwen-image extension](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md)
and having the agent build a game around it — in one conversation, without
leaving aar.

The shape of it:

```
you ──► aar (chat model, e.g. gemma4 on Ollama)
          ├─ spawn_agent("illustrator", "...")  ──► sub-agent ──► image_generate
          │                                                         └─► qwen-image sidecar (GPU)
          │                                            ◄── "sprites.png"
          └─ write_file index.html / game.js        ──► a game that loads sprites.png
```

The image tools return a **file path**, never pixels — so the sprite and the
code it belongs to have to end up in the same directory. That is what makes the
`out_dir` default (aar's working directory) the setting that matters most here.

---

## 1. Prerequisites

```bash
pip install aar-ext-qwen-image     # into aar's environment
aar extensions list                # should show "qwen_image (entrypoint)"
```

The sidecar needs its own venv with `torch` + `diffusers` + `pillow` — see the
extension README's [Setup](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md#setup)
section. aar itself never imports the ML stack.

---

## 2. Configure once

### `~/.aar/qwen-image.json` — where images land, and on which GPU

```json
{
  "url": "http://127.0.0.1:8770",
  "autostart": "on_demand",
  "python": "~/.aar/qwen-image/.venv/Scripts/python.exe",
  "device": "cuda:0",
  "hip_visible_devices": "0",
  "quant": "Q4_K_M",
  "offload": "model",
  "out_dir": "",
  "steps": 30,
  "request_timeout": 900,
  "idle_timeout": 0
}
```

- **`out_dir: ""`** means aar's current working directory. Start aar in the game
  repo and the sprite lands next to the code; `<img src="sprites.png">` then just
  works. A relative value like `"assets"` puts it in a subdirectory of the cwd.
- **`device` / `hip_visible_devices`** pin the renderer to one card. On a
  mixed-vendor box, `"device": "auto"` is not enough: ROCm addresses AMD cards as
  `cuda:N`, and a stray `CUDA_VISIBLE_DEVICES` in your environment will hide the
  GPU from HIP as well. Check what the sidecar actually sees:

  ```bash
  ~/.aar/qwen-image/.venv/Scripts/python.exe path/to/server.py --list-devices
  ```

  If that prints only `cpu`, the renderer will run on CPU (minutes per image,
  quietly). See [Two GPUs, two jobs](#two-gpus-two-jobs) below.
- **`quant: "Q4_K_M"`** drops the pipeline from ~31 GiB to ~22 GiB, which is the
  difference between comfortable and strained on a 24 GB card.
- **`idle_timeout: 0`** keeps the server alive between renders instead of exiting
  and rebuilding the pipeline. Matters a lot on a slow bus — see
  [Keeping the model in VRAM](#keeping-the-model-in-vram).

### `~/.aar/config.json` — an illustrator sub-agent

Optional but worth it: it keeps prompt iterations and retries out of the coding
model's context.

```json
{
  "subagents": {
    "enabled": true,
    "max_depth": 1,
    "agents": {
      "illustrator": {
        "description": "Generates one image from a description and returns the saved file path",
        "tools": [],
        "extension_tools": ["image_generate", "image_edit"],
        "system_prompt": "You are an image generator. Call image_generate exactly once, passing BOTH width and height explicitly, then reply with only the saved file path. Never explain.",
        "max_steps": 6,
        "timeout": 1800
      }
    }
  }
}
```

- `tools: []` removes every built-in — no `read_file`, no `bash`.
- `extension_tools` is the part people miss: **an installed extension registers
  into every agent in the process**, sub-agents included. Without this list the
  illustrator also inherits whatever other extensions you have installed.
- The "BOTH width and height" instruction is not padding. Small local models
  routinely pass only `width` and let `height` fall back to the config default,
  which silently gives you a 512x1024 image when you asked for 512x512.
- `timeout` becomes the tool's `timeout_s`, so a long render is not cut off by
  `tools.command_timeout`.

---

## 3. Generate the sheet

Start aar **in the game's directory** — that is what decides where the PNG goes:

```bash
cd ~/games/dino
aar tui
```

```
> Use spawn_agent with the illustrator: width=1024, height=512, transparent=true,
  steps=30, seed=2024, out='sprites.png', prompt='pixel-art sprite sheet, one
  horizontal row of 4 evenly spaced frames of a small green dinosaur running,
  side view facing right, bold dark outlines, flat colours, no background'
```

The first render of a session also starts the sidecar (~45 s to load a cached
Q4_K_M pipeline; several minutes the first time ever, while ~22 GiB downloads).
A 1024x512 render at 30 steps takes roughly 3–5 minutes on a 24 GB card with
`offload: "model"`.

### What actually matters in the prompt

| Say this | Why |
|---|---|
| `transparent=true` | Without it you get an opaque background. There is no pipeline argument for alpha — the flag wraps your prompt in the model card's RGBA wording |
| `one horizontal row of N frames`, `evenly spaced` | The model drifts into a diagonal arc otherwise |
| `bold dark outlines` | Keeps sprite edges readable against any level art |
| `side view facing right` | Pins the facing so frames agree with each other |
| *don't* also ask for a background colour | It competes with the alpha channel |

Verify you got a real cutout rather than a white rectangle:

```python
from PIL import Image
im = Image.open("sprites.png").convert("RGBA")
px = list(im.getchannel("A").get_flattened_data())
print(im.size, "fully transparent: %.1f%%" % (100 * px.count(0) / len(px)))
```

Anything in the 15–35% range is a normal cutout for a single row of sprites. A
flat `0.0%` means the render came back opaque — re-run, or fall back to the
chroma-key approach at the end of this page.

---

## 4. Build the game around it

Same conversation, next message:

```
> Write index.html: a canvas endless-runner that loads sprites.png from this
  directory, treats it as 4 frames side by side (frame width = image width / 4),
  cycles frames while running, space bar to jump, and shows a score.
```

The slicing maths the model should produce:

```js
const sheet = new Image();
sheet.src = "sprites.png";                 // same directory as index.html
const FRAMES = 4;

function drawPlayer(ctx, x, y, w, h, frame) {
  const fw = sheet.width / FRAMES;         // frame width
  const fh = sheet.height;
  ctx.imageSmoothingEnabled = false;       // keep pixel art crisp
  ctx.drawImage(sheet, frame * fw, 0, fw, fh, x, y, w, h);
}
```

`imageSmoothingEnabled = false` matters: the render is a raster *imitating*
pixel art, and the browser's default bilinear scaling turns its edges to mush.

---

## 5. Cleaning the sheet up

Two known limitations, both worth planning around:

**Frames are not on a uniform grid.** The model places them by eye, so slicing at
`width / N` will not line up perfectly, and it sometimes returns a different
frame count than you asked for. For production sheets, generate **one pose per
render with the same seed** and composite the grid yourself — that also keeps the
character consistent frame to frame:

```
> Use spawn_agent with the illustrator four times, same seed 2024, same prompt
  except the pose: crouch, launch, mid-air, land. Save as f1.png … f4.png.
  Then write a Python script that pastes them into one 4-wide sheet.
```

**It is not true pixel art.** Edges are anti-aliased, with semi-transparent
pixels around the outline. If your engine needs a hard 1-bit mask, downscale with
`NEAREST` and threshold the alpha:

```python
from PIL import Image

im = Image.open("sprites.png").convert("RGBA")
a = im.getchannel("A").point(lambda v: 255 if v > 128 else 0)
im.putalpha(a)
im.resize((im.width // 2, im.height // 2), Image.NEAREST).save("sprites-hard.png")
```

**Chroma-key fallback** — if a render comes back opaque despite `transparent=true`,
re-render on a `flat solid magenta background` and key it out.

---

## Two GPUs, two jobs

The natural setup for this workflow is a big card for the renderer and a second,
smaller one for the chat model — both busy at once, neither reloading. That split
is worth setting up deliberately, because the failure mode is silent: everything
still works, just on the CPU.

There are **two separate configurations** to get right. They do not talk to each
other.

### Configuration 1 — the chat model (Ollama)

Ollama is pinned through its **own process environment**, not through anything in
aar. On Windows these are user environment variables (`HKCU\Environment`); on
Linux, `Environment=` lines in the systemd unit.

| Variable | Value | Why |
|---|---|---|
| `CUDA_VISIBLE_DEVICES` | `0` | Expose the NVIDIA card to Ollama |
| `HIP_VISIBLE_DEVICES` | `-1` | Hide the AMD card, so Ollama leaves it to the renderer |
| `OLLAMA_FLASH_ATTENTION` | `1` | Required for KV-cache quantization |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves KV-cache VRAM — see the table below |

Two traps, both of which cost real debugging time:

- **`CUDA_VISIBLE_DEVICES` indexes CUDA devices only**, not GPUs in general. With
  one NVIDIA card the only valid value is `0`. Setting `1` because it is "the
  second GPU in the machine" makes CUDA report *no devices at all*
  (`cuInit failed: 100`) and the model falls back to CPU with no error surfaced.
- **HIP reads `CUDA_VISIBLE_DEVICES` as a fallback**, so one bad value hides the
  AMD card too. `HIP_VISIBLE_DEVICES` takes precedence when set, which is why the
  sidecar's own `hip_visible_devices` still reaches the card while the global
  value is `-1`.

Setting them on Windows, including the restart that actually applies them:

```powershell
[Environment]::SetEnvironmentVariable('CUDA_VISIBLE_DEVICES','0','User')
[Environment]::SetEnvironmentVariable('HIP_VISIBLE_DEVICES','-1','User')
[Environment]::SetEnvironmentVariable('OLLAMA_FLASH_ATTENTION','1','User')
[Environment]::SetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE','q8_0','User')

# restart the tray app *with* the new values in scope
Get-CimInstance Win32_Process -Filter "Name='ollama app.exe' OR Name='ollama.exe'" |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
```

On Linux, `systemctl edit ollama` and add them as `Environment=` lines, then
`systemctl restart ollama`.

**The environment must reach the process that spawns the server.** On Windows,
Ollama's tray app (`ollama app.exe`) launches `ollama.exe serve` and hands it its
own environment. Setting the registry value is therefore not enough on its own —
and note that relaunching the tray app from an *existing* shell inherits that
shell's environment, not the registry, so the variable has to be in scope at
launch (hence the `$env:` line above). Verify rather than assume:

```bash
grep -oE "OLLAMA_KV_CACHE_TYPE:[^ ]*" ~/AppData/Local/Ollama/server.log | tail -1
grep "inference compute" ~/AppData/Local/Ollama/server.log | tail -2
```

Then size the context. A model that "fits" on paper may not once the KV cache is
added, and the failure is a cliff rather than a slope: the moment Ollama cannot
fit everything, it falls back to a mostly-CPU layout. Measured on an 8B-class
model (≈3.0 GB resident) with a 6 GB card:

| KV cache | max `num_ctx` still 100% on GPU | above it |
|---|---|---|
| `f16` (default) | **10240** | ~31% on GPU, 8.9 GB total |
| `q8_0` | **16384** | ~33% on GPU, 8.8 GB total |

So `q8_0` buys 60% more context for free — quality impact is negligible, and it
only needs flash attention, which you want anyway. Put the chosen value in
**both** places in `~/.aar/config.json`:

```json
{
  "context_window": 16384,
  "providers": {
    "my-model": {
      "context_window": 16384,
      "extra": { "num_ctx": 16384, "supports_tools": true }
    }
  }
}
```

`num_ctx` is what Ollama allocates; the two `context_window` values are what aar
uses for its own token accounting and warnings. Leaving the top-level one at some
large default makes the token counter meaningless.

Confirm it landed:

```bash
curl -s -X POST localhost:11434/api/generate   -d '{"model":"my-model","prompt":"hi","stream":false,"options":{"num_ctx":16384}}' > /dev/null
curl -s localhost:11434/api/ps      # size_vram == size means fully resident
```

### Configuration 2 — the renderer (qwen-image sidecar)

The sidecar is pinned through `~/.aar/qwen-image.json`; the extension copies these
into the server process's own environment when it launches it, which is why they
win over the global values from Configuration 1.

```json
{
  "device": "cuda:0",
  "hip_visible_devices": "0",
  "quant": "Q4_K_M",
  "offload": "model",
  "width": 1024,
  "height": 512
}
```

ROCm addresses AMD cards as `cuda:N` too, so `"device": "cuda:0"` is correct for a
Radeon. `"device": "auto"` is not enough on a mixed box — it picks the card with
the most memory *among those it can see*, and a stray `CUDA_VISIBLE_DEVICES` may
mean it sees none. Check what it actually found:

```bash
curl -s 127.0.0.1:8770/health    # .device and .devices[]
# or, before it is running:
~/.aar/qwen-image/.venv/Scripts/python.exe path/to/server.py --list-devices
```

If that prints only `cpu`, renders will run on CPU — minutes per image, quietly.

#### Keeping the model in VRAM

`offload: "model"` (the default) is diffusers' `enable_model_cpu_offload`: the
weights live in system RAM and each component is moved onto the card as the
pipeline reaches it, then moved back. That is why **VRAM reads near-zero between
renders** — it is working as designed, not leaking.

On a normal PCIe x16 slot that shuffling is cheap. Over **Thunderbolt 3 to an
eGPU** it is not: the link gives roughly 2.5 GB/s against x16's ~25 GB/s, so every
render re-sends the pipeline across a bus ten times slower.

There are two separate costs, and they have different fixes.

**Cost 1 — the server reloading from scratch.** Controlled by `idle_timeout`.
When it expires the process exits and the next render pays a full rebuild:

```json
{ "idle_timeout": 0 }
```

`0` means never exit. The pipeline stays built in system RAM, so the next render
skips the load entirely. Measured load cost avoided: **~140 s**. This is safe with
any `offload` mode and is the first thing to change on a slow-bus machine.

**Cost 2 — the per-render transfer.** Controlled by `offload`. `"none"` keeps
everything resident on the card, which removes the transfer completely… if it
fits. Measured on a 24 GB card with Q4_K_M, 1024x512, 30 steps:

| `offload` | load | render | idle VRAM |
|---|---|---|---|
| `model` | 44 s | 153 s | ~0 GB |
| `none` | 130 s | **87 s** | **21.4 GB** |

**But `offload: "none"` did not prove reliable at 21.4 GB of 24.** The same
configuration that rendered 1024x512 in 87 s later took **336 s per step** for the
same size on a fresh server, and 1024x1024 took **286 s per step** — both are the
driver silently spilling to host memory over the same slow bus. There is no error;
the render just crawls, and with ~2.5 GB of headroom the outcome flips on
fragmentation you cannot see.

`--attention-slicing` does not rescue it (tested), and this pipeline has no
`enable_vae_tiling` / `enable_vae_slicing` to fall back on — the server warns and
carries on if you ask for them.

**Recommendation for a slow-bus / eGPU setup:**

1. Set `idle_timeout: 0`. Unambiguous win, no downside beyond a resident process.
2. Keep `offload: "model"`. Slower per render, but predictable at every size.
3. Only try `offload: "none"` if you pin sizes to 1024x512 or smaller **and** you
   benchmark it more than once, on a freshly started server.

#### Per-component placement (`resident_components`)

Components are not used equally. With Q4_K_M the transformer is ~4.5 GB and runs
for every denoising step; the **unquantized text encoder is ~15 GB** and runs once
per render, then sits idle. So it is worth being able to say which components stay
on the card and which travel:

```json
{ "offload": "model", "resident_components": ["transformer", "vae"] }
```

Named components are pinned to the GPU; everything else keeps the usual offload
hooks. `/health` reports `resident` (what you asked for) and `resident_applied`
(what the pipeline actually had) — names differ between pipeline classes, and an
unknown one is dropped with a warning rather than failing the start.

This works: with `["transformer", "vae"]` the card holds a steady 5.03 GB between
renders instead of 0.04 GB.

**On a 24 GB card it still does not pay off.** Measured, 1024x512, 30 steps,
Q4_K_M, same machine:

| configuration | idle VRAM | render |
|---|---|---|
| `offload: "model"`, nothing pinned | 0.04 GB | **153 s** |
| `resident: ["transformer", "vae"]` | 5.03 GB | 209 s / 199 s |
| `resident: ["text_encoder"]` | 16.38 GB | 202 s / 206 s |
| `offload: "none"` (everything) | 21.4 GB | 87 s, then 336 s/step — unstable |

Every pinned configuration is *slower*, and the reason is the same one that makes
`offload: "none"` unstable: whatever stays resident is headroom the activations no
longer have, and the spill goes over the same slow bus. Pinning the transformer
also collapses `model_cpu_offload_seq` to a single entry, and diffusers evicts a
module when the *next* one in the chain runs — with nothing after it, the 15 GB
text encoder stays on the card for the whole denoise.

So on a 24 GB card the plain `offload: "model"` eviction, which keeps peak VRAM
lowest, wins. `resident_components` is worth reaching for when the card has real
headroom — roughly, when the pipeline's resident footprint is under about half the
card.

**The remaining lever is the text encoder's size**, not its placement. Quantizing
it from bf16 (~15 GB) to 4-bit (~4 GB) would put the whole pipeline near 10 GB and
leave ~14 GB for activations, at which point `offload: "none"` becomes comfortable
and the bus stops mattering. That needs a quantization backend the server does not
wire up yet (`bitsandbytes` / `optimum-quanto`), and support for those on
ROCm + Windows is patchy.

#### Activation-memory options

Three opt-in flags, all default `false`, applied after placement:

```json
{ "vae_tiling": true, "vae_slicing": true, "attention_slicing": true }
```

They lower the peak activation memory rather than the weight footprint, so they
matter with `offload: "none"` where the weights already hold most of the card.
Each is best-effort — diffusers' Qwen-Image pipelines have gained and lost these
helpers between releases, so a missing one logs a warning and the server starts
anyway. On the pipeline tested, only `attention_slicing` was available.

---

## Timeouts

Three separate limits can cut a render short. All three must clear the render
time, or you get a confusing cancellation minutes in:

| Limit | Where | Note |
|---|---|---|
| `request_timeout` | `~/.aar/qwen-image.json` | HTTP wait for the sidecar's reply |
| `tools.command_timeout` | `~/.aar/config.json` | The executor's cap on **every** tool. The image tools declare their own `timeout_s` so they are not clipped by it |
| `subagents.agents.*.timeout` | `~/.aar/config.json` | Wall-clock for the whole sub-agent run — must cover the render *plus* the child model's own thinking |

---

## See also

- [aar-ext-qwen-image README](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md) — tools, sizes, GGUF quantization, AMD/ROCm setup,
  [Keeping the model in VRAM](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md#keeping-the-model-in-vram)
- [Configuration — Sub-agents](configuration.md#sub-agents-spawn_agent) — the full `subagents` key reference
- [Tools — spawn_agent](tools.md#spawn_agent) — parameters and safety model
