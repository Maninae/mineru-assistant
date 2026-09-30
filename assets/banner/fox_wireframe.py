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

# Right-half head outline, chin to brow: chin -> jaw -> cheek -> ear outer edge -> ear tip -> ear inner edge -> brow.
HEAD_OUTLINE = [(0, 232), [
    ((70, 206), (128, 168), (166, 128)),      # chin to jaw, nearly straight so the chin stays sharp
    ((200, 90), (222, 30), (226, -50)),       # jaw up the cheek
    ((208, -110), (192, -180), (176, -256)),  # cheek up the outer ear edge to the tip
    ((150, -210), (114, -160), (78, -116)),   # ear tip down the inner ear edge
]]
# The brow spans both halves as one run, so the word at the centre of the forehead is unbroken.
BROW = [(-78, -116), [((-30, -92), (30, -92), (78, -116))]]
# A second stroke just inside the ear, the inner ear.
INNER_EAR = [(168, -220), [((150, -186), (130, -150), (104, -122))]]
# The eye: a lifted arc, so the fox reads calm rather than fierce.
EYE = [(52, -22), [((70, -40), (108, -46), (134, -30))]]
# The muzzle: a stroke from under the eye converging on the nose.
MUZZLE = [(96, 36), [((80, 84), (54, 130), (24, 160))]]
# The nose: a small filled diamond at the tip of the muzzle (local coords, drawn as a polygon).
NOSE_POLYGON = [(0, 162), (8, 170), (0, 180), (-8, 170)]
# Cheek fur: two short ticks off the jaw.
CHEEK_TICKS = [
    [(176, 62), [((196, 68), (216, 76), (238, 84))]],
    [(150, 118), [((172, 126), (194, 136), (214, 148))]],
]
# Ear marks: the tips carry a single bright dot.
EAR_TIP_POINTS = [(176, -256), (-176, -256)]

OUTLINE_WORDS = ("mineru setup mineru profile install --apply mineru memory warm-resume "
                 "mineru secrets set mineru cron status mineru memory consolidate").split()
DETAIL_WORDS = ["profile", "memory", "secrets", "cron", "calendar", "gmail", "telegram", "browser",
                "warm-resume", "consolidate", "backup", "tree", "reindex", "validate", "export"]

FONT_PX_BY_ROLE = {
    "outline": 12.5,
    "detail": 9.5,
    "eye": 12.5,
    "tick": 10,
}
MONO_ADVANCE_EM = 0.6  # JetBrains Mono glyph width as a fraction of font size
JOINT_GAP_CHARS = 2
ARC_SAMPLES_PER_SEGMENT = 200
VERTICAL_RUN_SLOPE = 0.15  # |dx| under this fraction of |dy| counts as vertical
OUTLINE_WORD_STEP = 3
DETAIL_WORD_STEP = 4


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
    for mirrored in (False, True):
        for run_index, run in enumerate(each_segment_as_run(HEAD_OUTLINE, mirrored)):
            rotation = (run_index * OUTLINE_WORD_STEP) % len(OUTLINE_WORDS)
            words = OUTLINE_WORDS[rotation:] + OUTLINE_WORDS[:rotation]
            add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
        for detail in (INNER_EAR, MUZZLE):
            rotation = detail_offset % len(DETAIL_WORDS)
            detail_offset += DETAIL_WORD_STEP
            words = (" · ".join(DETAIL_WORDS[rotation:] + DETAIL_WORDS[:rotation]) + " ·").split()
            for run in each_segment_as_run(detail, mirrored):
                add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["detail"], True), "detail")
        for run in each_segment_as_run(EYE, mirrored):
            add_text_on_path(run, repeat_pattern_onto_curve("• ", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "eye")
        for tick in CHEEK_TICKS:
            for run in each_segment_as_run(tick, mirrored):
                add_text_on_path(run, repeat_pattern_onto_curve("- ", arc_length(run, scale), FONT_PX_BY_ROLE["tick"]), "tick")
    for run in each_segment_as_run(BROW):
        add_text_on_path(run, pack_words_onto_curve(OUTLINE_WORDS, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
    nose = '<polygon class="nose" points="' + " ".join(f"{center_x + x * scale:.1f},{center_y + y * scale:.1f}" for x, y in NOSE_POLYGON) + '"/>'
    ear_tips = nose + "".join(f'<circle cx="{center_x + x * scale:.1f}" cy="{center_y + y * scale:.1f}" r="3.2" class="tip"/>'
                       for x, y in EAR_TIP_POINTS)
    return f'<defs>{"".join(path_defs)}</defs>{"".join(text_elements)}{ear_tips}'
