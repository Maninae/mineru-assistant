"""A fox drawn as a text-on-path wireframe with body: every stroke is a line of `mineru` CLI text.

Front-facing head with the tail curling in from behind. Geometry is authored once for the RIGHT half
in a local frame with the face axis on x=0 and y pointing down; the left half is a mirrored copy.
Each path is `[start, [(control1, control2, end), ...]]`, a chain of cubic Beziers.

Reading order the design is built for: the amber eyes first, then the head silhouette in full
orange, then the tail and cheek fur in deeper orange, then the fine detail text.

- Text must read upright on both sides, so every run is re-oriented left-to-right (top-to-bottom
  only when it is truly vertical) after mirroring.
- JetBrains Mono is monospace, so how many characters fit on a stroke is its arc length divided by
  the glyph advance; runs are packed with whole words so no corner collides mid-word.
- Fills (head, ears, tail, muzzle, blaze) are polygons sampled from the same Bezier chains, drawn
  under the text. Dot fields sit on the text grid (no jitter) so they belong to the type.
"""
import html

# Right-half head outline, chin to ear tip. Mass sits low: narrow snout, cheek ruff widest below the
# eyes, a notch between ruff and ear, ears on top of a domed forehead.
HEAD_OUTLINE = [(0, 236), [
    ((18, 232), (48, 216), (72, 186)),        # snout
    ((112, 144), (196, 92), (232, 22)),       # cheek taper, widest at y=22
    ((238, -34), (214, -66), (198, -78)),     # the notch between cheek ruff and ear base
    ((220, -150), (212, -232), (182, -270)),  # outer ear, leaning slightly outward
    ((146, -230), (108, -170), (80, -130)),   # inner ear down to the brow
]]
# The brow spans both halves as one run and domes upward.
BROW = [(-80, -130), [((-30, -146), (30, -146), (80, -130))]]
INNER_EAR = [(176, -240), [((170, -196), (146, -160), (110, -136))]]
EAR_FIELD = [(180, -258), (204, -84), (88, -130)]
# The mask line: the edge between the dark upper face and the pale muzzle, cheek to under the eye.
MASK = [(162, 104), [((158, 62), (132, 26), (100, 20)), ((80, 26), (56, 44), (40, 62))]]
# The eye: a slanted almond, heavier upper lid, iris filling it so sclera shows only at the corners.
EYE_UPPER = [(44, -16), [((66, -50), (118, -62), (146, -50))]]
EYE_LOWER = [(44, -16), [((70, 0), (124, -18), (146, -50))]]
IRIS_CENTER = (92, -34)
IRIS_OUTER_RADIUS = 21
IRIS_INNER_RADIUS = 15
PUPIL_SLIT = (7, 11)  # rx, ry of the vertical pupil
HIGHLIGHT = ((84, -44), 3.5)  # centre, radius
# The blaze: a pale wedge down the centre of the face between the eyes.
BLAZE_POLYGON = [(0, -60), (10, 30), (0, 80), (-10, 30)]
# Cheek fur: three sweeps off the ruff.
CHEEK_FUR = [
    [(206, 58), [((232, 66), (248, 82), (258, 104))]],
    [(174, 100), [((204, 110), (222, 134), (228, 166))]],
    [(132, 142), [((158, 156), (174, 178), (176, 206))]],
]
# The nose: a filled diamond at the tip of the snout.
NOSE_POLYGON = [(0, 174), (11, 185), (0, 198), (-11, 185)]
# The tail: roots INSIDE the head at the right cheek (everything inside the head silhouette is clipped
# away, so it emerges from behind the cheek), curls under the chin and up beside the left cheek. One text edge (outer); the inner edge is dotted; cream wedge at the tip.
# The root legs run from inside the head to under the chin and carry dots only, so no word is cut by the head edge.
TAIL_ROOT_OUTER = [(100, 120), [((170, 250), (60, 330), (-40, 334))]]
TAIL_OUTER = [(-40, 334), [((-200, 340), (-340, 300), (-336, 100)), ((-334, 0), (-326, -50), (-300, -96))]]
TAIL_OUTER_TRAIL = [(-300, -96), [((-280, -140), (-250, -162), (-216, -170))]]
TAIL_ROOT_INNER = [(70, 110), [((130, 220), (40, 290), (-40, 292))]]
TAIL_INNER = [(-40, 292), [((-170, 296), (-270, 270), (-272, 100)), ((-274, 10), (-266, -30), (-248, -70))]]
TAIL_INNER_TRAIL = [(-248, -70), [((-240, -110), (-230, -150), (-216, -170))]]

OUTLINE_WORDS = ("mineru setup mineru profile install --apply mineru memory warm-resume "
                 "mineru secrets set mineru cron status mineru memory consolidate").split()
TAIL_WORDS = ("mineru profile export mineru memory backup mineru cron install --dry-run "
              "mineru calendar list mineru gmail search mineru telegram send mineru memory tree").split()
DETAIL_WORDS = ["profile", "memory", "secrets", "cron", "calendar", "gmail", "telegram", "browser",
                "warm-resume", "consolidate", "backup", "tree", "reindex", "validate", "export"]

FONT_PX_BY_ROLE = {
    "outline": 12.5,
    "fur": 10.5,
    "detail": 9.5,
    "tail": 11.5,
    "tail_core": 9,
}
MONO_ADVANCE_EM = 0.6  # JetBrains Mono glyph width as a fraction of font size
JOINT_GAP_CHARS = 2
ARC_SAMPLES_PER_SEGMENT = 200
VERTICAL_RUN_SLOPE = 0.15  # |dx| under this fraction of |dy| counts as vertical
OUTLINE_WORD_STEP = 3
DETAIL_WORD_STEP = 4
FIELD_DOT_SPACING = 7.5  # the JetBrains Mono advance at 12.5px, so dot fields sit on the text grid
FIELD_DOT_RADIUS = 1.1


def point_in_polygon(x: float, y: float, polygon: list) -> bool:
    """Even-odd test: is the local point inside the polygon."""
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def hex_grid_dots_in_polygon(polygon: list, spacing: float = FIELD_DOT_SPACING) -> list:
    """Hex-grid points (local coords) inside the polygon, on a fixed grid so fields align with the type."""
    xs = [x for x, _ in polygon]
    ys = [y for _, y in polygon]
    dots, row = [], 0
    y = min(ys)
    while y <= max(ys):
        x = min(xs) + (spacing / 2 if row % 2 else 0)
        while x <= max(xs):
            if point_in_polygon(x, y, polygon):
                dots.append((x, y))
            x += spacing
        y += spacing * 0.866
        row += 1
    return dots


def sample_chain(path: list, mirrored: bool = False, samples_per_segment: int = 40) -> list:
    """Dense polyline (local coords) along a Bezier chain, optionally mirrored across x=0."""
    start, segments = path
    points, current = [start], start
    for (x1, y1), (x2, y2), (x3, y3) in segments:
        x0, y0 = current
        for i in range(1, samples_per_segment + 1):
            t = i / samples_per_segment
            u = 1 - t
            points.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                           u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
        current = (x3, y3)
    return [(-x, y) for x, y in points] if mirrored else points


def head_silhouette() -> list:
    """Closed polygon of the whole head: right outline up, brow across, left outline down."""
    return sample_chain(HEAD_OUTLINE) + sample_chain(BROW)[::-1] + sample_chain(HEAD_OUTLINE, mirrored=True)[::-1]


def tail_silhouette() -> list:
    """Closed polygon of the tail: outer edge root to tip, then inner edge tip to root."""
    outer = sample_chain(TAIL_ROOT_OUTER) + sample_chain(TAIL_OUTER)[1:] + sample_chain(TAIL_OUTER_TRAIL)[1:]
    inner = sample_chain(TAIL_ROOT_INNER) + sample_chain(TAIL_INNER)[1:] + sample_chain(TAIL_INNER_TRAIL)[1:]
    return outer + inner[::-1]


def tail_tip() -> list:
    """Closed polygon of the last stretch of the tail, the white tip."""
    return sample_chain(TAIL_OUTER_TRAIL) + sample_chain(TAIL_INNER_TRAIL)[::-1]


def muzzle_field(mirrored: bool) -> list:
    """Closed polygon of one pale cheek-and-muzzle half: under the mask line down to the chin."""
    sign = -1 if mirrored else 1
    mask_edge = sample_chain(MASK, mirrored)[::-1]  # from under the eye out to the cheek
    return [(0, 44), (sign * 36, 54)] + mask_edge + [(sign * 72, 186), (0, 236)]


def eye_almond(mirrored: bool) -> list:
    """Closed polygon of one eye: upper lid then lower lid reversed."""
    return sample_chain(EYE_UPPER, mirrored) + sample_chain(EYE_LOWER, mirrored)[::-1]


def oriented_run(path: list, first_segment: int, last_segment: int, mirrored: bool = False) -> list:
    """Segments first..last of a path as one continuous run, mirrored if asked, oriented to read upright."""
    start, segments = path
    points = [start] + [point for segment in segments for point in segment]
    points = points[3 * first_segment: 3 * (last_segment + 1) + 1]
    if mirrored:
        points = [(-x, y) for x, y in points]
    dx, dy = points[-1][0] - points[0][0], points[-1][1] - points[0][1]
    is_vertical = abs(dx) < VERTICAL_RUN_SLOPE * abs(dy)
    if (not is_vertical and dx < 0) or (is_vertical and dy < 0):
        points = points[::-1]
    return [points[0], [tuple(points[i:i + 3]) for i in range(1, len(points), 3)]]


def each_segment_as_run(path: list, mirrored: bool = False) -> list:
    """Every segment of a path as its own oriented run."""
    return [oriented_run(path, index, index, mirrored) for index in range(len(path[1]))]


def arc_length(path: list, scale: float) -> float:
    """Arc length of a Bezier chain in page pixels, by dense sampling."""
    start, segments = path
    total, current = 0.0, start
    for (x1, y1), (x2, y2), (x3, y3) in segments:
        x0, y0 = current
        samples = []
        for i in range(ARC_SAMPLES_PER_SEGMENT + 1):
            t = i / ARC_SAMPLES_PER_SEGMENT
            u = 1 - t
            samples.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                            u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
        total += sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(samples, samples[1:]))
        current = (x3, y3)
    return scale * total


def pack_words_onto_curve(words: list, length_px: float, font_px: float, trail_dots: bool) -> str:
    """Whole words, cycled, until the stroke is full; never ends on a bare `mineru`; optional dot trail."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    packed, index = "", 0
    while True:
        candidate = (packed + " " + words[index % len(words)]).strip()
        if len(candidate) > capacity:
            break
        packed, index = candidate, index + 1
    if packed.split() and packed.split()[-1] == "mineru" and len(packed.split()) > 1:
        packed = packed.rsplit(" ", 1)[0]
    if packed == "mineru":  # a stroke too short for a verb shows the longest verb that fits instead
        fitting = [word for word in words if word != "mineru" and len(word) <= capacity]
        packed = fitting[0] if fitting else packed  # first fitting word of the rotated list, so the two sides differ
    if trail_dots:
        while len(packed) + 2 <= capacity:
            packed += " ·"
    return " " + packed


def repeat_pattern_onto_curve(pattern: str, length_px: float, font_px: float) -> str:
    """A character pattern repeated to exactly fill the stroke."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    return " " + (pattern * (capacity // len(pattern) + 1))[:max(capacity, 0)]


def fox_svg(center_x: float, center_y: float, scale: float) -> str:
    """The whole fox as SVG `<defs>` paths, fills, dot fields and `<textPath>` text, placed and scaled on the page."""
    path_defs, elements = [], []

    def to_page(point: tuple) -> tuple:
        return center_x + point[0] * scale, center_y + point[1] * scale

    def place(path: list) -> str:
        fmt = lambda p: "{:.1f},{:.1f}".format(*to_page(p))  # noqa: E731
        start, segments = path
        return f"M{fmt(start)} " + " ".join("C" + " ".join(fmt(p) for p in segment) for segment in segments)

    def add_text_on_path(path: list, text: str, css_class: str) -> None:
        path_id = f"p{len(path_defs)}"
        path_defs.append(f'<path id="{path_id}" d="{place(path)}"/>')
        elements.append(f'<text class="{css_class}"><textPath href="#{path_id}">{html.escape(text)}</textPath></text>')

    def polygon(points: list, css_class: str) -> str:
        return f'<polygon class="{css_class}" points="' + " ".join("{:.1f},{:.1f}".format(*to_page(p)) for p in points) + '"/>'

    def dot_field(points: list, css_class: str) -> None:
        for dot in points:
            px, py = to_page(dot)
            elements.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{FIELD_DOT_RADIUS * scale:.1f}" class="{css_class}"/>')

    # The tail is clipped to the region outside the head silhouette, so it emerges from behind the cheek.
    page_w, page_h = center_x + 2000, center_y + 2000
    path_defs.append(f'<clipPath id="outside-head" clip-rule="evenodd"><path clip-rule="evenodd" d="M-2000,-2000 H{page_w:.0f} V{page_h:.0f} H-2000 Z '
                     + "M" + " L".join("{:.1f},{:.1f}".format(*to_page(p)) for p in head_silhouette()) + ' Z"/></clipPath>')
    elements.append('<g clip-path="url(#outside-head)">')
    elements.append(polygon(tail_silhouette(), "tail-fill"))
    dot_field(hex_grid_dots_in_polygon(tail_silhouette()), "tail-field")
    elements.append(polygon(tail_tip(), "tail-tip"))
    dot_field(hex_grid_dots_in_polygon(tail_tip()), "tail-tip-field")
    elements.append('</g>')
    elements.append(polygon(head_silhouette(), "head-fill"))
    for mirrored in (False, True):
        sign = -1 if mirrored else 1
        elements.append(polygon([(sign * x, y) for x, y in EAR_FIELD], "ear-fill"))
        dot_field(hex_grid_dots_in_polygon([(sign * x, y) for x, y in EAR_FIELD]), "ear-field")
        elements.append(polygon(muzzle_field(mirrored), "muzzle-fill"))
        dot_field(hex_grid_dots_in_polygon(muzzle_field(mirrored)), "muzzle-field")
    elements.append(polygon(BLAZE_POLYGON, "blaze"))

    detail_offset = 0
    for mirrored in (False, True):
        sign = -1 if mirrored else 1
        for run_index, run in enumerate(each_segment_as_run(HEAD_OUTLINE, mirrored)):
            rotation = ((run_index + 2 + (1 if mirrored else 0)) * OUTLINE_WORD_STEP) % len(OUTLINE_WORDS)
            words = OUTLINE_WORDS[rotation:] + OUTLINE_WORDS[:rotation]
            add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
        for fur in CHEEK_FUR:
            rotation = detail_offset % len(DETAIL_WORDS)
            detail_offset += DETAIL_WORD_STEP
            words = (" · ".join(DETAIL_WORDS[rotation:] + DETAIL_WORDS[:rotation]) + " ·").split()
            for run in each_segment_as_run(fur, mirrored):
                add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["fur"], True), "fur")
        for detail in (INNER_EAR, MASK):
            for run in each_segment_as_run(detail, mirrored):
                rotation = detail_offset % len(DETAIL_WORDS)
                detail_offset += DETAIL_WORD_STEP
                words = (" · ".join(DETAIL_WORDS[rotation:] + DETAIL_WORDS[:rotation]) + " ·").split()
                add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["detail"], True), "detail")
        # The eye: cream almond, two-value amber iris clipped to it, vertical pupil, one highlight, heavy upper lid.
        eye_id = f"eye{int(mirrored)}"
        path_defs.append(f'<clipPath id="{eye_id}">{polygon(eye_almond(mirrored), "")}</clipPath>')
        ix, iy = to_page((sign * IRIS_CENTER[0], IRIS_CENTER[1]))
        hx, hy = to_page((sign * HIGHLIGHT[0][0], HIGHLIGHT[0][1]))
        elements.append(polygon(eye_almond(mirrored), "eye-white"))
        elements.append(f'<g clip-path="url(#{eye_id})">'
                        f'<circle cx="{ix:.1f}" cy="{iy:.1f}" r="{IRIS_OUTER_RADIUS * scale:.1f}" class="iris-outer"/>'
                        f'<circle cx="{ix:.1f}" cy="{iy:.1f}" r="{IRIS_INNER_RADIUS * scale:.1f}" class="iris"/>'
                        f'<ellipse cx="{ix:.1f}" cy="{iy:.1f}" rx="{PUPIL_SLIT[0] * scale:.1f}" ry="{PUPIL_SLIT[1] * scale:.1f}" class="pupil"/>'
                        f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="{HIGHLIGHT[1] * scale:.1f}" class="highlight"/></g>')
        elements.append(f'<path class="eye-lid" d="{place([(sign * EYE_UPPER[0][0], EYE_UPPER[0][1]), [tuple((sign * x, y) for x, y in seg) for seg in EYE_UPPER[1]]])}"/>')
    for run in each_segment_as_run(BROW):
        add_text_on_path(run, pack_words_onto_curve(OUTLINE_WORDS, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
    elements.append('<g clip-path="url(#outside-head)">')
    for run_index, run in enumerate(each_segment_as_run(TAIL_OUTER)):
        rotation = (run_index * OUTLINE_WORD_STEP) % len(TAIL_WORDS)
        words = TAIL_WORDS[rotation:] + TAIL_WORDS[:rotation]
        add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["tail"], True), "tail")
    for stroke in (TAIL_ROOT_OUTER, TAIL_ROOT_INNER, TAIL_INNER, TAIL_INNER_TRAIL, TAIL_OUTER_TRAIL):
        for run in each_segment_as_run(stroke):
            add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["tail_core"]), "tail-core")
    elements.append('</g>')
    elements.append(polygon(NOSE_POLYGON, "nose"))
    return f'<defs>{"".join(path_defs)}</defs>{"".join(elements)}'
