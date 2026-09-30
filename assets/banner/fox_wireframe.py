"""A fox head drawn as a text-on-path wireframe: every stroke is a line of `mineru` CLI text.

Front-facing, symmetric, angular in the spirit of a motorsport-style fox emblem, but with upright
ears and a lifted, calm eye. Geometry is authored once for the RIGHT half in a local frame with the
face axis on x=0 and y pointing down; the left half is a mirrored copy.

Each path is `[start, [(control1, control2, end), ...]]`, a chain of cubic Beziers.

- Text must read upright on both sides, so every run is re-oriented left-to-right (top-to-bottom
  only when it is truly vertical) after mirroring.
- JetBrains Mono is monospace, so how many characters fit on a stroke is its arc length divided by
  the glyph advance; runs are packed with whole words so no corner collides mid-word.
- The head outline is one polyline (chin, cheek, ear, brow); each straight segment is its own run
  so the corners stay crisp and every word reads upright.
"""
import html

# Right-half head outline, chin to ear tip. A fox head is a triangle: narrow pointed snout, wide cheeks, tall ears.
HEAD_OUTLINE = [(0, 236), [
    ((26, 228), (76, 206), (118, 168)),       # sharp chin up the narrow muzzle
    ((176, 124), (230, 60), (228, -30)),      # the cheek bulging out under the ear
    ((236, -130), (222, -210), (168, -272)),  # outer ear, tall, curling in at the tip
    ((138, -222), (104, -164), (72, -122)),   # inner ear back down to the brow
]]
# The brow spans both halves as one run, so the word at the centre of the forehead is unbroken.
BROW = [(-72, -122), [((-28, -100), (28, -100), (72, -122))]]
# A second stroke just inside the ear, the inner ear.
INNER_EAR = [(164, -244), [((158, -196), (136, -156), (100, -128))]]
# The face mask: from the ear base, around the outside of the eye, down toward the muzzle.
MASK = [(86, -100), [((142, -66), (160, 10), (126, 66)), ((110, 94), (90, 116), (64, 134))]]
# The eye: a large almond slanted up and outward, a dotted iris ring and a filled pupil.
EYE_UPPER = [(36, -24), [((64, -62), (120, -68), (154, -42))]]
EYE_LOWER = [(40, -12), [((68, 8), (120, 0), (152, -34))]]
IRIS_CENTER = (95, -32)
IRIS_RADIUS = 11
PUPIL_RADIUS = 5
# Whiskers: two per side, sweeping out from the muzzle across the cheek.
WHISKERS = [
    [(44, 140), [((100, 130), (160, 128), (214, 146))]],
    [(44, 154), [((96, 158), (154, 164), (206, 174))]],
]
# Cheek fur: three sweeps flaring out and down from the cheek, like a ruff.
CHEEK_FUR = [
    [(200, 96), [((236, 100), (262, 116), (284, 146))]],
    [(186, 128), [((222, 140), (244, 166), (254, 200))]],
    [(160, 160), [((190, 178), (206, 204), (210, 238))]],
]
# Dotted fur fields: polygons (local coords) filled with a jittered hex grid of faint dots.
EAR_FIELD = [(166, -256), (218, -70), (102, -122)]
CHEEK_FIELD = [(96, -92), (146, -56), (160, 10), (126, 66), (70, 130), (118, 166), (176, 122), (224, 58), (226, -30), (200, -66)]
# The blaze: a faint dotted line down the centre of the forehead.
BLAZE = [(0, -90), [((0, -60), (0, -30), (0, 0))]]
# The nose: a small filled diamond at the tip of the muzzle (local coords, drawn as a polygon).
NOSE_POLYGON = [(0, 176), (9, 185), (0, 196), (-9, 185)]
# The tail: emerges below the chin, sweeps left, curls up beside the head and hooks inward at the tip.
# Widest at the lower-left bend, tapering toward both the root and the tip; two dotted cores fill the body.
# The root tucks behind the right cheek fur; the last leg of each edge is a dotted trail so the cream tip stands alone.
TAIL_OUTER = [(176, 272), [((-40, 380), (-420, 330), (-412, 90)), ((-410, -30), (-400, -100), (-380, -150))]]
TAIL_OUTER_TRAIL = [(-380, -150), [((-350, -216), (-290, -252), (-216, -262)), ((-196, -266), (-182, -256), (-186, -238))]]
TAIL_INNER = [(130, 250), [((-40, 330), (-330, 296), (-334, 90)), ((-336, -20), (-330, -80), (-318, -126))]]
TAIL_INNER_TRAIL = [(-318, -126), [((-300, -180), (-270, -212), (-232, -224))]]
TAIL_CORES = [
    [(160, 264), [((-40, 364), (-392, 318), (-386, 90)), ((-382, -96), (-326, -218), (-240, -236))]],
    [(146, 256), [((-40, 348), (-360, 306), (-360, 90)), ((-360, -84), (-312, -200), (-236, -232))]],
]
# The white tip of the tail: two dense cream strokes beyond where the edge words stop.
TAIL_TIPS = [
    [(-286, -212), [((-262, -240), (-236, -258), (-206, -272))]],
    [(-304, -196), [((-282, -232), (-254, -260), (-220, -288))]],
]
EAR_TIP_POINTS = [(168, -272), (-168, -272)]

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
    "eye": 13,
    "whisker": 9,
    "tail": 11.5,
    "tail_core": 9,
    "blaze": 9,
}
MONO_ADVANCE_EM = 0.6  # JetBrains Mono glyph width as a fraction of font size
JOINT_GAP_CHARS = 2
ARC_SAMPLES_PER_SEGMENT = 200
VERTICAL_RUN_SLOPE = 0.15  # |dx| under this fraction of |dy| counts as vertical
OUTLINE_WORD_STEP = 3
DETAIL_WORD_STEP = 4
FIELD_DOT_SPACING = 9.0
FIELD_DOT_JITTER = 2.2
FIELD_DOT_RADIUS = 1.1


def point_in_polygon(x: float, y: float, polygon: list) -> bool:
    """Even-odd test: is the local point inside the polygon."""
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def hex_grid_dots_in_polygon(polygon: list, seed: int) -> list:
    """Jittered hex-grid points (local coords) inside the polygon; deterministic per seed."""
    import random
    rng = random.Random(seed)
    xs = [x for x, _ in polygon]
    ys = [y for _, y in polygon]
    dots, row = [], 0
    y = min(ys)
    while y <= max(ys):
        x = min(xs) + (FIELD_DOT_SPACING / 2 if row % 2 else 0)
        while x <= max(xs):
            jx, jy = x + rng.uniform(-FIELD_DOT_JITTER, FIELD_DOT_JITTER), y + rng.uniform(-FIELD_DOT_JITTER, FIELD_DOT_JITTER)
            if point_in_polygon(jx, jy, polygon):
                dots.append((jx, jy))
            x += FIELD_DOT_SPACING
        y += FIELD_DOT_SPACING * 0.866
        row += 1
    return dots


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
    """Whole words, cycled, until the stroke is full; optionally trail off in dots to reach its end."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    packed, index = "", 0
    while True:
        candidate = (packed + " " + words[index % len(words)]).strip()
        if len(candidate) > capacity:
            break
        packed, index = candidate, index + 1
    if trail_dots:
        while len(packed) + 2 <= capacity:
            packed += " ·"
    return " " + packed


def repeat_pattern_onto_curve(pattern: str, length_px: float, font_px: float) -> str:
    """A character pattern repeated to exactly fill the stroke."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    return " " + (pattern * (capacity // len(pattern) + 1))[:max(capacity, 0)]


def fox_svg(center_x: float, center_y: float, scale: float) -> str:
    """The whole fox head as SVG `<defs>` paths plus `<textPath>` text, placed and scaled on the page."""
    path_defs, text_elements = [], []

    def place(path: list) -> str:
        to_page = lambda p: f"{center_x + p[0] * scale:.1f},{center_y + p[1] * scale:.1f}"  # noqa: E731
        start, segments = path
        return f"M{to_page(start)} " + " ".join("C" + " ".join(to_page(p) for p in segment) for segment in segments)

    def add_text_on_path(path: list, text: str, css_class: str) -> None:
        path_id = f"p{len(path_defs)}"
        path_defs.append(f'<path id="{path_id}" d="{place(path)}"/>')
        text_elements.append(f'<text class="{css_class}"><textPath href="#{path_id}">{html.escape(text)}</textPath></text>')

    detail_offset = 0
    to_page = lambda p: (center_x + p[0] * scale, center_y + p[1] * scale)  # noqa: E731
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
            rotation = detail_offset % len(DETAIL_WORDS)
            detail_offset += DETAIL_WORD_STEP
            words = (" · ".join(DETAIL_WORDS[rotation:] + DETAIL_WORDS[:rotation]) + " ·").split()
            for run in each_segment_as_run(detail, mirrored):
                add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["detail"], True), "detail")
        for whisker in WHISKERS:
            for run in each_segment_as_run(whisker, mirrored):
                add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["whisker"]), "whisker")
        for run in each_segment_as_run(EYE_UPPER, mirrored):
            add_text_on_path(run, repeat_pattern_onto_curve("•", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "eye")
        for run in each_segment_as_run(EYE_LOWER, mirrored):
            add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "eye-lower")
        ix, iy = to_page((sign * IRIS_CENTER[0], IRIS_CENTER[1]))
        text_elements.append(f'<circle cx="{ix:.1f}" cy="{iy:.1f}" r="{IRIS_RADIUS * scale:.1f}" class="iris"/>')
        text_elements.append(f'<circle cx="{ix:.1f}" cy="{iy:.1f}" r="{PUPIL_RADIUS * scale:.1f}" class="pupil"/>')
        for polygon, css_class in ((EAR_FIELD, "field"), (CHEEK_FIELD, "field-faint")):
            for dot_x, dot_y in hex_grid_dots_in_polygon(polygon, seed=7):
                px, py = to_page((sign * dot_x, dot_y))
                text_elements.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{FIELD_DOT_RADIUS * scale:.1f}" class="{css_class}"/>')
    for run in each_segment_as_run(BROW):
        add_text_on_path(run, pack_words_onto_curve(OUTLINE_WORDS, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
    for run in each_segment_as_run(BLAZE):
        add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["blaze"]), "blaze")
    for stroke, css_class in ((TAIL_OUTER, "tail"), (TAIL_INNER, "tail-inner")):
        for run_index, run in enumerate(each_segment_as_run(stroke)):
            rotation = (run_index * OUTLINE_WORD_STEP) % len(TAIL_WORDS)
            words = TAIL_WORDS[rotation:] + TAIL_WORDS[:rotation]
            add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["tail"], True), css_class)
    for trail in (TAIL_OUTER_TRAIL, TAIL_INNER_TRAIL):
        for run in each_segment_as_run(trail):
            add_text_on_path(run, repeat_pattern_onto_curve("•", arc_length(run, scale), FONT_PX_BY_ROLE["tail_core"]), "tail-trail")
    for core in TAIL_CORES:
        for run in each_segment_as_run(core):
            add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["tail_core"]), "tail-core")
    for tip in TAIL_TIPS:
        for run in each_segment_as_run(tip):
            add_text_on_path(run, repeat_pattern_onto_curve("•", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "tip-text")
    nose = '<polygon class="nose" points="' + " ".join(f"{center_x + x * scale:.1f},{center_y + y * scale:.1f}" for x, y in NOSE_POLYGON) + '"/>'
    ear_tips = "".join(f'<circle cx="{center_x + x * scale:.1f}" cy="{center_y + y * scale:.1f}" r="3.2" class="tip"/>' for x, y in EAR_TIP_POINTS)
    return f'<defs>{"".join(path_defs)}</defs>{"".join(text_elements)}{nose}{ear_tips}'
