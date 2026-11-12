"""The shipped rehearsal encounter and its scenario builder.

The encounter is intentionally dense so one run exercises every cross-phase
rule.  The boss has 1000 HP across three phases:

* P1 (100%-60%): overlapping threshold mechanics contend for the same object;
  an interruptible channel and a periodic pulse run on timers.
* P2 (60%-30%): entry additionally requires the wardstone destroyed; one
  committed P1 mechanic is configured to carry over, another to cancel.
* P3 (<30%): final burn; entering it wins the encounter while any surviving
  cross-phase mechanic is explicitly torn down.

Three test characters (player-1..3) can damage targets, interrupt and die.
"""

from __future__ import annotations

from .config import (
    BossConfig,
    ConditionType,
    CrossPhasePolicy,
    EncounterConfig,
    MechanicConfig,
    PhaseCondition,
    PhaseConfig,
    TargetConfig,
    TargetState,
    TriggerType,
)

BOSS_ID = "boss-overlord"
WARDSTONE = "wardstone"
BRAZIER = "brazier"


def build_encounter_config() -> EncounterConfig:
    boss = BossConfig(boss_id=BOSS_ID, name="The Overlord", max_hp=1000.0)
    targets = [
        TargetConfig(target_id=WARDSTONE, name="Wardstone", max_hp=200.0),
        TargetConfig(target_id=BRAZIER, name="Soul Brazier", max_hp=120.0),
    ]

    phase_one = PhaseConfig(
        phase_id="p1",
        name="Opening Pressure",
        entry=[],
        mechanics=[
            # Two mechanics share the exact 80% boundary and the same object.
            MechanicConfig(
                mechanic_id="p1_seal",
                name="Seal the Brazier",
                trigger=TriggerType.HP_THRESHOLD,
                value=80,
                duration=12,
                resources=["object:brazier", "position:center"],
                mutex_group="center_control",
                cross_phase=CrossPhasePolicy.CONTINUE,
                priority=10,
            ),
            MechanicConfig(
                mechanic_id="p1_surge",
                name="Mana Surge",
                trigger=TriggerType.HP_THRESHOLD,
                value=80,
                duration=8,
                resources=["object:brazier"],
                mutex_group="center_control",
                priority=20,
            ),
            # Interruptible channel; test characters can break it.
            MechanicConfig(
                mechanic_id="p1_channel",
                name="Void Channel",
                trigger=TriggerType.TIMER,
                value=5,
                duration=10,
                resources=["position:north_dais"],
                interruptible=True,
            ),
            # Periodic pulse keeps re-arming until the phase ends.
            MechanicConfig(
                mechanic_id="p1_pulse",
                name="Reverberating Pulse",
                trigger=TriggerType.TIMER,
                value=8,
                duration=2,
                resources=["position:west_ring"],
                repeat_interval=8,
                cross_phase=CrossPhasePolicy.CANCEL,
            ),
            # Boundary shared with the P2 HP gate: crossing 60% fires this
            # before the phase entry conditions are re-evaluated.
            MechanicConfig(
                mechanic_id="p1_last_wrath",
                name="Last Wrath",
                trigger=TriggerType.HP_THRESHOLD,
                value=60,
                duration=6,
                resources=["position:east_ring"],
                cross_phase=CrossPhasePolicy.CANCEL,
            ),
        ],
    )

    phase_two = PhaseConfig(
        phase_id="p2",
        name="Wardbreaker",
        entry=[
            PhaseCondition(kind=ConditionType.HP_BELOW, ref=60),
            PhaseCondition(
                kind=ConditionType.TARGET_STATE, ref=WARDSTONE, state=TargetState.DESTROYED
            ),
        ],
        mechanics=[
            # Timer scheduled at P2 entry (t+10 relative to entry).
            MechanicConfig(
                mechanic_id="p2_cataclysm",
                name="Cataclysm",
                trigger=TriggerType.TIMER,
                value=10,
                duration=6,
                resources=["position:center"],
                mutex_group="center_control",
                cross_phase=CrossPhasePolicy.CANCEL,
            ),
            MechanicConfig(
                mechanic_id="p2_chain",
                name="Soul Chain",
                trigger=TriggerType.TIMER,
                value=4,
                duration=20,
                resources=["object:wardstone_anchor"],
                cross_phase=CrossPhasePolicy.CONTINUE,
            ),
            MechanicConfig(
                mechanic_id="p2_burn",
                name="Brazier Burn",
                trigger=TriggerType.HP_THRESHOLD,
                value=45,
                duration=10,
                resources=["object:brazier"],
                required_targets=[BRAZIER],
                cross_phase=CrossPhasePolicy.CANCEL,
            ),
        ],
    )

    phase_three = PhaseConfig(
        phase_id="p3",
        name="Final Burn",
        entry=[PhaseCondition(kind=ConditionType.HP_BELOW, ref=30)],
        mechanics=[
            MechanicConfig(
                mechanic_id="p3_despair",
                name="Final Despair",
                trigger=TriggerType.HP_THRESHOLD,
                value=20,
                duration=5,
                resources=["position:center"],
                mutex_group="center_control",
            ),
        ],
        victory_phase=True,
    )

    config = EncounterConfig(
        encounter_id="overlord-rehearsal",
        name="The Overlord, Rehearsal Cut",
        boss=boss,
        targets=targets,
        phases=[phase_one, phase_two, phase_three],
        player_slots=3,
    )
    config.validate()
    return config
