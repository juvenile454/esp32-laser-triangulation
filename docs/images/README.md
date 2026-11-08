# Documentation images

| Image | What it shows | Source |
|---|---|---|
| `measurement-principle-*.png` | Schematic of the tilted-camera triangulation layout with angle α | hand-drawn SVG in `source/render_images.py` |
| `height-noise-*.png` | Height noise against working distance, B = 35/84/140 mm | `tools/triangulation_budget.py`, tilted-camera layout, f ≈ 375 px, 0.22 px floor |
| `extraction-fixtures-*.png` | Extractor output on three synthetic fixtures | `tools/generate_synthetic_dataset.py` + `tools/analyze_laser_pair.py` |
| `social-preview.png` | 1280 × 640 repository preview card | upload under *Settings → General → Social preview* |

Every image comes in a `-light` and a `-dark` variant, selected in the README
with `<picture>` and `prefers-color-scheme`.

## Regenerating

The images are rendered from HTML/SVG with Playwright's Chromium. The
`height-noise` chart is computed from `tools/triangulation_budget.py`, and the
fixture panel runs the repository's own extractor, so both stay in sync with the
code. Install Playwright once outside the repository
(`npm install --prefix /tmp/pw playwright`), then:

```bash
python3 docs/images/source/render_images.py prepare /tmp/laser-images
NODE_PATH=/tmp/pw/node_modules node docs/images/source/shoot.js /tmp/laser-images/manifest.json
python3 docs/images/source/render_images.py quantize /tmp/laser-images
```

`prepare` writes the pages and a manifest into the scratch directory, `shoot.js`
renders the PNGs into `docs/images/`, and `quantize` palette-quantises them with
Pillow to keep the repository small. Real captures are never used here.
