"""
agency_org.py
=============
FC27 Pro Clubs — Agency Organisation Chart
East Coasters FC Intelligence Platform

Defines the role hierarchy, agent-to-role mapping,
communication flow rules, and escalation paths.
Load this as shared context into any swarm framework.
"""

from __future__ import annotations

import json
import os
from typing import Any

FC_VERSION   = "27"
PLATFORM     = os.getenv("FC_PLATFORM",  "common-gen5")
CLUB_ID      = os.getenv("FC27_CLUB_ID", "330323")
COMMISSIONER = os.getenv("COMMISSIONER", "batmanL8")

# ── Organisation Chart ─────────────────────────────────────────────────────────

ORG_CHART: dict[str, Any] = {
    "club":        "East Coasters FC",
    "club_id":     CLUB_ID,
    "platform":    PLATFORM,
    "fc_version":  FC_VERSION,
    "hierarchy": [
        {
            "role":        "Commissioner",
            "agent":       None,                    # Human — batmanL8
            "human":       True,
            "gamertag":    COMMISSIONER,
            "authority":   "SUPREME",
            "can_approve": True,
            "can_reject":  True,
            "description": (
                "The Commissioner is the human controller (batmanL8). "
                "No agent action executes without explicit Commissioner approval. "
                "The Commissioner is the only entity that can activate training programs, "
                "deploy tactical packages, or approve transfer moves."
            ),
            "direct_reports": ["DirectorPrime"],
        },
        {
            "role":        "Director of Football",
            "agent":       "DirectorPrime",
            "human":       False,
            "authority":   "HIGH",
            "can_approve": False,
            "can_reject":  False,
            "description": (
                "Front office manager. Handles roster strategy, transfer pipeline, "
                "budget allocation, season planning. Reports to Commissioner. "
                "Instructs ScoutAlpha for recruitment. Instructs TacticsCore for matchday."
            ),
            "direct_reports": ["ScoutAlpha", "TacticsCore"],
            "reports_to":     "Commissioner",
        },
        {
            "role":        "Head of Recruitment",
            "agent":       "ScoutAlpha",
            "human":       False,
            "authority":   "MEDIUM",
            "can_approve": False,
            "can_reject":  False,
            "description": (
                "Identifies and benchmarks recruitment targets using FC27 API data. "
                "Produces scouting reports for Commissioner review. "
                "Cannot approach players or clubs without Commissioner approval. "
                "Passes onboarded recruits to CoachPrime."
            ),
            "direct_reports": ["CoachPrime"],
            "reports_to":     "DirectorPrime",
        },
        {
            "role":        "Tactical Director",
            "agent":       "TacticsCore",
            "human":       False,
            "authority":   "MEDIUM",
            "can_approve": False,
            "can_reject":  False,
            "description": (
                "Designs formations, custom tactics, and player role instructions. "
                "Prepares matchday tactical packages for Commissioner approval. "
                "Cannot deploy any tactical config without approval. "
                "Receives analyst data from AnalystUnit for opponent assessment."
            ),
            "direct_reports": [],
            "reports_to":     "DirectorPrime",
        },
        {
            "role":        "Head Coach / Development",
            "agent":       "CoachPrime",
            "human":       False,
            "authority":   "MEDIUM",
            "can_approve": False,
            "can_reject":  False,
            "description": (
                "Designs FC27 build programs: AP spend plans, Mastery tree progressions, "
                "Amp selections, skill move drill sequences. "
                "Receives training briefs from AnalystUnit. "
                "Cannot activate any program without Commissioner approval."
            ),
            "direct_reports": [],
            "reports_to":     "ScoutAlpha",
        },
        {
            "role":        "Match Analyst",
            "agent":       "AnalystUnit",
            "human":       False,
            "authority":   "LOW",
            "can_approve": False,
            "can_reject":  False,
            "description": (
                "Parses FC27 synthetic match data. Generates match narratives, "
                "identifies performance gaps, commissions training briefs to CoachPrime. "
                "Flags Fair Play violations directly to Commissioner (bypasses normal chain). "
                "Cannot commission training without Commissioner approval via CoachPrime."
            ),
            "direct_reports": ["CoachPrime"],
            "reports_to":     "DirectorPrime",
        },
    ]
}

# ── Communication Flow Rules ───────────────────────────────────────────────────

COMMUNICATION_FLOWS: list[dict] = [
    {
        "from":    "Commissioner",
        "to":      "Any",
        "type":    "instruction",
        "requires_approval": False,
        "note":    "Commissioner is the only source of authorised instructions.",
    },
    {
        "from":    "DirectorPrime",
        "to":      "ScoutAlpha",
        "type":    "recruitment_brief",
        "requires_approval": True,
        "note":    "Director instructs Scout after Commissioner approves transfer strategy.",
    },
    {
        "from":    "DirectorPrime",
        "to":      "TacticsCore",
        "type":    "tactical_brief",
        "requires_approval": True,
        "note":    "Director instructs Tactics after Commissioner approves matchday prep.",
    },
    {
        "from":    "AnalystUnit",
        "to":      "CoachPrime",
        "type":    "training_brief",
        "requires_approval": True,
        "note":    "Analyst commissions training — CoachPrime awaits Commissioner approval before activating.",
    },
    {
        "from":    "AnalystUnit",
        "to":      "Commissioner",
        "type":    "fair_play_flag",
        "requires_approval": False,
        "note":    "Fair Play flags bypass normal chain. Go direct to Commissioner immediately.",
    },
    {
        "from":    "ScoutAlpha",
        "to":      "CoachPrime",
        "type":    "recruit_handoff",
        "requires_approval": True,
        "note":    "Scout passes approved recruits to Coach for onboarding program design.",
    },
]

# ── Escalation Rules ───────────────────────────────────────────────────────────

ESCALATION_RULES: list[dict] = [
    {
        "trigger":   "tilt_detected",
        "condition": "3 or more consecutive losses in last 5 matches",
        "from":      "AnalystUnit",
        "to":        "Commissioner",
        "via":       "DirectorPrime",
        "action":    "Squad rotation recommendation + form reset brief",
        "priority":  "HIGH",
    },
    {
        "trigger":   "fair_play_violation",
        "condition": "Statistical anomaly suggesting exploit play",
        "from":      "AnalystUnit",
        "to":        "Commissioner",
        "via":       "DIRECT",
        "action":    "Immediate flag — match review required before next game",
        "priority":  "CRITICAL",
    },
    {
        "trigger":   "relegation_risk",
        "condition": "Club in bottom 2 of division with 5 or fewer games remaining",
        "from":      "DirectorPrime",
        "to":        "Commissioner",
        "via":       "DIRECT",
        "action":    "Emergency tactical review + form analysis brief",
        "priority":  "HIGH",
    },
    {
        "trigger":   "rate_limit_breach",
        "condition": "MCP server returns rate limit error",
        "from":      "MCP Server",
        "to":        "All Agents",
        "via":       "MCP error propagation",
        "action":    "Back off, log, retry after window. Do not retry immediately.",
        "priority":  "MEDIUM",
    },
]

# ── Role-to-Agent Mapping ──────────────────────────────────────────────────────

ROLE_MAP: dict[str, str | None] = {
    "Commissioner":         None,          # Human
    "DirectorPrime":        "DirectorPrime",
    "ScoutAlpha":           "ScoutAlpha",
    "TacticsCore":          "TacticsCore",
    "CoachPrime":           "CoachPrime",
    "AnalystUnit":          "AnalystUnit",
}

# ── FC27 Archetype Roster Assignment ──────────────────────────────────────────

ROSTER: list[dict] = [
    {"gamertag": "Mr GoodieGREEN",   "position": "MID", "archetype": "Recycler", "arch_id": 9,  "avg_rating": 8.1, "status": "active"},
    {"gamertag": "butter84",         "position": "FWD/DEF", "archetype": "Dual",  "arch_id": "4/8", "avg_rating": 8.1, "status": "active"},
    {"gamertag": "Willyumm24",       "position": "MID", "archetype": "arch11",   "arch_id": 11, "avg_rating": 8.3, "status": "active"},
    {"gamertag": "BKhardscoper",     "position": "FWD", "archetype": "arch12",   "arch_id": 12, "avg_rating": 7.8, "status": "active"},
    {"gamertag": "Fa11enGemini",     "position": "MID/FWD", "archetype": "arch13","arch_id": 13, "avg_rating": 7.5, "status": "active"},
    {"gamertag": "Chriskohh",        "position": "MID/FWD", "archetype": "arch9/11","arch_id": "9/11", "avg_rating": 7.6, "status": "active"},
    {"gamertag": "RoseSoup7591004",  "position": "MID", "archetype": "arch9",    "arch_id": 9,  "avg_rating": 6.4, "status": "development"},
    {"gamertag": "batmanL8",         "position": "CM",  "archetype": "Recycler", "arch_id": 9,  "avg_rating": 7.8, "status": "commissioner"},
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_org_chart_json() -> str:
    return json.dumps(ORG_CHART, indent=2)

def get_agent_role(agent_name: str) -> dict | None:
    for role in ORG_CHART["hierarchy"]:
        if role.get("agent") == agent_name:
            return role
    return None

def get_communication_rules(from_agent: str) -> list[dict]:
    return [f for f in COMMUNICATION_FLOWS if f["from"] == from_agent]

def get_escalation_rules(trigger: str) -> list[dict]:
    return [r for r in ESCALATION_RULES if r["trigger"] == trigger]

def get_roster() -> list[dict]:
    return ROSTER

def print_org_chart() -> None:
    print(f"\n{'='*60}")
    print(f"  EAST COASTERS FC — AGENCY ORG CHART")
    print(f"  Club ID: {CLUB_ID} | Platform: {PLATFORM} | FC{FC_VERSION}")
    print(f"{'='*60}")
    for role in ORG_CHART["hierarchy"]:
        indent = "  " if role["role"] != "Commissioner" else ""
        human  = " 👤 HUMAN" if role["human"] else " 🤖 AGENT"
        print(f"{indent}{'  ' if role.get('reports_to') else ''}{role['role']}{human}")
        if role.get("agent"):
            print(f"{indent}  └─ Agent: {role['agent']}")
        print(f"{indent}  └─ Authority: {role['authority']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    print_org_chart()
    print("Communication flows:")
    for flow in COMMUNICATION_FLOWS:
        approval = "⚠️ REQUIRES APPROVAL" if flow["requires_approval"] else "✅ DIRECT"
        print(f"  {flow['from']} → {flow['to']} [{flow['type']}] {approval}")
    print("\nEscalation rules:")
    for rule in ESCALATION_RULES:
        print(f"  [{rule['priority']}] {rule['trigger']}: {rule['action']}")
