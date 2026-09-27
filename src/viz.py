"""Shared plotting style: one palette, one set of mark rules, both themes.

Colour choices follow the project's data-viz method, so they are assigned by the job
the colour does rather than picked by eye:

  * magnitude (choropleths, heatmaps) -> ONE hue, light to dark. Never a rainbow.
  * identity (series)                 -> the categorical hues in fixed order, never
                                         cycled and never reassigned by rank.

The categorical order below clears the adjacent-pair CVD and normal-vision floors,
but only the first three slots clear the all-pairs floors. Forms that put every
series against every other (scatter, choropleth categories) therefore cap at three
and facet beyond that - which is why the pilot trends are small multiples rather
than eight lines on one axis.
"""

from __future__ import annotations

import matplotlib as mpl
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

# Categorical hues, fixed order (light mode).
SERIES = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

# Sequential blue ramp, 100 -> 700. The lightest step means "near zero".
SEQUENTIAL_BLUE = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
    "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#82817c"
GRID = "#e6e5e1"

RAIN_CMAP = LinearSegmentedColormap.from_list("rain_blue", SEQUENTIAL_BLUE)

# Diverging, for quantities with a meaningful zero (Brier Skill Score: positive beats
# climatology, negative loses to it). Two hues with a NEUTRAL GREY midpoint, so zero
# reads as "nothing" rather than as a colour. Built only from documented palette
# values: the critical red pole, the neutral midpoint, and the blue pole.
DIVERGING_RED = "#d03b3b"
DIVERGING_NEUTRAL = "#f0efec"
DIVERGING_BLUE = "#2a78d6"
SKILL_CMAP = LinearSegmentedColormap.from_list(
    "skill", ["#7a1a1a", DIVERGING_RED, "#eab3b1", DIVERGING_NEUTRAL,
              "#9ec5f4", DIVERGING_BLUE, "#0d366b"],
)
# Units with no observation are a distinct absence, not a low value.
NO_DATA = "#eceae4"


def apply_style() -> None:
    """Recessive chrome, thin marks, text in ink tokens rather than series colour."""
    mpl.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "savefig.dpi": 150,
        "figure.dpi": 110,
        "font.size": 9,
        "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
        "text.color": TEXT_PRIMARY,
        "axes.labelcolor": TEXT_SECONDARY,
        "axes.edgecolor": GRID,
        "axes.linewidth": 0.8,
        "axes.titlesize": 10,
        "axes.titleweight": "600",
        "axes.titlecolor": TEXT_PRIMARY,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": TEXT_SECONDARY,
        "ytick.color": TEXT_SECONDARY,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "xtick.major.width": 0.8,
        "ytick.major.width": 0.8,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "legend.frameon": False,
        "legend.fontsize": 8,
        "lines.linewidth": 2.0,       # 2px lines
        "lines.markersize": 4,
        "lines.solid_capstyle": "round",
    })


def recessive_grid(ax, axis: str = "y") -> None:
    ax.grid(True, axis=axis, alpha=0.9, zorder=0)
    ax.set_axisbelow(True)


def nice_limit(values: np.ndarray, pad: float = 0.08) -> tuple[float, float]:
    finite = np.asarray(values)[np.isfinite(values)]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = float(finite.min()), float(finite.max())
    span = max(hi - lo, 1e-9)
    return lo - pad * span, hi + pad * span
