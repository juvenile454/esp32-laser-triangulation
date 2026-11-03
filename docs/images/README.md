# Documentation images

| Image | What it shows | Source |
|---|---|---|
| `measurement-principle-*.png` | Schematic of the laser/camera triangulation layout | hand-drawn SVG in `source/render_images.py` |
| `height-noise-*.png` | Height noise against working distance | `tools/triangulation_budget.py` model, f ≈ 375 px, 0.22 px noise |
| `extraction-fixtures-*.png` | Extractor output on three synthetic fixtures | `tools/generate_synthetic_dataset.py` + `tools/analyze_laser_pair.py` |
| `social-preview.png` | 1280 × 640 repository preview card | upload under *Settings → General → Social preview* |

Every image comes in a `-light` and a `-dark` variant, selected in the README
with `<picture>` and `prefers-color-scheme`.

## Regenerating

The images are rendered with the Playwright build that ships with Node
(`npm install playwright` if your machine has none):

```bash
python3 tools/generate_synthetic_dataset.py /tmp/laser-images/syn
for n in 011_fillet_corner 005_ghost 004_occlusion_gap; do
  python3 tools/analyze_laser_pair.py \
    --on  /tmp/laser-images/syn/synthetic_$n/laser_on.png \
    --off /tmp/laser-images/syn/synthetic_$n/laser_off.png \
    --output-dir /tmp/laser-images/an_$n
done
python3 docs/images/source/render_images.py . /tmp/laser-images
node docs/images/source/shoot.js /tmp/laser-images/manifest.json
```

The PNGs were then palette-quantised with Pillow to keep the repository small.
