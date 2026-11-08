# Measurement geometry

The camera fixes the pixel noise floor. Geometry fixes everything else. This
document records what the current layout can reach, and what has to change
before a weld seam can be measured to a tenth of a millimetre.

Compute any of the numbers below with:

```bash
.venv/bin/python tools/triangulation_budget.py --layout tilted-laser \
  --baseline-mm 70 --working-distance-mm 400 \
  --focal-length-mm 2.1 --pixel-pitch-um 5.6 \
  --target-accuracy-mm 0.1 --sweep
```

The default layout is `tilted-camera`; the example above reproduces the
provisional carrier, which is `tilted-laser`.

## Two layouts

Which component is perpendicular to the work plane changes both the model and
the behaviour. The tool implements both; pass `--layout`.

### tilted-camera — the standard arrangement, and what to build

The laser fires **perpendicular to the work plane**; the camera sits at the
baseline and is aimed back at the stripe. Because the beam is perpendicular, the
illuminated point does not wander across the workpiece when the height changes —
it moves straight up and down the beam. Only the camera's view of it shifts.
That keeps "where am I measuring" independent of "how high is it", which is
exactly what seam tracking needs.

```text
alpha = atan(B / Z)                     triangulation angle
d     = sqrt(B^2 + Z^2)                 camera to stripe
dU/dh = f_px * sin(alpha) / d
      = f_px * sin(2*alpha) / (2 * Z)   pixels per millimetre of height
```

The second form carries the whole design rule. Sensitivity is
`f_px / (2*Z)` multiplied by `sin(2*alpha)`, so:

- it **peaks at alpha = 45 degrees**, where the baseline equals the working
  distance;
- `sin(2*alpha)` is a flat-topped curve. 30° keeps 87 % of the optimum, 35°
  keeps 94 %, 40° keeps 98 %, and 55° is back down to 98 %. Anywhere between
  30° and 55° is effectively as good as the optimum;
- everything else is **1/Z**. Distance is the only lever that really matters.

Halving the standoff halves the height noise. Changing the angle within the
usable band moves it by a few per cent. Build the fixture as close to the work
as focus and clearance allow, then pick a convenient angle.

### tilted-laser — what the provisional carrier does

Camera perpendicular, laser aimed across to meet the camera axis:

```text
dU/dh = f_px * B / Z^2
```

Sensitivity falls with the square of the distance here, and the stripe walks
across the workpiece as the height changes, so a height error also displaces the
measured location. Usable for bring-up, not what to build.

## Measured on 2026-08-13

The stripe centre repeatability was measured over 20 consecutive frames of a
completely static scene. Anything that varies is measurement noise.

| Quantity | Value | Note |
|---|---:|---|
| Sensor noise floor (luma centroid) | 0.22 px | The real floor. Very good. |
| Red-excess centroid, unsplit columns | 0.20 px | Agrees with the floor. |
| Red-excess centroid, saturated core | 2.7–3.5 px | Flank flipping, not noise. |
| Frame-to-frame drift | 0.003 px | None. Lock and mounting are stable. |
| Stripe width perpendicular to the line | ≈ 7 px | Partly defocus. |

The 0.22 px figure is what the accuracy budget assumes. It is only achievable
once the core stops clipping; see the saturation note below.

## Current layout

Camera and laser both point straight down, roughly 70 mm apart, about 400 mm
above the work plane. The laser is tilted so its plane crosses the camera axis,
which is why the stripe images through the frame centre. This is the
`tilted-laser` layout.

```text
baseline B                    70.0 mm
working distance Z           400.0 mm
focal length (estimated)     375.0 px
triangulation angle            9.93 deg
millimetres per pixel        1.0667 mm/px
field of view (width)         682.7 mm
height sensitivity           0.1641 px/mm
height noise (1 sigma)       1.3410 mm
```

**A shallow 9.9 degree angle is the binding constraint.** One millimetre of
height moves the stripe by a sixth of a pixel, so even a perfect centroid
yields 1.34 mm of height noise — roughly thirteen times worse than a fillet
weld needs.

## Building the rail fixture

A rail was built that allows up to 400 mm of camera-to-laser separation. Most of
that range is not useful, and the reason follows directly from the model above.

### Aim the rail along the seam, not across it

Triangulation needs the camera offset **perpendicular to the laser plane**. The
laser plane must be perpendicular to the seam so the stripe crosses it, which
leaves exactly one direction for the baseline: **along the seam**.

This also settles occlusion for a fillet weld. With the camera offset purely
along the seam, both plates extend uniformly towards and away from it, so
neither plate shadows the stripe on the other. Offsetting the camera sideways
instead puts the upright plate directly in the way.

In the image this maps to: stripe vertical (image y = across the seam), height
variation shifting it horizontally (image x = disparity), travel direction not
visible in a single frame.

### Illuminate a fillet along its bisector, not straight down

"Perpendicular to the work plane" is unambiguous for flat stock and misleading
for a fillet, which has two faces at 90 degrees. A vertical laser plane hits the
horizontal plate square and grazes the upright one — the stripe on the upright
plate becomes faint, stretched and hypersensitive to small tilts.

The fix is to illuminate along the **corner bisector**, 45 degrees to each face.
On the bench the easy way is to rotate the workpiece rather than the fixture:
stand the tacked assembly on its corner so it forms a V opening upwards. A
vertical laser then bisects it exactly, both faces are lit at 45 degrees, and
the rail stays level.

On a robot the same condition is met by orienting the sensor head so its axis
bisects the corner, which is roughly where the torch points anyway.

### Recommended starting point

All rows use the tilted-camera layout.

| Layout | Angle | Efficiency | mm/px | Field | Height noise |
|---|---:|---:|---:|---:|---:|
| B = 105 mm, Z = 150 mm | 35.0° | 0.94 | 0.49 | 313 mm | 0.187 mm |
| B = 84 mm, Z = 120 mm | 35.0° | 0.94 | 0.39 | 250 mm | 0.150 mm |
| B = 63 mm, Z = 90 mm | 35.0° | 0.94 | 0.29 | 188 mm | 0.112 mm |

Only about a quarter of the rail's travel is needed. Using its full 400 mm at a
matching 400 mm standoff would give 0.47 mm — worse than half the separation at
half the distance.

Depth of field is not a constraint at this scale. With a 2.1 mm lens at roughly
f/2.4 and a one-pixel circle of confusion, the hyperfocal distance is about
330 mm, so focusing at 183 mm keeps everything from 118 mm to 411 mm sharp. The
tilt of the camera is comfortably inside that, and no Scheimpflug adjustment is
needed.

The remaining limit is the wide lens: the field stays two to three times the
working distance, so most pixels fall outside the seam. That costs lateral
resolution, not height resolution, and the two-leg corner fit recovers much of
it by using every inlier. Fit a longer M12 lens only if lateral precision turns
out to be the binding constraint after the first real measurements.

## Where to move

The two layouts give different numbers for the same B and Z, so every table in
this section names its layout. All values are height noise in millimetres, one
sigma, at the 0.22 px floor and the estimated f ≈ 375 px.

**Keeping the provisional carrier (`--layout tilted-laser`):**

```text
  Z \ B          35        70       105       140       210
    120       0.241     0.121     0.080     0.060     0.040
    200       0.670     0.335     0.223     0.168     0.112
    300       1.509     0.754     0.503     0.377     0.251
    400       2.682     1.341     0.894     0.670     0.447
    600       6.034     3.017     2.011     1.509     1.006
  (columns are baseline in mm, rows are working distance in mm)
```

**The same positions in the recommended layout (`--layout tilted-camera`):**

| B, Z | Angle | mm/px | Field | tilted-laser | tilted-camera |
|---|---:|---:|---:|---:|---:|
| B = 70 mm, Z = 120 mm | 30.3° | 0.37 | 237 mm | 0.121 mm | 0.162 mm |
| B = 100 mm, Z = 150 mm | 33.7° | 0.48 | 308 mm | 0.132 mm | 0.191 mm |
| B = 120 mm, Z = 200 mm | 31.0° | 0.62 | 398 mm | 0.196 mm | 0.266 mm |

(mm/px and field are given for the tilted-camera layout, which views the plane
obliquely and is therefore slightly coarser.)

The tilted-laser figures look better, but they come from the simpler
`f · B / Z²` model, and they buy that sensitivity by letting the stripe walk
across the workpiece as the height changes. A height error then also moves the
measured location, which is exactly what seam tracking cannot tolerate. That is
why the rail fixture uses the tilted-camera layout, and why its recommended
point (B = 84 mm, Z = 120 mm, 0.150 mm) is quoted in that model.

Lowering the existing carrier to about 120 mm is still the cheapest bring-up
step: about 0.12 mm in its own layout. Aim for a triangulation angle of
**30° to 45°**: below 20° height resolution collapses, above 60° the near leg
of a fillet weld starts to occlude the far one.

The 400 mm standoff cannot reach 0.10 mm with a sensible machine. The
tilted-laser layout would need a 939 mm baseline. The tilted-camera layout
bottoms out at about 0.47 mm at 45°, however long the baseline.

## Focal length: measure it, do not trust the estimate

The 375 px above is derived from an assumed 2.1 mm lens and a 5.6 µm effective
pixel pitch at VGA. It has never been measured, and every millimetre figure in
this document scales with it.

The honest way to pin it down needs no lens data at all. Put a block of known
thickness under the target, capture before and after, and measure how far the
stripe moved:

```bash
.venv/bin/python tools/triangulation_budget.py --layout tilted-laser \
  --baseline-mm 70 --working-distance-mm 400 \
  --from-measured-shift 20 3.3
```

That derives `f_px` from the geometry that actually exists, including whatever
the lens really does. Pass the `--layout` that matches the hardware: the two
layouts convert the same shift into different focal lengths (up to about a third
apart at a 35° angle), and the tool inverts the exact forward model of each. Do this as the first measurement on the rigid fixture,
before any calibration series.

## Stripe orientation

The extractor scans image rows and searches along x. That encodes an
assumption: **disparity runs along the image x-axis, so the laser line must
image vertically.**

The assumption is confirmed by data. In the 2026-08-08 capture the stripe
crosses a box edge and jumps horizontally, which identifies x as the disparity
axis.

Orientation history, same hardware, same algorithm:

| Date | Stripe angle to x-axis | Row coverage | Mean width | Contract |
|---|---:|---:|---:|---|
| 2026-08-13, morning | +29.3° | 64.8 % | 14.5 px | v2 loosened to width 64, threshold 30 |
| 2026-08-13, evening | +89.5° | 98.3 % | 8.8 px | v1 frozen, width 16, threshold 20 |

A shallow crossing angle inflates the apparent stripe width by `1/sin(angle)`;
at 29.3° that is a factor of 2.04. Every parameter that was loosened during the
morning session was compensating for the projection, not for a property of the
algorithm. Rotating the laser restored the frozen contract and improved
coverage at the same time.

## Saturation is still open

With the orientation fixed, clipping is the only remaining source of large
error. The core reads `R = 255` across the whole line and `G = 255` in its
middle, so `R - max(G, B)` — the extraction score — collapses to zero exactly
where the signal is strongest. Each cross-section splits into two red flanks
and the selected centre flips between them.

Continuity tracking was added in contract v3 and does **not** fix this:

| Configuration | Median centroid spread |
|---|---:|
| No tracking | 3.47 px |
| Tracking, 4 px window | 3.74 px |
| Tracking, 3 px window | 2.70 px |

Tracking works against candidates that are spatially separated from the true
stripe — specular blobs, ghost reflections — and the synthetic fixtures prove
it there. Two flanks of one clipped core sit only a few pixels apart, so no
window separates them cleanly. The fix is optical, not algorithmic:

1. lower `agc_gain` towards 0 and reduce `aec_value` until the core peaks near
   200 of 255 (firmware commands `Gnn` and `Ennnn`);
2. fit a 650 nm bandpass filter so the exposure can be cut hard without losing
   the stripe to a darkened background;
3. focus the laser line itself — at roughly 7 px it is wider than it needs to
   be, which spreads the same energy over more pixels.

Only then does the 0.22 px floor become the working accuracy.
