"""Public-engine hygiene: templates render clean and shipping artifacts stay generic.

Two gates live here (the code/tests gate is in `test_engine_source_no_operator_literals.py`):

  1. TEMPLATE gate. Each `.template` under `engine/{charter,prompts,recurring}/`,
     rendered against the shipped synthetic profile, must render without raising,
     leave zero `{{marker}}`s, and contain zero banned literals.
  2. SHIPPING-ARTIFACT gate. Raw text of every file under `engine/config/`,
     `engine/launchd/`, and `engine/app-deploy/` must contain zero banned literals.
     These are copied or rendered into every install, so a hard-coded operator
     string reaches every fork.

Banned literals = generic product tokens (`GENERIC_TEMPLATE_BANNED_LITERALS`) plus
the operator's private list, loaded at test time and never shipped (see
`leak_gate_literals.py`). The synthetic fixture pins persona=Zephyr, user=Sam.
"""

import os
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import yaml

from leak_gate_literals import (
    FICTIONAL_EXAMPLE_BANNED_LITERALS,
    GENERIC_TEMPLATE_BANNED_LITERALS,
    OPERATOR_BANNED_LITERALS,
    describe_banned_literal_for_failure,
    describe_operator_banned_literals,
)
from mineru_cli.install import build_render_context
from mineru_cli.install.renderer import (
    find_unrendered_markers,
    render_template,
)
from mineru_cli.profile.loader import (
    PROFILE_BASE_DIR_ENV_VAR,
    PROFILE_NAME_ENV_VAR,
    load_active_profile,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_ROOT = REPO_ROOT / "engine"
TEMPLATE_DIRS = ("charter", "prompts", "recurring")
# Engine shipping artifacts scanned as raw text (no render pass).
ENGINE_SHIPPING_ARTIFACT_DIRS = ("config", "launchd", "app-deploy")

# Ships in-repo so this test runs on every fork (persona Zephyr, user Sam,
# household Robin/Juno/Mira/Kai: nothing real).
SYNTHETIC_SRC = Path(__file__).resolve().parent / "fixtures" / "synthetic-profile"

BANNED_LITERALS: Tuple[str, ...] = tuple(
    dict.fromkeys(GENERIC_TEMPLATE_BANNED_LITERALS + OPERATOR_BANNED_LITERALS.literals)
)


def test_operator_banned_literals_loaded() -> None:
    """Surface how many private operator literals the gates are enforcing.

    Skips (with the count and file path in the reason) when zero loaded, which is
    expected on a fresh clone and a red flag on the operator's machine.
    """
    if not OPERATOR_BANNED_LITERALS.literals:
        pytest.skip(describe_operator_banned_literals(OPERATOR_BANNED_LITERALS))
    print(describe_operator_banned_literals(OPERATOR_BANNED_LITERALS))

@pytest.fixture(scope="module")
def synthetic_context(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, object]:
    """Build the exact context `build_render_context` produces against the
    shipped synthetic profile + connectors fixture.

    Skips if the fixture directory is somehow absent (should never happen
    in a normal checkout — the fixtures ship in-repo). Remaps the fixture's
    Linux paths to a per-test tmp dir so the profile loader accepts them
    on macOS.
    """
    profile_yaml_path = SYNTHETIC_SRC / "profile.yaml"
    connectors_yaml_path = SYNTHETIC_SRC / "connectors.yaml"
    if not profile_yaml_path.exists() or not connectors_yaml_path.exists():
        pytest.skip(
            f"synthetic fixture missing at {SYNTHETIC_SRC}; expected both "
            "profile.yaml and connectors.yaml to ship in-repo."
        )

    workspace = tmp_path_factory.mktemp("synth-ws")
    (workspace / "memory").mkdir()

    profile_dict = yaml.safe_load(profile_yaml_path.read_text())
    connectors_dict = yaml.safe_load(connectors_yaml_path.read_text())

    # Remap fixture's Linux paths onto the per-test workspace so the
    # profile loader's exists-checks pass on macOS.
    for key, sub in (
        ("workspace_absolute", ""),
        ("memory_root", "memory"),
        ("user_dev_root", "code"),
        ("user_claude_home", ".config/claude"),
        ("heavy_storage_root", "bulk"),
    ):
        profile_dict[key] = str(workspace / sub) if sub else str(workspace)

    profile_base = tmp_path_factory.mktemp("synth-profile-base")
    (profile_base / "sam").mkdir()
    (profile_base / "sam" / "profile.yaml").write_text(yaml.dump(profile_dict))

    prev_env = {
        PROFILE_BASE_DIR_ENV_VAR: os.environ.get(PROFILE_BASE_DIR_ENV_VAR),
        PROFILE_NAME_ENV_VAR: os.environ.get(PROFILE_NAME_ENV_VAR),
    }
    os.environ[PROFILE_BASE_DIR_ENV_VAR] = str(profile_base)
    os.environ[PROFILE_NAME_ENV_VAR] = "sam"
    try:
        profile = load_active_profile()
        context = build_render_context(
            profile=profile,
            connectors=connectors_dict,
            env={"HOME": str(workspace)},
        )
    finally:
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return context


def _iter_template_paths() -> List[Path]:
    """Every `.template` file under the three shipped template dirs."""
    paths: List[Path] = []
    for sub in TEMPLATE_DIRS:
        base = ENGINE_ROOT / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.template")):
            paths.append(path)
    return paths


TEMPLATE_PATHS = _iter_template_paths()


def test_engine_ships_expected_template_count() -> None:
    """Sanity guard: the shipped template count is within the expected
    range. A template accidentally deleted OR an out-of-scope one added
    fails this test loudly. Update the range if the starter set genuinely
    changes shape."""
    assert 20 <= len(TEMPLATE_PATHS) <= 50, (
        f"unexpected shipped-template count: {len(TEMPLATE_PATHS)} — "
        "audit engine/{charter,prompts,recurring}/ before adjusting."
    )


@pytest.mark.parametrize(
    "template_path",
    TEMPLATE_PATHS,
    ids=lambda p: str(p.relative_to(ENGINE_ROOT)),
)
def test_template_renders_against_synthetic_fixture(
    template_path: Path, synthetic_context: Dict[str, object]
) -> None:
    """The template renders end-to-end against the synthetic context."""
    text = template_path.read_text(encoding="utf-8")
    # A ValueError here means the template references a `{{VAR}}` that
    # `build_render_context` doesn't emit. Either add the var to the
    # context builder + connectors fixture, or drop the reference.
    render_template(text, synthetic_context)


@pytest.mark.parametrize(
    "template_path",
    TEMPLATE_PATHS,
    ids=lambda p: str(p.relative_to(ENGINE_ROOT)),
)
def test_template_leaves_no_unrendered_markers(
    template_path: Path, synthetic_context: Dict[str, object]
) -> None:
    """Rendered output has zero surviving `{{...}}` markers."""
    text = template_path.read_text(encoding="utf-8")
    rendered = render_template(text, synthetic_context)
    leftovers = find_unrendered_markers(rendered)
    assert leftovers == [], (
        f"{template_path.relative_to(ENGINE_ROOT)} has unrendered markers "
        f"after render: {leftovers[:5]}"
    )


@pytest.mark.parametrize(
    "template_path",
    TEMPLATE_PATHS,
    ids=lambda p: str(p.relative_to(ENGINE_ROOT)),
)
def test_template_contains_no_banned_literal(
    template_path: Path, synthetic_context: Dict[str, object]
) -> None:
    """Rendered output contains none of the banned literals.

    Set = generic product tokens plus the operator's private literals
    (`leak_gate_literals.py`).
    """
    text = template_path.read_text(encoding="utf-8")
    rendered = render_template(text, synthetic_context)
    for banned in BANNED_LITERALS:
        assert banned not in rendered, (
            f"{template_path.relative_to(ENGINE_ROOT)} leaked banned literal "
            f"{describe_banned_literal_for_failure(banned)} into rendered output; "
            "parameterize it or replace it with a generic example."
        )


# ---------------------------------------------------------------------------
# Shipping-artifact gate: raw text of engine/{config,launchd,app-deploy}/.
# The template gate above only walks charter/prompts/recurring, so these
# directories need their own scan. No render pass: a literal in the source
# text reaches every install whether or not it is inside a {{VAR}} block.
# ---------------------------------------------------------------------------

SHIPPING_ARTIFACT_SCAN_EXTS = frozenset({
    ".json", ".jsonl",
    ".yaml", ".yml",
    ".txt", ".md",
    ".toml", ".ini", ".cfg",
    ".sh", ".bash",
    ".py", ".plist",
    ".template",
})

# Relative path (from repo root) -> literals legitimately present in that file
# (e.g. a generic example that happens to name a product). Keep entries rare.
SHIPPING_ARTIFACT_ALLOWED_LITERALS_BY_FILE: Dict[str, frozenset] = {
    "engine/config/banned-literals.example.txt": frozenset(FICTIONAL_EXAMPLE_BANNED_LITERALS),
}


def engine_shipping_artifact_paths() -> List[Path]:
    """Every scan-eligible, non-symlink file under the engine shipping-artifact dirs."""
    paths: List[Path] = []
    for sub in ENGINE_SHIPPING_ARTIFACT_DIRS:
        base = ENGINE_ROOT / sub
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if "__pycache__" in path.parts:
                continue
            if path.suffix.lower() not in SHIPPING_ARTIFACT_SCAN_EXTS:
                continue
            paths.append(path)
    return sorted(paths)


ENGINE_SHIPPING_ARTIFACT_PATHS = engine_shipping_artifact_paths()


@pytest.mark.parametrize("artifact_dir_name", ENGINE_SHIPPING_ARTIFACT_DIRS)
def test_engine_shipping_artifact_dir_is_scanned(artifact_dir_name: str) -> None:
    """Each shipping-artifact dir contributes at least one scanned file.

    A collapse to zero means a walker exclusion broke and the gate is silently
    passing every leak in that directory.
    """
    artifact_dir = ENGINE_ROOT / artifact_dir_name
    if not artifact_dir.exists():
        pytest.skip(f"{artifact_dir.relative_to(REPO_ROOT)} does not ship yet.")
    scanned_in_dir = [p for p in ENGINE_SHIPPING_ARTIFACT_PATHS if artifact_dir in p.parents]
    assert scanned_in_dir, (
        f"{artifact_dir.relative_to(REPO_ROOT)} exists but no file under it is "
        "scan-eligible; audit SHIPPING_ARTIFACT_SCAN_EXTS."
    )


@pytest.mark.parametrize(
    "artifact_path",
    ENGINE_SHIPPING_ARTIFACT_PATHS,
    ids=lambda p: str(p.relative_to(ENGINE_ROOT)),
)
def test_engine_shipping_artifact_contains_no_banned_literal(artifact_path: Path) -> None:
    """Raw text of every shipping artifact contains none of `BANNED_LITERALS`."""
    relative_path = artifact_path.relative_to(REPO_ROOT).as_posix()
    try:
        text = artifact_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        pytest.skip(f"unreadable as utf-8: {relative_path}")
    allowed_here = SHIPPING_ARTIFACT_ALLOWED_LITERALS_BY_FILE.get(relative_path, frozenset())
    for banned in BANNED_LITERALS:
        if banned in allowed_here:
            continue
        assert banned not in text, (
            f"{relative_path} contains banned literal "
            f"{describe_banned_literal_for_failure(banned)}, which every install "
            "would inherit. Replace it "
            "with a generic placeholder (e.g. 'Pet Summary', not a vendor name)."
        )
