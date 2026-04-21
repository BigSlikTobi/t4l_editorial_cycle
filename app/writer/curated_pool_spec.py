"""Specification for the pre-generated curated image pool.

Single source of truth for what the pool contains:
  - 224 team-specific rows (32 teams × 7 scenes)
  - 26 generic rows (no team, covers league-wide / fallback stories)
  = 250 images total.

Used by:
  - scripts/build_curated_pool_plan.py (produces plan JSON + taxonomy markdown)
  - scripts/submit_curated_batch.py (builds Gemini batch input)
  - scripts/upload_curated_pool.py (looks up metadata when uploading survivors)

Prompts are intentionally permissive on team colors, logos, and wordmarks:
the pool is curated by hand before going live, so trademark risk is
controlled at review time rather than at generation time.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.team_codes import NFL_TEAM_CODES, team_colors, team_full_name


# Appended to EVERY prompt so we never ship an image with a recognizable
# player or staff face. Two modes: helmets-on for on-field scenes, or
# non-identifiable framing (from behind / side / distance / soft focus) for
# press and office scenes where helmets don't belong.
UNIVERSAL_CONSTRAINTS = (
    "\n\nFACE / PRIVACY RULE (required, non-negotiable): Do NOT render any "
    "identifiable human face. "
    "• For game, practice, pregame, and locker-room scenes: all players must "
    "be wearing helmets with face masks down — no player shown with helmet "
    "off. "
    "• For press-room, office, training-room, media-scrum, combine, and "
    "other scenes where helmets are not natural: show people from behind, "
    "from the side, at a distance, in shadow, or in soft focus so that no "
    "individual face is recognizable. "
    "Avoid any close-up portrait of any identifiable person."
)


@dataclass(frozen=True)
class SceneSpec:
    key: str                # short slug, used in filenames and DB
    description: str        # human summary (for taxonomy doc + DB)
    prompt_template: str    # str.format with {team_full} and {colors}


@dataclass(frozen=True)
class GenericItemSpec:
    key: str                # scene slug (e.g. "press_conference")
    count: int              # how many variants to generate for this scene
    description: str        # human summary
    prompt: str             # fully-formed prompt — no templating


# --- Team-specific scenes (7 × 32 teams = 224 images) --------------------

TEAM_SCENES: list[SceneSpec] = [
    SceneSpec(
        key="offense_action",
        description="Offense in live-game action (QB release or RB breaking tackle)",
        prompt_template=(
            "Wire-service NFL photograph of the {team_full} on offense in live "
            "game action. Players in {colors} uniforms; team branding, "
            "logos, and wordmarks may appear where natural. A quarterback in the "
            "pocket releasing a pass, OR a running back breaking through a "
            "defender's tackle. Real stadium setting with crowd blurred behind. "
            "Natural daylight or stadium lights. Some motion blur on limbs or "
            "ball. AP / Getty / Reuters documentary photojournalism style — "
            "NOT cinematic, NOT moody, NOT illustration, NOT cartoon."
        ),
    ),
    SceneSpec(
        key="defense_action",
        description="Defense making a play (tackle, sack, pass break-up)",
        prompt_template=(
            "Wire-service NFL photograph of the {team_full} defense making a "
            "play in live game action. Defenders in {colors} uniforms; team "
            "branding and logos may appear where natural. A defender wrapping up a ball "
            "carrier mid-tackle, pads colliding, or a sack on the quarterback. "
            "Crowd blurred in background, natural stadium lighting. AP / Getty "
            "documentary photojournalism, NOT cinematic, NOT illustration."
        ),
    ),
    SceneSpec(
        key="sideline",
        description="Head coach on the sideline talking to players during a game",
        prompt_template=(
            "Wire-service NFL photograph of the {team_full} head coach on the "
            "sideline during a game. Coach in {colors} team gear (hat, "
            "headset, pullover) talking intently to a player in uniform. "
            "Bench and blurred stadium crowd behind. Natural game-time "
            "lighting. Documentary photojournalism style — NOT posed, NOT "
            "cinematic."
        ),
    ),
    SceneSpec(
        key="celebration",
        description="Players celebrating a touchdown or big play in the end zone",
        prompt_template=(
            "Wire-service NFL photograph of {team_full} players celebrating "
            "a touchdown in the end zone. Two or three players in {colors} "
            "uniforms (team branding and logos may be visible), arms raised, embracing or "
            "high-fiving, crowd visible cheering behind. Real stadium, "
            "natural lighting. AP / Getty sports photojournalism, NOT "
            "cinematic, NOT posed."
        ),
    ),
    SceneSpec(
        key="pregame_tunnel",
        description="Players emerging from the tunnel before a game",
        prompt_template=(
            "Wire-service NFL photograph of {team_full} players walking out "
            "of the stadium tunnel before a game. Players in full {colors} "
            "uniforms with helmets on (team branding may be visible). Stadium "
            "lights visible down the tunnel mouth, intense expressions, "
            "possible breath condensation or steam. Documentary "
            "photojournalism style, NOT cinematic, NOT slow-motion."
        ),
    ),
    SceneSpec(
        key="locker_room",
        description="Player seated in the locker room in uniform",
        prompt_template=(
            "Wire-service NFL photograph of a {team_full} player in the "
            "locker room in uniform, shown from behind or in three-quarter "
            "rear view so his face is not visible. {colors} jersey and back "
            "of the helmet visible, lockers lined up behind with gear "
            "hanging. Overhead fluorescent lighting, candid composition. "
            "Documentary photojournalism, NOT posed, NOT cinematic."
        ),
    ),
    SceneSpec(
        key="stadium_wide",
        description="Wide stadium shot during a game with team identity visible",
        prompt_template=(
            "Wire-service NFL photograph of {team_full}'s home stadium from "
            "an upper sideline angle during a game. Crowd in {colors} filling "
            "the stands, field visible with small players mid-play in the "
            "distance. Natural daylight or stadium lights. Documentary, "
            "NOT drone-aerial, NOT cinematic color grading."
        ),
    ),
]


# --- Generic scenes (26 images covering no-team / league-wide stories) ---

GENERIC_ITEMS: list[GenericItemSpec] = [
    GenericItemSpec(
        key="press_conference", count=3,
        description="Coach or player at an NFL press conference podium",
        prompt=(
            "Wire-service photograph of an NFL press conference. A coach or "
            "player seated at a plain black or neutral podium, two "
            "microphones in front, blank neutral backdrop behind. Professional "
            "interior press-room lighting. Documentary photojournalism, NOT "
            "cinematic, NOT staged."
        ),
    ),
    GenericItemSpec(
        key="front_office", count=2,
        description="NFL team executives in a front-office meeting",
        prompt=(
            "Wire-service photograph of two NFL team executives in business "
            "attire at a conference-room table reviewing documents, serious "
            "expressions, large window behind with a view of a practice field. "
            "Professional interior lighting. Documentary, NOT cinematic."
        ),
    ),
    GenericItemSpec(
        key="draft_room", count=2,
        description="NFL draft war room with staff at a table watching monitors",
        prompt=(
            "Wire-service photograph of an NFL team draft war room. Several "
            "staff members at a long table in team polos, monitors on the "
            "wall showing player data, earnest discussion. Fluorescent "
            "overhead lighting. Documentary, NOT cinematic."
        ),
    ),
    GenericItemSpec(
        key="medical_training", count=3,
        description="Athletic trainer working on a player in the training room",
        prompt=(
            "Wire-service photograph of an NFL athletic training-room scene. "
            "A trainer in team gear taping or examining a player's ankle, "
            "knee, or shoulder; the player seated on a training table in "
            "practice gear. Medical supplies visible on a counter behind. "
            "Overhead lighting, documentary style."
        ),
    ),
    GenericItemSpec(
        key="coach_player_convo", count=3,
        description="Coach talking one-on-one with a player on the sideline",
        prompt=(
            "Wire-service photograph of an NFL head coach in team gear "
            "talking intently to a player in uniform on the sideline. Coach "
            "holding a laminated play sheet, player wearing his helmet with "
            "face mask down. Both shown from the side or three-quarter rear "
            "angle so faces are not recognizable. Blurred stadium behind. "
            "Documentary photojournalism, natural lighting."
        ),
    ),
    GenericItemSpec(
        key="referee", count=2,
        description="NFL referee signaling a play or penalty on the field",
        prompt=(
            "Wire-service photograph of an NFL referee in the black-and-white "
            "striped uniform signaling a touchdown or penalty, arms raised, "
            "whistle visible, yellow penalty flag in hand if applicable. "
            "Blurred field and players behind. Stadium daylight, documentary."
        ),
    ),
    GenericItemSpec(
        key="media_scrum", count=2,
        description="Player at a locker-room media scrum surrounded by reporters",
        prompt=(
            "Wire-service photograph of an NFL player at a locker-room media "
            "scrum. Player in a team polo speaking, surrounded by reporters "
            "holding microphones, phones, and recorders at chest height. "
            "Fluorescent lighting, candid documentary composition."
        ),
    ),
    GenericItemSpec(
        key="empty_field_dusk", count=3,
        description="Empty NFL stadium field at dusk — neutral editorial fallback",
        prompt=(
            "Wire-service photograph of an empty NFL stadium bowl at dusk. "
            "Goal posts visible, field markings chalked, bleachers empty, "
            "sunset sky with warm ambient light. Documentary, NOT cinematic "
            "color grading, NOT drone-aerial."
        ),
    ),
    GenericItemSpec(
        key="stadium_exterior", count=2,
        description="Stadium exterior on game day with fans arriving",
        prompt=(
            "Wire-service photograph of a generic NFL stadium exterior on a "
            "game day. Fans walking toward the entrance, flags flying, "
            "daylight. Documentary photojournalism, wide angle, NOT cinematic."
        ),
    ),
    GenericItemSpec(
        key="practice_generic", count=2,
        description="Generic NFL practice field (no identifying team)",
        prompt=(
            "Wire-service photograph of a generic NFL practice field. Players "
            "in plain practice uniforms with no identifying logos running "
            "drills, orange cones set up, a coach watching with arms folded. "
            "Daylight, documentary photojournalism."
        ),
    ),
    GenericItemSpec(
        key="combine", count=2,
        description="NFL Combine — player running 40-yard dash indoors",
        prompt=(
            "Wire-service photograph of the NFL Combine. A player in a "
            "compression shirt and shorts running a 40-yard dash on the "
            "indoor turf, scouts watching from the sidelines with stopwatches "
            "and clipboards. Bright interior arena lighting, documentary."
        ),
    ),
]


def _sorted_team_codes() -> list[str]:
    return sorted(NFL_TEAM_CODES)


@dataclass(frozen=True)
class PoolItem:
    slug: str
    team_code: str | None
    scene: str
    description: str
    prompt: str


def build_pool_items() -> list[PoolItem]:
    """Deterministically expand the spec into 250 concrete items."""
    items: list[PoolItem] = []

    for team in _sorted_team_codes():
        full = team_full_name(team) or team
        colors = team_colors(team) or "team colors"
        for scene in TEAM_SCENES:
            items.append(
                PoolItem(
                    slug=f"{team}_{scene.key}",
                    team_code=team,
                    scene=scene.key,
                    description=f"{full} — {scene.description}",
                    prompt=scene.prompt_template.format(
                        team_full=full, colors=colors
                    ) + UNIVERSAL_CONSTRAINTS,
                )
            )

    for generic in GENERIC_ITEMS:
        for i in range(1, generic.count + 1):
            items.append(
                PoolItem(
                    slug=f"generic_{generic.key}_{i:02d}",
                    team_code=None,
                    scene=generic.key,
                    description=f"Generic — {generic.description} (variant {i})",
                    prompt=generic.prompt + UNIVERSAL_CONSTRAINTS,
                )
            )

    return items


def pool_size() -> int:
    return len(_sorted_team_codes()) * len(TEAM_SCENES) + sum(
        g.count for g in GENERIC_ITEMS
    )
