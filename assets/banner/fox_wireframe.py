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

# Right-half head outline, chin to ear tip: the jaw flows out into the cheek, the ear bows outward and hooks in at the tip.
HEAD_OUTLINE = [(0, 202), [
    ((44, 206), (112, 188), (152, 148)),      # chin out along the jaw
    ((198, 104), (226, 40), (218, -40)),      # cheek rising under the ear
    ((234, -126), (214, -204), (152, -264)),  # outer ear, bowed outward, curling in at the tip
    ((122, -216), (96, -160), (70, -118)),    # inner ear back down to the brow
]]
# The brow spans both halves as one run, so the word at the centre of the forehead is unbroken.
BROW = [(-70, -118), [((-26, -96), (26, -96), (70, -118))]]
# A second stroke just inside the ear, the inner ear.
INNER_EAR = [(150, -236), [((148, -190), (128, -150), (96, -124))]]
# The face mask: from the ear base, around the outside of the eye, into the muzzle and the nose.
MASK = [(84, -96), [((136, -64), (154, 0), (126, 54)), ((104, 96), (64, 132), (26, 152))]]
# The eye: an almond, slanted up and outward. Upper lid dense and bright, lower lid faint.
EYE_UPPER = [(46, -18), [((70, -48), (112, -58), (140, -40))]]
EYE_LOWER = [(50, -10), [((76, 4), (112, -6), (136, -32))]]
PUPIL_POINTS = [(96, -30), (-96, -30)]
# Cheek fur: three sweeps flowing out and down from the jaw, like a ruff.
CHEEK_FUR = [
    [(150, 130), [((190, 128), (222, 140), (248, 168))]],
    [(134, 158), [((170, 166), (196, 188), (208, 220))]],
    [(104, 184), [((132, 198), (146, 220), (150, 246))]],
]
# The blaze: a faint dotted line down the centre of the forehead.
BLAZE = [(0, -86), [((0, -60), (0, -30), (0, -2))]]
# The nose: a small filled diamond at the tip of the muzzle (local coords, drawn as a polygon).
NOSE_POLYGON = [(0, 160), (8, 168), (0, 178), (-8, 168)]
# The tail: a teardrop that starts below the chin, sweeps left and curls up beside the head, cream at the tip.
# Outer and inner edges carry commands; two dotted cores fill the body so it reads bushy, not as a single line.
TAIL_OUTER = [(40, 300), [((-150, 380), (-410, 300), (-402, 80)), ((-396, -100), (-316, -222), (-236, -250))]]
TAIL_INNER = [(0, 286), [((-130, 336), (-326, 276), (-326, 80)), ((-326, -60), (-280, -180), (-236, -250))]]
TAIL_CORES = [
    [(26, 295), [((-142, 364), (-380, 292), (-376, 80)), ((-372, -86), (-302, -208), (-236, -250))]],
    [(12, 290), [((-136, 350), (-352, 284), (-350, 80)), ((-348, -72), (-290, -194), (-236, -250))]],
]
TAIL_TIP = [(-262, -222), [((-244, -250), (-218, -276), (-190, -298))]]
EAR_TIP_POINTS = [(152, -264), (-152, -264)]

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
    "eye": 12,
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
            rotation = ((run_index + 2) * OUTLINE_WORD_STEP) % len(OUTLINE_WORDS)
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
        for run in each_segment_as_run(EYE_UPPER, mirrored):
            add_text_on_path(run, repeat_pattern_onto_curve("•", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "eye")
        for run in each_segment_as_run(EYE_LOWER, mirrored):
            add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "eye-lower")
    for run in each_segment_as_run(BROW):
        add_text_on_path(run, pack_words_onto_curve(OUTLINE_WORDS, arc_length(run, scale), FONT_PX_BY_ROLE["outline"], False), "outline")
    for run in each_segment_as_run(BLAZE):
        add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["blaze"]), "blaze")
    for stroke, css_class, role in ((TAIL_OUTER, "tail", "tail"), (TAIL_INNER, "tail-inner", "tail")):
        for run_index, run in enumerate(each_segment_as_run(stroke)):
            rotation = (run_index * OUTLINE_WORD_STEP) % len(TAIL_WORDS)
            words = TAIL_WORDS[rotation:] + TAIL_WORDS[:rotation]
            add_text_on_path(run, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE[role], True), css_class)
    for core in TAIL_CORES:
        for run in each_segment_as_run(core):
            add_text_on_path(run, repeat_pattern_onto_curve("· ", arc_length(run, scale), FONT_PX_BY_ROLE["tail_core"]), "tail-core")
    for run in each_segment_as_run(TAIL_TIP):
        add_text_on_path(run, repeat_pattern_onto_curve("•", arc_length(run, scale), FONT_PX_BY_ROLE["eye"]), "tip-text")
    points = lambda pts, r, cls: "".join(f'<circle cx="{center_x + x * scale:.1f}" cy="{center_y + y * scale:.1f}" r="{r}" class="{cls}"/>' for x, y in pts)  # noqa: E731
    nose = '<polygon class="nose" points="' + " ".join(f"{center_x + x * scale:.1f},{center_y + y * scale:.1f}" for x, y in NOSE_POLYGON) + '"/>'
    marks = nose + points(EAR_TIP_POINTS, 3.2, "tip") + points(PUPIL_POINTS, 4.2, "pupil")
    return f'<defs>{"".join(path_defs)}</defs>{"".join(text_elements)}{marks}'
