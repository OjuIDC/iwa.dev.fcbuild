from __future__ import annotations

from typing import Any

from .models import AudienceProfile, Insight, Persona

_TEMPLATES: dict[str, dict[str, dict[str, tuple[str, str, str]]]] = {
    "en": {
        "goal": {
            "fan": ("GOAL!", "{team} changes the match. The score is now {score}.", "GOAL - {team} | {score}"),
            "analyst": ("Goal changes the game state", "{team} scores; the updated aggregate score is {score}.", "Goal | {team} | {score}"),
            "broadcaster": ("Goal", "{team} scores. Updated score: {score}.", "GOAL - {team} | {score}"),
        },
        "shot_speed": {
            "fan": ("A thunderous strike", "{player} lets fly at {speed} km/h{target}.", "{player} shot | {speed} km/h"),
            "analyst": ("High-velocity attempt", "{player}'s shot reached {speed} km/h; on-target flag: {on_target}.", "Shot speed {speed} km/h | OT {on_target}"),
            "broadcaster": ("Shot speed", "{player} strikes at {speed} km/h{target}.", "{speed} km/h shot - {player}"),
        },
        "pass_quality": {
            "fan": ("A daring pass", "{player} attempts a difficult {distance}-unit pass under pressure.", "Difficult pass - {player}"),
            "analyst": ("High-difficulty progression", "Pass difficulty {difficulty}/100 over {distance} units; progressive gain {progressive}; completed: {completed}.", "Pass difficulty {difficulty} | +{progressive}"),
            "broadcaster": ("Line-breaking attempt", "{player} tries a {distance}-unit progressive pass.", "Progressive pass - {player}"),
        },
        "pressing_surge": {
            "fan": ("The pressure is rising", "{team} is swarming: {events} high-pressure actions in 30 seconds.", "PRESSING SURGE - {team}"),
            "analyst": ("Sustained press detected", "{team} produced {events} high-pressure actions in 30 seconds, average intensity {average}.", "Press {events}/30s | intensity {average}"),
            "broadcaster": ("Pressing surge", "{team} has stepped up the press with {events} high-pressure actions.", "PRESSING SURGE - {team}"),
        },
        "possession_swing": {
            "fan": ("Momentum swings", "{team} has seized more of the ball, moving from {from_pct}% to {to_pct}% possession.", "Possession swing - {team}"),
            "analyst": ("Possession state change", "{team} possession changed {swing} points, from {from_pct}% to {to_pct}%.", "Possession {from_pct}% -> {to_pct}%"),
            "broadcaster": ("Possession swing", "{team} has moved from {from_pct}% to {to_pct}% possession.", "Possession swing - {team}"),
        },
        "tackle": {
            "fan": ("Won high up the pitch", "{player} wins the ball in dangerous territory.", "High regain - {player}"),
            "analyst": ("High regain", "{player} completes a tackle at field position {field_x}/100.", "High regain | x={field_x}"),
            "broadcaster": ("High regain", "{player} wins possession in the attacking third.", "High regain - {player}"),
        },
    },
    "es": {
        "goal": {
            "fan": ("¡GOL!", "{team} cambia el partido. El marcador ahora es {score}.", "GOL - {team} | {score}"),
            "analyst": ("El gol cambia el estado del partido", "Marca {team}; el marcador agregado es {score}.", "Gol | {team} | {score}"),
            "broadcaster": ("Gol", "Marca {team}. Marcador actualizado: {score}.", "GOL - {team} | {score}"),
        },
        "shot_speed": {"fan": ("Un disparo potentísimo", "{player} dispara a {speed} km/h{target}.", "Disparo de {player} | {speed} km/h"), "analyst": ("Remate de alta velocidad", "El disparo de {player} alcanzó {speed} km/h; a portería: {on_target}.", "Velocidad {speed} km/h | AP {on_target}"), "broadcaster": ("Velocidad del disparo", "{player} remata a {speed} km/h{target}.", "Disparo {speed} km/h - {player}")},
        "pass_quality": {"fan": ("Un pase atrevido", "{player} intenta un pase difícil de {distance} unidades bajo presión.", "Pase difícil - {player}"), "analyst": ("Progresión de alta dificultad", "Dificultad {difficulty}/100 en {distance} unidades; avance {progressive}; completado: {completed}.", "Dificultad {difficulty} | +{progressive}"), "broadcaster": ("Intento rompe-líneas", "{player} intenta un pase progresivo de {distance} unidades.", "Pase progresivo - {player}")},
        "pressing_surge": {"fan": ("Aumenta la presión", "{team} asfixia: {events} acciones de alta presión en 30 segundos.", "OLEADA DE PRESIÓN - {team}"), "analyst": ("Presión sostenida detectada", "{team} produjo {events} acciones de alta presión en 30 segundos, intensidad media {average}.", "Presión {events}/30s | intensidad {average}"), "broadcaster": ("Oleada de presión", "{team} sube la presión con {events} acciones intensas.", "OLEADA DE PRESIÓN - {team}")},
        "possession_swing": {"fan": ("Cambia el impulso", "{team} toma el balón: del {from_pct}% al {to_pct}% de posesión.", "Cambio de posesión - {team}"), "analyst": ("Cambio de estado de posesión", "La posesión de {team} cambió {swing} puntos, del {from_pct}% al {to_pct}%.", "Posesión {from_pct}% -> {to_pct}%"), "broadcaster": ("Cambio de posesión", "{team} pasa del {from_pct}% al {to_pct}% de posesión.", "Cambio de posesión - {team}")},
        "tackle": {"fan": ("Recuperación arriba", "{player} recupera el balón en zona peligrosa.", "Recuperación alta - {player}"), "analyst": ("Recuperación alta", "{player} completa una entrada en la posición {field_x}/100.", "Recuperación alta | x={field_x}"), "broadcaster": ("Recuperación alta", "{player} gana la posesión en el último tercio.", "Recuperación alta - {player}")},
    },
    "fr": {
        "goal": {"fan": ("BUT !", "{team} change le match. Le score est maintenant {score}.", "BUT - {team} | {score}"), "analyst": ("Le but change l'état du match", "{team} marque ; le score agrégé est {score}.", "But | {team} | {score}"), "broadcaster": ("But", "{team} marque. Score actualisé : {score}.", "BUT - {team} | {score}")},
        "shot_speed": {"fan": ("Une frappe surpuissante", "{player} frappe à {speed} km/h{target}.", "Frappe de {player} | {speed} km/h"), "analyst": ("Tentative à haute vitesse", "La frappe de {player} atteint {speed} km/h ; cadrée : {on_target}.", "Vitesse {speed} km/h | cadrée {on_target}"), "broadcaster": ("Vitesse de frappe", "{player} frappe à {speed} km/h{target}.", "Frappe {speed} km/h - {player}")},
        "pass_quality": {"fan": ("Une passe audacieuse", "{player} tente une passe difficile de {distance} unités sous pression.", "Passe difficile - {player}"), "analyst": ("Progression à haute difficulté", "Difficulté {difficulty}/100 sur {distance} unités ; gain {progressive} ; réussie : {completed}.", "Difficulté {difficulty} | +{progressive}"), "broadcaster": ("Tentative entre les lignes", "{player} tente une passe progressive de {distance} unités.", "Passe progressive - {player}")},
        "pressing_surge": {"fan": ("La pression monte", "{team} étouffe l'adversaire : {events} actions de forte pression en 30 secondes.", "VAGUE DE PRESSING - {team}"), "analyst": ("Pressing soutenu détecté", "{team} produit {events} actions de forte pression en 30 secondes, intensité moyenne {average}.", "Pressing {events}/30s | intensité {average}"), "broadcaster": ("Vague de pressing", "{team} intensifie le pressing avec {events} actions fortes.", "VAGUE DE PRESSING - {team}")},
        "possession_swing": {"fan": ("Le momentum bascule", "{team} prend le ballon, de {from_pct}% à {to_pct}% de possession.", "Bascule de possession - {team}"), "analyst": ("Changement de possession", "La possession de {team} évolue de {swing} points, de {from_pct}% à {to_pct}%.", "Possession {from_pct}% -> {to_pct}%"), "broadcaster": ("Bascule de possession", "{team} passe de {from_pct}% à {to_pct}% de possession.", "Bascule de possession - {team}")},
        "tackle": {"fan": ("Récupération haute", "{player} récupère le ballon dans une zone dangereuse.", "Récupération haute - {player}"), "analyst": ("Récupération haute", "{player} réussit un tacle à la position {field_x}/100.", "Récupération haute | x={field_x}"), "broadcaster": ("Récupération haute", "{player} gagne le ballon dans le dernier tiers.", "Récupération haute - {player}")},
    },
}


def _values(insight: Insight) -> dict[str, Any]:
    stats = dict(insight.stats)
    on_target = int(stats.get("on_target", 0))
    return {
        "team": insight.team_id or "Team",
        "player": insight.player_id or "Player",
        "score": stats.get("score", ""),
        "speed": stats.get("speed_kmh", ""),
        "target": " on target" if on_target else "",
        "on_target": on_target,
        "distance": stats.get("distance_units", ""),
        "difficulty": stats.get("difficulty", ""),
        "progressive": stats.get("progressive_units", ""),
        "completed": stats.get("completed", ""),
        "events": stats.get("high_pressure_events_30s", ""),
        "average": stats.get("average_pressure", ""),
        "from_pct": stats.get("from_pct", ""),
        "to_pct": stats.get("to_pct", ""),
        "swing": stats.get("swing_points", ""),
        "field_x": stats.get("field_x", ""),
    }


def render(insight: Insight, profile: AudienceProfile) -> tuple[str, str, str]:
    language = _TEMPLATES.get(profile.locale, _TEMPLATES["en"])
    templates = language.get(insight.kind, _TEMPLATES["en"].get(insight.kind))
    if not templates:
        return ("Match insight", str(insight.stats), str(insight.stats))
    persona = profile.persona.value
    title, body, overlay = templates.get(persona, templates[Persona.FAN.value])
    values = _values(insight)
    return title.format(**values), body.format(**values), overlay.format(**values)
