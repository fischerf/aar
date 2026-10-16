# Generating and editing images with the qwen-image sidecar

How to drive the [qwen-image extension](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md)
from aar: one-shot `aar run` jobs, interactive sessions, editing, and — the part that
needs the most care — transparent output.

```
aar ──(chat + tool calls)──► chat model (Ollama, Anthropic, …)
 └──(image_generate / image_edit)──► qwen-image sidecar (own venv, GPU) ──► PNG on disk
```

The chat model only decides *what* to call. The pixels come from Qwen-Image-2.1 running in
a separate server process that aar starts on demand. Tools return a **file path**, never
the image itself.

This page is the task guide. Installation, quantization, GPU selection and every config
key live in the extension README; the sprite-sheet recipe is in
[sprite-sheet-workflow.md](sprite-sheet-workflow.md).

---

## 1. Before the first render

```bash
aar extensions list                         # must list "qwen_image (entrypoint)"
curl -s 127.0.0.1:11434/api/version         # your chat model's Ollama is up
curl -s 127.0.0.1:8770/health               # sidecar — "connection refused" is fine,
                                            # the first tool call starts it
```

Two sidecar configurations are set up — the same Qwen-Image-2.1 model at two precisions
([section 6](#6-two-configurations-bf16-and-sdnq-int4) has the numbers and how to switch):

| file | checkpoint | port | use it for |
|---|---|---|---|
| `~/.aar/qwen-image.json` (**default**) | bf16 `Qwen/Qwen-Image-2.1` | 8770 | quality: transparent assets, edits, final renders |
| `~/.aar/qwen-image-sdnq.json` | SDNQ int4 | 8771 | speed: drafts, photos, large canvases |

The two keys that decide where files land and how big they are by default (same in both):

| key | default | meaning |
|---|---|---|
| `out_dir` | `""` | aar's **current working directory**. Run aar where you want the PNGs |
| `width` / `height` / `steps` | `1024` / `1024` / `30` | used whenever the model omits them |

If a tool call fails with *All connection attempts failed*, it is the **chat model's**
Ollama that is down, not the sidecar.

---

## 2. Pick a chat model that doesn't share the renderer's GPU

This decides whether a job takes two minutes or fifteen.

The sidecar and a large chat model cannot both sit on one 24 GB card. `evict_ollama`
unloads the chat model *before* each render, but nothing hands the card back *after* it:
the sidecar stays resident until `idle_timeout`, and the chat model has to reload for its
next turn. On Windows that reload does not fail when memory runs out — the overflow spills
into shared system memory and every token crawls.

Measured on an RX 7900 XTX, same job (generate a 768px sticker, then edit it, SDNQ int4):

| chat model | where | job | renders |
|---|---|---|---|
| `qwen3.8` (27B, `num_ctx` 65536, ~21 GB on the card) | same card as the sidecar | **899 s** | 91 + 106 s |
| `gemma4` (e4b) | the other GPU (RTX 3060) | **86 s** | 30 + 39 s |

What the 899 s was made of: a 102 s cold reload of `qwen3.8` after every render, into a card
the sidecar was still holding — Windows spilled the overflow (23.8 GB dedicated + 14.7 GB
shared) and every token crawled. Two things that look like fixes and aren't:

- **Switching SDNQ to `offload: "model"`** — an idle server still holds ~3.6 GB (the HIP
  runtime and its kernels, not tensors — only the process exiting frees it), and its renders
  got ~3x slower. (The bf16 default uses `offload: "model"` because it has to: its 31 GB of
  weights don't fit on the card.)
- **A shorter `idle_timeout`** — the next chat turn starts the instant the render returns,
  long before any timeout fires.

So for image work, run the job on a small model on the other card — a profile name from
`providers` in `config.json` works directly:

```bash
aar run --provider gemma4e4b "…"
```

The `illustrator` / `retoucher` sub-agents (section 8) pin their own `provider` and don't
need this.

> **Check the small model really is on the other card.** On Windows the Ollama tray app
> (`Startup\Ollama.lnk`) starts a third server on `[::]:11434` with your global environment.
> `localhost` resolves to `::1` first, so requests for "the NVIDIA server" land on it, and it
> puts the model on the biggest GPU — the renderer's. `curl -s 127.0.0.1:11435/api/ps` should
> not list your small model; per-process VRAM (`Get-Counter '\GPU Process Memory(*)\Dedicated
> Usage'`) shows which card a `llama-server` sits on. Remove the startup link, and prefer
> `127.0.0.1` over `localhost` in `base_url` (it also skips a ~2.8 s IPv6 attempt per request).

---

## 3. Generate

### One-shot: `aar run`

```bash
cd ~/projects/game
aar run "Use image_generate: width=1024, height=1024, steps=30, seed=42, out=cat.png,
prompt: photorealistic portrait of a long-haired tabby cat, natural light, sharp fur detail"
```

Name the tool and spell out every parameter as `key=value`. Small local models drop
optional arguments stated in prose ("make it wide, about 30 steps") and you get the
config-default size under a timestamp name.

| argument | notes |
|---|---|
| `prompt` | describe the picture, not the request |
| `width`, `height` | **pass both**. Pass only one and the other falls back to the config default, silently changing the aspect ratio |
| `steps` | 30 is a good default; 8–12 for quick drafts |
| `seed` | fixes the result — the only way to make two renders comparable |
| `out` | a bare file name; always saved in `out_dir`, forced to `.png`, never overwrites (`cat.png` → `cat-1.png`) |
| `negative_prompt` | what to avoid |
| `transparent` | RGBA cutout — see [section 5](#5-transparency) |

Sizes the model was trained on: 1:1 2048x2048, 4:3 2400x1792, 3:2 2528x1696, 16:9
2752x1536 (and their portrait flips). Anything up to `max_pixels` (2400x1792) works;
smaller renders are much faster.

**Approvals.** Both image tools write files, so aar asks before each call:

```
Allow? [y]es / [n]o / [a]lways:
```

In a terminal, answer `a` to allow that tool for the rest of the run. When stdin is not a
terminal (scripts, CI, piping), the prompt reads end-of-input and aborts the call. Either
pass `--no-require-approval` — which drops approval for **every** write and execute tool,
`bash` included — or feed answers in with `yes y | aar run …`.

### Interactive: `aar chat` / `aar tui`

Same tools, plus a slash-command that bypasses the model entirely — every parameter is a
flag, nothing is inferred from prose:

```
/qwenimage generate --size 1024x1024 --steps 30 --seed 42 --out cat.png
  photorealistic portrait of a long-haired tabby cat, natural light
```

| flag | |
|---|---|
| `--size WxH` (or `--width` / `--height`) | output size |
| `--steps N`, `--seed N`, `--out name.png` | as for the tool |
| `--negative "..."` | negative prompt |
| `--transparent` | RGBA cutout |
| `--image <path>` | extra reference for `edit`, repeatable |

`/qwenimage start`, `stop`, `status` and `devices` manage the server. Slash-commands only
exist in `chat` and `tui` — `aar run` sends a leading `/` to the model as text.

Use the slash-command when you are iterating on one picture; use the tools when the image
is a step in a larger task (write the code that uses it, edit it next, …).

---

## 4. Edit

`image_edit` takes up to ten reference images and a prompt describing **the change**, and
writes a **new** file. The original is never modified.

```bash
aar run "Use image_edit on cat.png: give the cat a small red bow tie, keep everything
else unchanged. width=1024, height=1024, steps=30, seed=42, out=cat-bowtie.png"
```

Or, interactively:

```
/qwenimage edit cat.png --size 1024x1024 --seed 42 --out cat-bowtie.png
  give the cat a small red bow tie, keep everything else unchanged
```

References may be given as a full or `~` path, as `@path` (aar's attachment syntax — the
`@` is stripped), or as a bare name, which is looked up in the working directory and then
in `out_dir`. Model-issued paths are policy-checked like `read_file`: a picture outside
`safety.allowed_paths` is refused before the sidecar sees it.

Rules that avoid most failed edits:

- **Match the aspect ratio.** An edit renders at `width` x `height` — the config default if
  omitted — not at the source's size. Editing an 800x480 photo without passing its ratio
  squares it.
- **Describe only the change.** "Add a red bow tie, keep everything else unchanged", not a
  full description of the scene.
- **Keep the seed** from the original render when comparing variants.
- **Generate and edit can go in one message.** When a model emits both calls in the same
  step, aar runs them in the order given (any batch containing a write runs sequentially),
  so the edit finds the file. To iterate on a result later, continue the session:

  ```bash
  aar run "…generate cat.png…"               # prints "Session: 87a696a4e3934c1d"
  aar run -s 87a696a4e3934c1d "Use image_edit on cat.png: …"
  ```

  Sessions are stored under `.agent/sessions` **in the working directory**, so `-s` only
  finds them from the same directory.
- **Edits depend on the checkpoint.** "Add a small red wizard hat" produced a clear hat on
  bf16 and only a red tip on SDNQ int4 — edit on the default config, see
  [section 6](#6-two-configurations-bf16-and-sdnq-int4). Naming size and position ("a tall
  red wizard hat covering the top of the head") helps.

If the result looks like an unrelated new image, the model called `image_generate`
instead: the sidecar log (`log_file` in the config — `server-bf16.log` or
`server-sdnq.log` under `~/.aar/qwen-image/`) shows `0 refs` for that render rather than
`1 refs`.

---

## 5. Transparency

Qwen-Image-2.1 produces real RGBA output, but there is no pipeline switch for it — the
request lives in the prompt. `transparent=true` (or `--transparent`) wraps your prompt in
the model card's wording:

```
This is an RGBA image with transparency. <your prompt>. The image has alpha channel and
the background is transparent.
```

A prompt that already contains that sentence is not wrapped twice.

### Prompting for a clean cutout

| do | why |
|---|---|
| pass `transparent=true` on **every** call, edits included | without it the output is opaque |
| describe a single subject: "sticker", "game sprite", "icon" | an isolated subject separates cleanly |
| ask for `bold dark outlines` | keeps edges readable over any background, hides fringe |
| **don't** mention a background colour | it competes with the alpha channel |
| for sprite rows: `one horizontal row of N evenly spaced frames` | otherwise frames drift into an arc |

### The checkpoint matters

Same prompt and seed (768x768 sticker), measured:

| config | alpha == 0 | alpha ≤ 8 | looks like |
|---|---|---|---|
| bf16 (default) | 12.3% | 53.4% | clean cutout, thin sticker outline |
| SDNQ int4 | 7.8% | 41.0% | wide blue-purple halo around the subject |

Both leave most of the "empty" background at alpha 1–8 rather than 0 — invisible on a
checkerboard, but a strict count says it isn't transparent, and an engine that treats low
alpha as visible (or filters bilinearly) shows a faint haze. Int4 is worse: the
under-colour is purple (RGB ≈ 167, 55, 209) and bleeds into a visible halo.

**Render transparent assets on the default (bf16) config.** If you are in an SDNQ session,
stop that sidecar and start a new aar without `AAR_QWEN_IMAGE_CONFIG` —
[section 6](#6-two-configurations-bf16-and-sdnq-int4).

Opaque renders come back as RGB. The model decodes to RGBA for everything, and an
"opaque" photo's alpha is not a clean 255 (int4: 214–255 over 22% of the pixels; bf16:
250–255 over 4%) — so the
server drops the alpha channel unless the call asked for `transparent`.

### Check what you got

```python
from PIL import Image

im = Image.open("dragon.png")
h = im.convert("RGBA").getchannel("A").histogram()
n = sum(h)
print(im.mode, im.size)
print("alpha == 0: %.1f%%" % (100 * h[0] / n))
print("alpha <= 8: %.1f%%" % (100 * sum(h[:9]) / n))
```

`mode` must be `RGBA`. If `alpha == 0` and `alpha <= 8` are both near zero, the render came
back opaque — re-render, or use the chroma-key fallback below.

### Clean it up

Snap the near-transparent haze to zero (on the int4 sticker: 7.8% → 41.3% fully
transparent):

```python
from PIL import Image

im = Image.open("dragon.png").convert("RGBA")
im.putalpha(im.getchannel("A").point(lambda v: 0 if v <= 16 else v))
im.save("dragon-clean.png")
```

For pixel art that needs a hard 1-bit mask, threshold at the midpoint and downscale with
`NEAREST`:

```python
im.putalpha(im.getchannel("A").point(lambda v: 255 if v > 128 else 0))
im.resize((im.width // 4, im.height // 4), Image.NEAREST).save("sprite-hard.png")
```

In a canvas game, set `ctx.imageSmoothingEnabled = false` so the browser doesn't blend the
under-colour into the edges.

### Editing a transparent image

The sidecar flattens every reference to RGB before the model sees it (`convert("RGB")`), so
**the alpha channel of the input is discarded**. What the model edits is the subject on
top of whatever colour hides under the transparent pixels — purple for int4 output.

Consequences:

- Pass `transparent=true` on the edit, or the result is an opaque picture on that
  under-colour. With it, the int4 test edit came back RGBA with the same alpha profile as
  the original (6.9% zero, 39.7% ≤ 8).
- Clean up alpha **after** the last edit, not before — the edit does not see your cleanup.
- Transparency is re-generated, not preserved: the new outline can differ slightly from the
  original's. Compare them over a checkerboard before replacing an asset.

### Fallback: chroma key

If a render keeps coming back opaque, render on a flat key colour and cut it out yourself:

```
/qwenimage generate --size 768x768 --seed 7 --out dragon-key.png
  cute cartoon dragon sticker, bold dark outlines, on a flat solid magenta background
```

```python
from PIL import Image

im = Image.open("dragon-key.png").convert("RGBA")
px = im.load()
for y in range(im.height):
    for x in range(im.width):
        r, g, b, _ = px[x, y]
        if r > 200 and b > 200 and g < 80:        # magenta
            px[x, y] = (r, g, b, 0)
im.save("dragon-keyed.png")
```

Pick a key colour that does not appear in the subject.

---

## 6. Two configurations: bf16 and SDNQ int4

Both files run the same Qwen-Image-2.1 model; they differ in precision and in how the
weights sit on the GPU:

| | `qwen-image.json` — **default** | `qwen-image-sdnq.json` |
|---|---|---|
| `model` | `Qwen/Qwen-Image-2.1` (bf16) | `OzzyGT/Qwen_Image_2_1_sdnq_dynamic_4bit` (int4) |
| `offload` | `model` — weights in RAM, staged onto the GPU per render | `none` — whole pipeline resident on the GPU |
| `url` / `log_file` | `:8770` / `server-bf16.log` | `:8771` / `server-sdnq.log` |
| shared by both | `evict_ollama` (unload the big chat model before a render), `idle_timeout: 300`, `request_timeout: 1800`, `out_dir: ""` | |

### Using the default

Nothing to set — every `aar run`, `chat`, `tui` and `acp` process uses `~/.aar/qwen-image.json`.

### Using SDNQ

Point `AAR_QWEN_IMAGE_CONFIG` at the other file, for one command or for a whole shell
session. Use a full path — the extension does not expand `~` itself.

```bash
# bash / Git Bash — one command
AAR_QWEN_IMAGE_CONFIG=~/.aar/qwen-image-sdnq.json aar run --provider gemma4e4b "…"
```

```powershell
# PowerShell — this session, until you remove it
$env:AAR_QWEN_IMAGE_CONFIG = "$HOME\.aar\qwen-image-sdnq.json"
aar tui
Remove-Item Env:AAR_QWEN_IMAGE_CONFIG      # back to the default
```

The variable is read by the aar process, so sub-agents (`illustrator`, `retoucher`) and
`/qwenimage` inside that process use the same file.

### Switching between them

Only one sidecar should hold the card: bf16 peaks at 23.7 GB, and an idle SDNQ server keeps
14.7 GB resident. Both stop by themselves after 5 idle minutes; to switch sooner, stop the
running one first:

```bash
curl -s 127.0.0.1:8770/health | head -c 120; echo    # which one is up?
curl -s 127.0.0.1:8771/health | head -c 120; echo
curl -s -X POST 127.0.0.1:8771/shutdown              # stop SDNQ (8770 for bf16)
```

— or `/qwenimage stop` from a `chat` / `tui` started with the matching config.

### Measured

RX 7900 XTX 24 GB, 64 GB RAM + 46 GB page file (109.6 GB commit limit), native ROCm on
Windows; the same renders through `aar run`, chat model on the other GPU:

| | bf16 (default) | SDNQ int4 |
|---|---|---|
| server start | 21–26 s | 70 s |
| first render after a start | **+~90 s** (attention kernels compiled once) | no extra cost |
| 512x512, 8 steps (first render) | 188 s | 11 s |
| 1024x1024, 30 steps | 228 s | **53 s** |
| 768x768, 30 steps | 188–206 s | **29 s** |
| edit, 768x768 | 196 s — first attempt **crashed** | **39 s** |
| peak VRAM | 23.7 GB + 0.4 GB spilled | **17.0 GB** |
| VRAM while idle | **3.7 GB** | 14.7 GB |
| server RAM, peak | 52.2 GB | **13.8 GB** |
| system commit, peak | 109.2 GB (limit 109.6) | **56 GB** |
| weights on disk | 31 GB | **11.4 GB** |
| photo | **sharp** | good, slightly softer |
| transparency | **clean cutout** | purple halo |
| edit follows the prompt | **yes** | weakly |

**bf16 is the quality configuration.** Clean alpha, edits that do what they are told,
sharper photos. What it costs:

- **Time.** About 90 s per render go to text encoding, staging weights across PCIe and VAE
  decode, whatever the step count — fewer steps do not make a quick draft. Draft on SDNQ,
  render the final on bf16 with the same prompt and seed.
- **Memory — the edit crash.** A bf16 edit pushed system commit to 109.21 of 109.6 GB and the
  server died (`ReadError — the server closed the connection mid-render`); the retry
  succeeded. On this machine bf16 edits run at the edge. Raise the page file — the extension
  README's [page-file section](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md#windows-size-your-page-file-before-using-model-offload)
  has the numbers and commands — or close memory-hungry programs before a batch of edits.

**SDNQ int4 is the speed configuration**: 3–7x faster, half the RAM, a third of the disk.
Use it for drafts, photos and large canvases. It is weaker at transparency and at edits, and
it keeps 14.7 GB on the card until `idle_timeout` — the reason a chat model sharing that card
crawls ([section 2](#2-pick-a-chat-model-that-doesnt-share-the-renderers-gpu)).

> A GGUF Q4_K_M transformer (`quant: Q4_K_M`) rendered visually the same as bf16 with 15 GB
> less RAM, but its original files were deleted upstream — the repo now only serves
> different, "uncensored" weights — so it is no longer part of this setup.

---

## 7. Letting the agent see the result

Tool results are strings; the chat model never sees pixels. On a vision-capable provider,
attach the file back:

```bash
aar run -s <session> "@dragon.png — is the background actually clear? If not, use
image_edit with transparent=true to fix it."
```

---

## 8. Sub-agents: illustrator and retoucher

`config/samples/config.json` ships two image profiles for the built-in `spawn_agent` tool:
an `illustrator` that can only call `image_generate` and a `retoucher` that can only call
`image_edit`, each returning just the saved path. They keep image prompts and retries out
of the main agent's context, and each profile can pin a small `provider` on the other GPU
(section 2).

```bash
aar run "Use spawn_agent with the illustrator: width=1024, height=512, transparent=true,
seed=2024, out=player.png, prompt: pixel-art green dinosaur running, side view,
bold dark outlines, no background. Then write index.html that shows player.png."
```

Profile keys, and why `extension_tools` matters: extension README,
[A dedicated image sub-agent](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md#b-a-dedicated-image-sub-agent).

---

## 9. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `All connection attempts failed` before any tool runs | the chat model's Ollama is not running | start it (`ollama serve`, or your own start script) |
| every turn after a render takes minutes | chat model reloading onto the renderer's card | chat model on the other GPU — [section 2](#2-pick-a-chat-model-that-doesnt-share-the-renderers-gpu) |
| the small model is slow, or sits on the renderer's GPU | the Ollama tray app's extra server answered `localhost` | stop it and remove `Startup\Ollama.lnk`; use `127.0.0.1` — [section 2](#2-pick-a-chat-model-that-doesnt-share-the-renderers-gpu) |
| `Aborted.` after `Allow?` | no terminal to answer the approval | `--no-require-approval`, or `yes y \| aar run …` |
| the model "decides" to call the tool but the run just ends | small models sometimes end the turn without the call | re-run; or use `/qwenimage generate` |
| `reference image not found` | wrong directory — bare names resolve against the cwd and `out_dir` | pass a full path |
| edit ignores the change / returns a fresh image | model called `image_generate`, or the change was too subtle, or the SDNQ config | check `0 refs` vs `1 refs` in the sidecar log; name size and position; edit on the default (bf16) config |
| wrong size or aspect ratio | the model passed only `width` or only `height` | always pass both, or use `/qwenimage … --size WxH` |
| transparent output shows a haze or purple fringe | near-zero rather than zero alpha; SDNQ's purple under-colour | alpha threshold ([section 5](#clean-it-up)), or render on the default (bf16) config |
| `ReadError — the server closed the connection mid-render` | bf16 server killed by the Windows commit limit (edits peak ~109 GB) | retry; raise the page file ([section 6](#6-two-configurations-bf16-and-sdnq-int4)) |
| a quick 8-step draft takes minutes | bf16: ~90 s per render are fixed, plus ~90 s once per server start | draft on the SDNQ config |
| renders come from the wrong checkpoint | the other config's sidecar is still running, or `AAR_QWEN_IMAGE_CONFIG` is (not) set | `curl` both `/health` ports; stop one — [section 6](#switching-between-them) |
| `… is not a valid model identifier` | `model` names a repo that doesn't exist | check `model` in the config against Hugging Face |
| server restarts in the middle of a job | a turn outlasted `idle_timeout` | see section 2; or raise `idle_timeout` |

---

## See also

- [aar-ext-qwen-image README](../aar-extensions-registry/packages/aar-ext-qwen-image/README.md)
  — setup, GPU selection, GGUF / SDNQ, every config key, Windows page-file sizing
- [Sprite sheet workflow](sprite-sheet-workflow.md) — transparent sprite sheet → canvas game,
  two-GPU Ollama setup, timeouts
- [Configuration — Sub-agents](configuration.md#sub-agents-spawn_agent)
- [Safety](safety.md) — `allowed_paths` / `denied_paths` for reference images
