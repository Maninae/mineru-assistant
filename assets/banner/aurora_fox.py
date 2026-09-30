"""A neon single-line fox drawn as text-on-path: two strokes, every character a piece of `mineru` CLI text.

The glyph is the aurora logo: one continuous outline (two horn-like ears, a dip between them, rounded
cheeks into a U-shaped chin) and one inner swoosh for the eyes and snout. Both strokes are filled with
a mint-to-cyan-to-violet gradient and duplicated underneath with a Gaussian blur for the neon glow.
Dotted echoes of the outline, offset inward and outward, make the aurora trail.

Geometry is authored in a local frame with the face axis on x=0 and y pointing down. Each path is
`[start, [(control1, control2, end), ...]]`, a chain of cubic Beziers.

- Every outline segment is its own run, re-oriented so its words read upright wherever they sit.
- JetBrains Mono is monospace, so how many characters fit on a stroke is its arc length divided by
  the glyph advance; runs are packed with whole words so no corner collides mid-word.
- Echo trails use a symmetric glyph (`·`) so their orientation never matters.
"""
import html

# The outline, one closed loop authored counter-clockwise from the left of the chin. Segments:
# chin, right cheek, right ear tip, right ear inner edge, left ear inner edge, left ear tip, left cheek.
OUTLINE = [(-170, 150), [
    ((-90, 206), (90, 206), (170, 150)),         # chin
    ((240, 80), (276, -40), (250, -140)),        # right cheek up
    ((262, -192), (250, -218), (228, -208)),     # right ear, rounded tip
    ((172, -182), (92, -122), (0, -116)),        # right ear inner edge down to the dip
    ((-92, -122), (-172, -182), (-228, -208)),   # left ear inner edge up from the dip
    ((-250, -218), (-262, -192), (-250, -140)),  # left ear, rounded tip
    ((-276, -40), (-240, 80), (-170, 150)),      # left cheek down
]]
# The inner swoosh: left eye, down across the snout, up into the right eye.
SWOOSH = [(-162, -6), [((-104, -2), (-70, 52), (-20, 90)), ((32, 126), (112, 70), (162, -12))]]

OUTLINE_WORDS = ("mineru setup mineru profile install --apply mineru memory warm-resume "
                 "mineru secrets set mineru cron status mineru memory consolidate mineru profile export").split()
SWOOSH_WORDS = "mineru memory warm-resume mineru cron status mineru calendar list".split()

FONT_PX_BY_ROLE = {
    "outline": 13,
    "swoosh": 12,
    "echo": 9,
}
MONO_ADVANCE_EM = 0.6  # JetBrains Mono glyph width as a fraction of font size
JOINT_GAP_CHARS = 0
ARC_SAMPLES_PER_SEGMENT = 200
VERTICAL_RUN_SLOPE = 0.15  # |dx| under this fraction of |dy| counts as vertical
OUTLINE_WORD_STEP = 3
# Echo trails: (offset in local units, negative = inward, css class). Dotted copies of the outline.
ECHO_TRAILS = [(-14, "echo-1"), (-28, "echo-2"), (14, "echo-1"), (28, "echo-3")]
SWOOSH_ECHOES = [(-12, "echo-2"), (12, "echo-2")]


def oriented_run(path: list, first_segment: int, last_segment: int) -> list:
    """Segments first..last of a path as one continuous run, oriented to read upright."""
    start, segments = path
    points = [start] + [point for segment in segments for point in segment]
    points = points[3 * first_segment: 3 * (last_segment + 1) + 1]
    dx, dy = points[-1][0] - points[0][0], points[-1][1] - points[0][1]
    is_vertical = abs(dx) < VERTICAL_RUN_SLOPE * abs(dy)
    if (not is_vertical and dx < 0) or (is_vertical and dy < 0):
        points = points[::-1]
    return [points[0], [tuple(points[i:i + 3]) for i in range(1, len(points), 3)]]


def each_segment_as_run(path: list) -> list:
    """Every segment of a path as its own oriented run."""
    return [oriented_run(path, index, index) for index in range(len(path[1]))]


def sample_chain(path: list, samples_per_segment: int = 60) -> list:
    """Dense polyline (local coords) along a Bezier chain."""
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
    return points


def offset_polyline(points: list, distance: float, closed: bool) -> list:
    """Shift a polyline along its normals by `distance` (positive = to the right of travel)."""
    count = len(points)
    shifted = []
    for index, (x, y) in enumerate(points):
        prev_point = points[index - 1] if (index > 0 or closed) else points[index]
        next_point = points[(index + 1) % count] if (index < count - 1 or closed) else points[index]
        tx, ty = next_point[0] - prev_point[0], next_point[1] - prev_point[1]
        length = (tx * tx + ty * ty) ** 0.5 or 1.0
        nx, ny = -ty / length, tx / length
        shifted.append((x + nx * distance, y + ny * distance))
    return shifted


def polyline_length(points: list, scale: float) -> float:
    """Length of a polyline in page pixels."""
    return scale * sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(points, points[1:]))


def arc_length(path: list, scale: float) -> float:
    """Arc length of a Bezier chain in page pixels."""
    return polyline_length(sample_chain(path, ARC_SAMPLES_PER_SEGMENT), scale)


def pack_words_onto_curve(words: list, length_px: float, font_px: float) -> str:
    """Whole words, cycled, until the stroke is full; a stroke too short for a verb shows the first verb that fits."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    packed, index = "", 0
    while True:
        candidate = (packed + " " + words[index % len(words)]).strip()
        if len(candidate) > capacity:
            break
        packed, index = candidate, index + 1
    if packed.split() and packed.split()[-1] == "mineru" and len(packed.split()) > 1:
        packed = packed.rsplit(" ", 1)[0]
        # Backfill the room the bare `mineru` left with the longest verb that fits.
        room = capacity - len(packed) - 1
        fitting = [word for word in words if word != "mineru" and len(word) <= room]
        if fitting:
            packed += " " + max(fitting, key=len)
    if packed in ("", "mineru"):
        fitting = [word for word in words if word != "mineru" and len(word) <= capacity]
        packed = fitting[0] if fitting else ""
    # Pad to the full stroke with dots so the neon line never breaks at a corner.
    if packed:
        packed += " "
    return packed + "·" * max(capacity - len(packed), 0)


def repeat_pattern_onto_curve(pattern: str, length_px: float, font_px: float) -> str:
    """A character pattern repeated to exactly fill the stroke."""
    capacity = int(length_px / (MONO_ADVANCE_EM * font_px)) - JOINT_GAP_CHARS
    return " " + (pattern * (capacity // len(pattern) + 1))[:max(capacity, 0)]


def fox_svg(center_x: float, center_y: float, scale: float) -> str:
    """The whole glyph as SVG: gradient + glow defs, echo trails, glow copy, then the crisp text strokes."""
    path_defs, elements = [], []

    def to_page(point: tuple) -> tuple:
        return center_x + point[0] * scale, center_y + point[1] * scale

    def place_chain(path: list) -> str:
        fmt = lambda p: "{:.1f},{:.1f}".format(*to_page(p))  # noqa: E731
        start, segments = path
        return f"M{fmt(start)} " + " ".join("C" + " ".join(fmt(p) for p in segment) for segment in segments)

    def place_polyline(points: list) -> str:
        return "M" + " L".join("{:.1f},{:.1f}".format(*to_page(p)) for p in points)

    def add_path(d: str) -> str:
        path_id = f"p{len(path_defs)}"
        path_defs.append(f'<path id="{path_id}" d="{d}"/>')
        return path_id

    def text_on(path_id: str, text: str, css_class: str) -> str:
        return f'<text class="{css_class}"><textPath href="#{path_id}">{html.escape(text)}</textPath></text>'

    # Gradient across the glyph: mint on the left, cyan, violet, lavender on the right.
    left_x, right_x = to_page((-280, 0))[0], to_page((280, 0))[0]
    path_defs.append(f'<linearGradient id="aurora" gradientUnits="userSpaceOnUse" x1="{left_x:.0f}" y1="0" x2="{right_x:.0f}" y2="0">'
                     '<stop offset="0" stop-color="#8cf7c8"/><stop offset="0.35" stop-color="#6fdcff"/>'
                     '<stop offset="0.7" stop-color="#8f6bff"/><stop offset="1" stop-color="#c9b8ff"/></linearGradient>')
    path_defs.append('<filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="5"/></filter>')
    path_defs.append('<filter id="halo" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="16"/></filter>')
    path_defs.append('<filter id="haze" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="26"/></filter>')

    # Crisp strokes: outline segment by segment, then the swoosh.
    crisp = []
    for run_index, run in enumerate(each_segment_as_run(OUTLINE)):
        rotation = (run_index * OUTLINE_WORD_STEP) % len(OUTLINE_WORDS)
        words = OUTLINE_WORDS[rotation:] + OUTLINE_WORDS[:rotation]
        path_id = add_path(place_chain(run))
        crisp.append(text_on(path_id, pack_words_onto_curve(words, arc_length(run, scale), FONT_PX_BY_ROLE["outline"]), "outline"))
    swoosh_run = oriented_run(SWOOSH, 0, len(SWOOSH[1]) - 1)  # one continuous run, eye to eye
    path_id = add_path(place_chain(swoosh_run))
    crisp.append(text_on(path_id, pack_words_onto_curve(SWOOSH_WORDS, arc_length(swoosh_run, scale), FONT_PX_BY_ROLE["swoosh"]), "swoosh"))

    # Echo trails: dotted offsets of both strokes.
    echoes = []
    outline_points = sample_chain(OUTLINE)[:-1]
    for distance, css_class in ECHO_TRAILS:
        trail = offset_polyline(outline_points, distance, closed=True)
        path_id = add_path(place_polyline(trail + trail[:1]))
        echoes.append(text_on(path_id, repeat_pattern_onto_curve("· ", polyline_length(trail + trail[:1], scale), FONT_PX_BY_ROLE["echo"]), css_class))
    swoosh_points = sample_chain(SWOOSH)
    for distance, css_class in SWOOSH_ECHOES:
        trail = offset_polyline(swoosh_points, distance, closed=False)
        path_id = add_path(place_polyline(trail))
        echoes.append(text_on(path_id, repeat_pattern_onto_curve("· ", polyline_length(trail, scale), FONT_PX_BY_ROLE["echo"]), css_class))

    cx, cy = to_page((0, 0))
    haze = f'<ellipse cx="{cx:.0f}" cy="{cy - 10 * scale:.0f}" rx="{230 * scale:.0f}" ry="{190 * scale:.0f}" class="haze" filter="url(#haze)"/>'
    glow = ('<g class="halo" filter="url(#halo)">' + "".join(crisp) + '</g>'
            '<g class="glow" filter="url(#glow)">' + "".join(crisp) + '</g>')
    return f'<defs>{"".join(path_defs)}</defs>{haze}{"".join(echoes)}{glow}{"".join(crisp)}'
